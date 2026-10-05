import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from klepa_core.host import runtime as rt
from klepa_core.host.reference import HostLayout

VERSION = "9.9.9"
TOP = "node-v9.9.9-darwin-arm64"


def node_archive(
    path: Path, *, extra: list[tuple[str, bytes]] | None = None, link: tuple[str, str] | None = None
) -> str:
    """A small stand-in for Node's archive: bin/node prints its version. Returns its SHA-256."""
    with tarfile.open(path, "w:xz") as tar:
        for name, data, mode in [
            (f"{TOP}/bin/node", f"#!/bin/sh\necho v{VERSION}\n".encode(), 0o755),
            (f"{TOP}/README.md", b"node\n", 0o644),
            *((name, data, 0o644) for name, data in extra or []),
        ]:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
        if link is not None:
            info = tarfile.TarInfo(link[0])
            info.type, info.linkname = tarfile.SYMTYPE, link[1]
            tar.addfile(info)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copying(source: Path, calls: list[str]):
    def fetch(url: str, target: Path, limit: int) -> None:
        calls.append(url)
        target.write_bytes(source.read_bytes())

    return fetch


def ensure(runtime, archive: Path, digest: str, calls: list[str]) -> None:
    rt.ensure_node(
        runtime,
        fetch=copying(archive, calls),
        say=lambda line: None,
        url=f"https://nodejs.example/{archive.name}",
        archive_sha256=digest,
        version=VERSION,
    )


def test_node_is_downloaded_checked_and_unpacked_once(tmp_path):
    archive = tmp_path / f"{TOP}.tar.xz"
    digest = node_archive(archive)
    runtime = rt.Runtime(tmp_path / "runtime")
    calls: list[str] = []
    ensure(runtime, archive, digest, calls)
    assert subprocess.run([runtime.node], capture_output=True, text=True).stdout.strip() == f"v{VERSION}"
    assert (runtime.node_dir / "README.md").exists()
    assert (runtime.downloads / archive.name).exists()
    ensure(runtime, archive, digest, calls)
    assert len(calls) == 1  # the second time Node was there already


def test_an_archive_that_does_not_match_its_checksum_installs_nothing(tmp_path):
    archive = tmp_path / f"{TOP}.tar.xz"
    node_archive(archive)
    runtime = rt.Runtime(tmp_path / "runtime")
    with pytest.raises(rt.HostRuntimeError, match="SHA-256"):
        ensure(runtime, archive, "0" * 64, [])
    assert not runtime.node_dir.exists()
    assert list(runtime.downloads.iterdir()) == []


@pytest.mark.parametrize(
    ("extra", "link", "message"),
    [
        ([("elsewhere/file", b"x")], None, "outside"),
        ([], (f"{TOP}/bin/escape", "../../../../etc/passwd"), "Node archive"),
    ],
)
def test_an_archive_that_reaches_outside_its_folder_is_refused(tmp_path, extra, link, message):
    archive = tmp_path / f"{TOP}.tar.xz"
    digest = node_archive(archive, extra=extra, link=link)
    runtime = rt.Runtime(tmp_path / "runtime")
    with pytest.raises(rt.HostRuntimeError, match=message):
        ensure(runtime, archive, digest, [])
    assert not runtime.node_dir.exists()


class FakeRunner:
    """npm and node as the tests need them: npm ci leaves an OpenClaw behind."""

    def __init__(self, runtime, *, npm_fails: bool = False, reports: str = rt.OPENCLAW_VERSION) -> None:
        self.runtime = runtime
        self.npm_fails = npm_fails
        self.reports = reports
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def __call__(self, argv, *, env=None, timeout=600.0):
        argv = [str(part) for part in argv]
        self.calls.append((argv, env))
        if argv[0] == str(self.runtime.npm):
            if self.npm_fails:
                return subprocess.CompletedProcess(argv, 1, "", "npm error code EINTEGRITY")
            self.runtime.openclaw_entry.parent.mkdir(parents=True, exist_ok=True)
            self.runtime.openclaw_entry.write_text("// openclaw\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, f"OpenClaw {self.reports} (3a9d69d)\n", "")


def with_node(runtime) -> None:
    runtime.node.parent.mkdir(parents=True)
    runtime.node.write_text("#!/bin/sh\n")


def test_openclaw_comes_from_the_pinned_lockfile_without_install_scripts(tmp_path):
    runtime = rt.Runtime(tmp_path / "runtime")
    with_node(runtime)
    run = FakeRunner(runtime)
    rt.ensure_openclaw(runtime, run=run, say=lambda line: None)
    npm = [call for call in run.calls if call[0][0] == str(runtime.npm)]
    assert len(npm) == 1
    argv, env = npm[0]
    assert argv[1:] == [
        "ci",
        "--omit=dev",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--prefix",
        str(runtime.openclaw_dir),
    ]
    assert env is not None
    assert env["PATH"].startswith(str(runtime.node.parent))
    assert "ANTHROPIC_API_KEY" not in env
    lock = json.loads((runtime.openclaw_dir / "package-lock.json").read_text())
    assert lock["packages"]["node_modules/openclaw"]["version"] == rt.OPENCLAW_VERSION
    rt.ensure_openclaw(runtime, run=run, say=lambda line: None)
    assert len([call for call in run.calls if call[0][0] == str(runtime.npm)]) == 1  # nothing to do the second time


def test_a_failed_or_wrong_openclaw_install_is_reported(tmp_path):
    runtime = rt.Runtime(tmp_path / "runtime")
    with_node(runtime)
    with pytest.raises(rt.HostRuntimeError, match="EINTEGRITY"):
        rt.ensure_openclaw(runtime, run=FakeRunner(runtime, npm_fails=True), say=lambda line: None)
    with pytest.raises(rt.HostRuntimeError, match=r"not 2026\.9\.4"):
        rt.ensure_openclaw(runtime, run=FakeRunner(runtime, reports="2026.9.5"), say=lambda line: None)


def test_the_adapter_is_copied_and_any_changed_file_counts(tmp_path):
    runtime = rt.Runtime(tmp_path / "runtime")
    assert rt.install_adapter(runtime) is True
    assert rt.sha256_of(runtime.adapter_dir / "index.ts") == rt.adapter_sha256()
    assert sorted(path.name for path in runtime.adapter_dir.iterdir()) == sorted(rt.ADAPTER_FILES)
    manifest = json.loads((runtime.adapter_dir / "openclaw.plugin.json").read_text())
    assert manifest["id"] == "klepa-adapter"
    assert rt.install_adapter(runtime) is False
    (runtime.adapter_dir / "openclaw.plugin.json").write_text("{}")  # the manifest alone: the gateway read it too
    assert rt.install_adapter(runtime) is True


def test_the_wrapper_starts_openclaw_with_the_engines_environment_only(tmp_path, monkeypatch):
    runtime = rt.Runtime(tmp_path / "Application Support é" / "runtime")  # a space, as in the real path, and more
    layout = HostLayout(runtime_dir=runtime.root, host_dir=tmp_path / "data" / "host")
    runtime.node.parent.mkdir(parents=True)
    runtime.node.write_text('#!/bin/sh\necho "args: $*"\nenv\n')  # a stand-in that shows what it was given
    runtime.node.chmod(0o755)
    wrapper = rt.write_wrapper(runtime, layout)
    assert os.stat(wrapper).st_mode & 0o777 == 0o700
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak")
    monkeypatch.setenv("HTTPS_PROXY", "http://elsewhere.example")
    out = subprocess.run([wrapper, "models", "list"], capture_output=True, text=True, check=True).stdout
    assert f"args: {runtime.openclaw_entry} --profile klepa models list" in out
    assert f"OPENCLAW_CONFIG_PATH={layout.config_path}" in out
    assert "OPENCLAW_CONFIG_READONLY=1" in out
    assert "OPENCLAW_NO_RESPAWN=1" in out
    assert "leak" not in out
    assert "elsewhere" not in out


def test_the_gateway_environment_is_the_spec_one(tmp_path):
    runtime = rt.Runtime(tmp_path / "runtime")
    layout = HostLayout(runtime_dir=runtime.root, host_dir=tmp_path / "host")
    env = rt.gateway_env(runtime, layout)
    assert env["OPENCLAW_SUPERVISOR_MODE"] == "external"
    assert env["OPENCLAW_SERVICE_REPAIR_POLICY"] == "external"
    assert env["OPENCLAW_NO_RESPAWN"] == "1"  # without it a restart leaves the gateway down (spike report, 35)
    assert env["OPENCLAW_CONFIG_READONLY"] == "1"
    assert env["OPENCLAW_STATE_DIR"] == str(layout.state_dir)
    assert env["HOME"] == str(layout.home)  # the gateway's own: nothing of the owner's by default paths


def answering(answer: dict):
    calls = []

    def run(argv, *, env=None, timeout=600.0):
        calls.append([str(part) for part in argv])
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_the_ownership_claim_must_name_core(tmp_path):
    runtime = rt.Runtime(tmp_path / "runtime")
    layout = HostLayout(runtime_dir=runtime.root, host_dir=tmp_path / "host")
    run = answering({"status": "external", "ownership": {"managerId": "klepa-core"}})
    rt.claim_ownership(runtime, layout, run=run)
    assert run.calls[0][-5:] == ["ownership", "claim", "--manager", "klepa-core", "--json"]
    with pytest.raises(rt.HostRuntimeError, match="already claimed"):
        rt.claim_ownership(runtime, layout, run=answering({"error": "already claimed by someone"}))


def test_a_config_openclaw_refuses_stops_the_install(tmp_path):
    runtime = rt.Runtime(tmp_path / "runtime")
    layout = HostLayout(runtime_dir=runtime.root, host_dir=tmp_path / "host")
    rt.validate_config(runtime, layout, run=answering({"valid": True, "warnings": []}))
    with pytest.raises(rt.HostRuntimeError, match="refuses"):
        rt.validate_config(runtime, layout, run=answering({"valid": False, "issues": [{"path": "tools"}]}))


def test_downloads_are_https_only(tmp_path):
    with pytest.raises(rt.HostRuntimeError, match="plain HTTP"):
        rt._fetch("http://nodejs.org/dist/node.tar.xz", tmp_path / "x", 10)


def test_a_new_lockfile_installs_into_a_folder_of_its_own(tmp_path):
    runtime = rt.Runtime(tmp_path / "runtime")
    digest = hashlib.sha256(rt.packaged("runtime", "package-lock.json").read_bytes()).hexdigest()
    assert rt.lock_tag() == digest[:12]
    assert runtime.openclaw_dir.name == f"openclaw-2026.9.4-{digest[:12]}"


def test_private_files_are_replaced_whole_or_not_at_all(tmp_path, monkeypatch):
    target = tmp_path / "openclaw.json"
    rt.write_private(target, b"old")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def full(*args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rt.os, "replace", full)
    with pytest.raises(OSError, match="No space"):
        rt.write_private(target, b"new")
    assert target.read_bytes() == b"old"
    assert [path.name for path in tmp_path.iterdir()] == ["openclaw.json"]  # no temporary file left


def test_the_pinned_node_is_refused_on_another_kind_of_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(rt.platform, "machine", lambda: "x86_64")

    def never(*args):
        raise AssertionError("nothing may be downloaded")

    with pytest.raises(rt.HostRuntimeError, match="Apple silicon"):
        rt.ensure_node(rt.Runtime(tmp_path / "runtime"), fetch=never, say=lambda line: None)


def test_a_failed_openclaw_command_raises_whatever_it_printed(tmp_path):
    runtime = rt.Runtime(tmp_path / "runtime")
    layout = HostLayout(runtime_dir=runtime.root, host_dir=tmp_path / "host")

    def failing(argv, *, env=None, timeout=600.0, input=None):
        return subprocess.CompletedProcess(argv, 1, json.dumps({"ok": False, "error": "unknown method"}), "")

    with pytest.raises(rt.HostRuntimeError, match="unknown method"):
        rt.openclaw_json(runtime, layout, "gateway", "call", "tools.effective", run=failing)
