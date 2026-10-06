"""The gateway's own runtime (spec 4.6 "own installation", 7.1, D30): a pinned Node, a pinned OpenClaw, the adapter
plugin and the klepa-openclaw wrapper, in Klepa's program folder and apart from any other OpenClaw on the Mac.

- Node comes from nodejs.org as one archive, checked against the SHA-256 pinned here before anything is unpacked.
- OpenClaw comes from npm with the lockfile that ships with Core, so npm checks every package against its integrity
  hash, and no package runs an install script.
- Each version lives in a folder of its own, so a new version never half-replaces a working one.
- The wrapper and the launchd agent start OpenClaw with the same environment: Core's config and state paths, external
  supervision, no respawn, a config OpenClaw cannot write (spec 4.6). The wrapper starts from an empty environment, so
  nothing from the owner's shell (an API key, a proxy) leaks into it.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .reference import MODEL_PROFILE, HostLayout

NODE_VERSION = "24.21.0"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-darwin-arm64.tar.xz"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ARCHIVE}"
NODE_SHA256 = "6239d4cf92d864487ec8cd3615038f7b67e7f58b77b21cd2f09ea9fbd68065fe"
NODE_MAX_BYTES = 100 * 1024 * 1024
OPENCLAW_VERSION = "2026.9.4"
PROFILE = "klepa"
OWNERSHIP_MANAGER = "klepa-core"
ADAPTER_FILES = ("index.ts", "mcp.ts", "openclaw.plugin.json", "package.json")
RUNTIME_FILES = ("package.json", "package-lock.json")
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class HostRuntimeError(Exception):
    """The runtime could not be installed or does not work. The message says what to do."""


Fetch = Callable[[str, Path, int], None]
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(
    argv: Sequence[str], *, env: dict[str, str] | None = None, timeout: float = 600.0, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, env=env, timeout=timeout, input=input
    )


def _fetch(url: str, target: Path, limit: int) -> None:
    """Download `url` into `target`, refusing more than `limit` bytes."""
    if not url.startswith("https://"):
        raise HostRuntimeError(f"refusing to download over plain HTTP: {url}")
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as out:
        size = 0
        while chunk := response.read(1 << 20):
            size += len(chunk)
            if size > limit:
                raise HostRuntimeError(f"{url} is larger than {limit} bytes")
            out.write(chunk)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def packaged(*parts: str) -> Path:
    """A file Core ships for the gateway: the adapter plugin or the runtime's lockfile."""
    return Path(str(resources.files("klepa_core.host").joinpath("openclaw", *parts)))


@dataclass(frozen=True)
class Runtime:
    root: Path

    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    @property
    def node_dir(self) -> Path:
        return self.root / f"node-v{NODE_VERSION}"

    @property
    def node(self) -> Path:
        return self.node_dir / "bin" / "node"

    @property
    def npm(self) -> Path:
        return self.node_dir / "bin" / "npm"

    @property
    def openclaw_dir(self) -> Path:
        """A folder per lockfile: a new lockfile never rebuilds the packages a running gateway uses."""
        return self.root / f"openclaw-{OPENCLAW_VERSION}-{lock_tag()}"

    @property
    def openclaw_entry(self) -> Path:
        return self.openclaw_dir / "node_modules" / "openclaw" / "openclaw.mjs"

    @property
    def adapter_dir(self) -> Path:
        return self.root / "adapter"

    @property
    def wrapper(self) -> Path:
        return self.root / "bin" / "klepa-openclaw"


@functools.cache
def lock_tag() -> str:
    return sha256_of(packaged("runtime", "package-lock.json"))[:12]


def gateway_env(runtime: Runtime, layout: HostLayout) -> dict[str, str]:
    """The whole environment of every OpenClaw process the engine starts (spec 4.6, 7.1)."""
    return {
        "PATH": f"{runtime.node.parent}:{SYSTEM_PATH}",
        "HOME": str(layout.home),
        "OPENCLAW_CONFIG_PATH": str(layout.config_path),
        "OPENCLAW_STATE_DIR": str(layout.state_dir),
        "OPENCLAW_SUPERVISOR_MODE": "external",
        "OPENCLAW_SERVICE_REPAIR_POLICY": "external",
        "OPENCLAW_NO_RESPAWN": "1",
        "OPENCLAW_CONFIG_READONLY": "1",
        "DO_NOT_TRACK": "1",
        "NO_COLOR": "1",
    }


def openclaw_argv(runtime: Runtime, *args: str) -> list[str]:
    return [str(runtime.node), str(runtime.openclaw_entry), "--profile", PROFILE, *args]


def ensure_node(
    runtime: Runtime,
    *,
    fetch: Fetch = _fetch,
    run: Runner = _run,
    say: Callable[[str], None] = print,
    url: str = NODE_URL,
    archive_sha256: str = NODE_SHA256,
    version: str = NODE_VERSION,
) -> None:
    """Node of the pinned version in its own folder; downloaded and checked only when missing."""
    if _node_version(runtime, run) == f"v{version}":
        return
    if url == NODE_URL and (sys.platform, platform.machine()) != ("darwin", "arm64"):
        raise HostRuntimeError("the pinned Node is built for Macs with Apple silicon, and this machine is not one")
    runtime.downloads.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = runtime.downloads / Path(url).name
    if not archive.exists() or sha256_of(archive) != archive_sha256:
        say(f"Downloading {archive.name} from {url} (about 27 MB), checked against SHA-256 {archive_sha256[:16]}…")
        partial = archive.with_name(archive.name + ".partial")
        partial.unlink(missing_ok=True)
        fetch(url, partial, NODE_MAX_BYTES)
        if sha256_of(partial) != archive_sha256:
            partial.unlink()
            raise HostRuntimeError(f"{archive.name} does not match its pinned SHA-256; nothing was installed")
        partial.replace(archive)
    unpacked = runtime.node_dir.with_name(runtime.node_dir.name + ".partial")
    shutil.rmtree(unpacked, ignore_errors=True)
    unpacked.mkdir(parents=True)
    try:
        _unpack(archive, unpacked)
    except (HostRuntimeError, tarfile.TarError, KeyError, OSError) as exc:
        shutil.rmtree(unpacked, ignore_errors=True)
        raise HostRuntimeError(f"the Node archive cannot be unpacked safely: {exc}") from None
    shutil.rmtree(runtime.node_dir, ignore_errors=True)
    unpacked.replace(runtime.node_dir)
    found = _node_version(runtime, run)
    if found != f"v{version}":
        raise HostRuntimeError(f"the unpacked Node reports {found or 'nothing'}, not v{version}")


def _unpack(archive: Path, target: Path) -> None:
    """Unpack the archive's one top folder into `target`: no absolute paths, no links out of the folder."""
    top = archive.name.removesuffix(".tar.xz")
    with tarfile.open(archive) as tar:
        members = []
        for member in tar.getmembers():
            path = Path(member.name)
            if path.parts[:1] != (top,):
                raise HostRuntimeError(f"it holds a file outside {top}/")
            member.name = str(Path(*path.parts[1:])) if len(path.parts) > 1 else "."
            members.append(member)
        tar.extractall(target, members=members, filter="data")


def _node_version(runtime: Runtime, run: Runner) -> str | None:
    if not runtime.node.exists():
        return None
    result = run([str(runtime.node), "--version"], env={"PATH": SYSTEM_PATH}, timeout=30)
    return result.stdout.strip() if result.returncode == 0 else None


def ensure_openclaw(runtime: Runtime, *, run: Runner = _run, say: Callable[[str], None] = print) -> None:
    """OpenClaw of the pinned version from the lockfile that ships with Core; npm runs no install scripts."""
    runtime.openclaw_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    changed = False
    for name in RUNTIME_FILES:
        source, target = packaged("runtime", name), runtime.openclaw_dir / name
        if not target.exists() or target.read_bytes() != source.read_bytes():
            shutil.copyfile(source, target)
            changed = True
    if not changed and _openclaw_version(runtime, run) == OPENCLAW_VERSION:
        return
    say(f"Installing OpenClaw {OPENCLAW_VERSION} with npm from its pinned lockfile (about 540 MB)…")
    env = {
        "PATH": f"{runtime.node.parent}:{SYSTEM_PATH}",
        "HOME": str(Path.home()),
        "npm_config_registry": "https://registry.npmjs.org/",
        "npm_config_update_notifier": "false",
    }
    result = run(
        [
            str(runtime.npm),
            "ci",
            "--omit=dev",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--prefix",
            str(runtime.openclaw_dir),
        ],
        env=env,
        timeout=1800,
    )
    if result.returncode != 0:
        raise HostRuntimeError(f"npm ci failed: {result.stderr.strip()[-500:]}")
    found = _openclaw_version(runtime, run)
    if found != OPENCLAW_VERSION:
        raise HostRuntimeError(f"the installed OpenClaw reports {found or 'nothing'}, not {OPENCLAW_VERSION}")


def _openclaw_version(runtime: Runtime, run: Runner) -> str | None:
    if not runtime.openclaw_entry.exists() or not runtime.node.exists():
        return None
    result = run([str(runtime.node), str(runtime.openclaw_entry), "--version"], env={"PATH": SYSTEM_PATH}, timeout=60)
    words = result.stdout.split()
    return words[1] if result.returncode == 0 and len(words) > 1 and words[0] == "OpenClaw" else None


def install_adapter(runtime: Runtime) -> bool:
    """Copy the adapter plugin that ships with Core into the runtime. True when any of its files changed: its code,
    its manifest or its package file, each of which a running gateway read at its start."""
    runtime.adapter_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    changed = False
    for name in ADAPTER_FILES:
        source, target = packaged("adapter", name), runtime.adapter_dir / name
        if not target.exists() or target.read_bytes() != source.read_bytes():
            write_private(target, source.read_bytes())
            changed = True
    return changed


def adapter_sha256() -> str:
    """The SHA-256 the running adapter must report: the code that ships with this Core."""
    return sha256_of(packaged("adapter", "index.ts"))


def write_wrapper(runtime: Runtime, layout: HostLayout) -> Path:
    """klepa-openclaw: OpenClaw as the engine runs it, for the owner's terminal. It never touches ~/.openclaw."""
    env = gateway_env(runtime, layout)
    keep = " ".join(f'{name}="${{{name}:-}}"' for name in ("TERM", "LANG", "LC_ALL"))
    lines = [
        "#!/bin/sh",
        "# OpenClaw as the Klepa engine runs it: its own Node, config and state, never ~/.openclaw.",
        f"exec /usr/bin/env -i {keep} \\",
        *(f"  {name}={shlex.quote(value)} \\" for name, value in env.items()),
        "  " + " ".join(shlex.quote(part) for part in openclaw_argv(runtime)) + ' "$@"',
        "",
    ]
    runtime.wrapper.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(runtime.wrapper, "\n".join(lines).encode("utf-8"))
    runtime.wrapper.chmod(0o700)
    return runtime.wrapper


def openclaw_json(
    runtime: Runtime, layout: HostLayout, *args: str, run: Runner = _run, timeout: float = 120.0
) -> dict[str, object]:
    """Run an OpenClaw command with --json in the engine's environment and return its answer. A command that fails
    raises, whatever it printed: OpenClaw prints its errors as JSON too."""
    try:
        result = run(openclaw_argv(runtime, *args, "--json"), env=gateway_env(runtime, layout), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostRuntimeError(f"openclaw {' '.join(args)} did not run: {exc}") from None
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        answer = None
    if result.returncode != 0 or not isinstance(answer, dict):
        detail = (result.stdout.strip() or result.stderr.strip())[-300:]
        raise HostRuntimeError(f"openclaw {' '.join(args)} failed: {detail}")
    return answer


def model_token_stored(runtime: Runtime, layout: HostLayout, *, run: Runner = _run) -> bool:
    """Whether the gateway's auth store holds the model's profile. Whether the token still works only a call shows."""
    try:
        answer = openclaw_json(runtime, layout, "models", "auth", "list", "--provider", "anthropic", run=run)
    except HostRuntimeError:
        return False
    profiles = answer.get("profiles")
    return isinstance(profiles, list) and any(
        isinstance(profile, dict) and profile.get("id") == MODEL_PROFILE for profile in profiles
    )


def validate_config(runtime: Runtime, layout: HostLayout, *, run: Runner = _run) -> None:
    answer = openclaw_json(runtime, layout, "config", "validate", run=run)
    if answer.get("valid") is not True:
        raise HostRuntimeError(f"OpenClaw refuses the rendered config: {json.dumps(answer)[:500]}")


def claim_ownership(runtime: Runtime, layout: HostLayout, *, run: Runner = _run) -> None:
    """Make Core the only manager of the gateway's state database (spec 4.6); repeating it is harmless."""
    answer = openclaw_json(runtime, layout, "database", "ownership", "claim", "--manager", OWNERSHIP_MANAGER, run=run)
    ownership = answer.get("ownership")
    if not isinstance(ownership, dict) or ownership.get("managerId") != OWNERSHIP_MANAGER:
        raise HostRuntimeError(f"OpenClaw refused the ownership claim: {str(answer.get('error', answer))[:300]}")


def write_private(path: Path, data: bytes) -> None:
    """Replace a file atomically, mode 0600, through a temporary file of its own: two writers never share one, and a
    failed write leaves the old file and no temporary one."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
