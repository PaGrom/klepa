import json
import os
import sqlite3
import stat
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from helpers import BASE_CONFIG
from klepa_core import __main__ as cli
from klepa_core import db
from klepa_core.app import acquire_lock
from klepa_core.host import install as host
from klepa_core.host import reference
from klepa_core.host import runtime as rt

TOKEN = "sk-ant-oat01-" + "x" * 80


@pytest.fixture
def cfg(make_config, short_dir, tmp_path):
    section = (
        f'\n[host]\nsocket = "{short_dir / "run" / "adapter.sock"}"\n'
        f'runtime_dir = "{tmp_path / "runtime"}"\negress_allow = ["api.anthropic.com:443"]\n'
    )
    return make_config(text=BASE_CONFIG + section)


class FakeOpenClaw:
    """node, npm and OpenClaw's commands as the install needs them."""

    def __init__(self, runtime: rt.Runtime, *, profiles=("anthropic:klepa",), gateway_runs=False, paste=None) -> None:
        self.runtime = runtime
        self.profiles = list(profiles)
        self.gateway_runs = gateway_runs
        self.paste = paste
        self.calls: list[tuple[list[str], dict | None, str | None]] = []

    def __call__(self, argv, *, env=None, timeout=600.0, input=None):
        argv = [str(part) for part in argv]
        self.calls.append((argv, env, input))
        tail = argv[2:]
        if argv[:2] == [str(self.runtime.node), "--version"]:
            return subprocess.CompletedProcess(argv, 0, "v24.21.0\n", "")
        if argv[0] == str(self.runtime.npm):
            self.runtime.openclaw_entry.parent.mkdir(parents=True, exist_ok=True)
            self.runtime.openclaw_entry.write_text("// openclaw\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if tail == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, "OpenClaw 2026.9.4 (3a9d69d)\n", "")
        if "validate" in tail:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"valid": True, "warnings": []}), "")
        if "ownership" in tail:
            answer = {"status": "external", "ownership": {"managerId": "klepa-core"}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")
        if "list" in tail:
            answer = {"profiles": [{"id": name, "type": "token"} for name in self.profiles]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")
        if "paste-token" in tail:
            if self.paste is not None:
                return subprocess.CompletedProcess(argv, 1, "", self.paste)
            self.profiles.append("anthropic:klepa")  # the real command stores the token, then rewrites the config,
            if (env or {}).get("OPENCLAW_CONFIG_READONLY") == "1":  # which a read-only config refuses
                return subprocess.CompletedProcess(argv, 1, "", "Config is externally managed")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "models.authStatus" in tail:
            if not self.gateway_runs:
                return subprocess.CompletedProcess(argv, 1, "", "gateway closed (1006): connect ECONNREFUSED")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"providers": []}), "")
        if argv[0] == "launchctl":
            return subprocess.CompletedProcess(argv, 113, "", "Could not find service")
        raise AssertionError(argv)


def installed_node(runtime: rt.Runtime) -> None:
    runtime.node.parent.mkdir(parents=True)
    runtime.node.write_text("#!/bin/sh\n")


def test_install_prepares_everything_core_needs_to_start_the_gateway(cfg):
    runtime = rt.Runtime(cfg.host.runtime_dir)
    installed_node(runtime)
    fake = FakeOpenClaw(runtime)
    excluded: list[Path] = []
    said: list[str] = []
    host.install(cfg, say=said.append, run=fake, exclude=lambda path: excluded.append(path))
    layout_dir = cfg.host_dir
    config = json.loads((layout_dir / "openclaw.json").read_text())
    assert config["channels"]["telegram"]["apiRoot"] == f"http://127.0.0.1:{cfg.host.api_port}"
    assert stat.S_IMODE((layout_dir / "openclaw.json").stat().st_mode) == 0o600
    assert cfg.host_token_path.exists()
    assert cfg.adapter_key_path.exists()
    assert runtime.wrapper.exists()
    assert (runtime.adapter_dir / "index.ts").exists()
    assert excluded == [layout_dir]
    verbs = [" ".join(call[0][4:7]) for call in fake.calls]  # node, openclaw.mjs, --profile klepa, then the command
    assert any("config validate" in verb for verb in verbs)
    assert any("database ownership claim" in verb for verb in verbs)
    assert not [line for line in said if line.startswith("warning")]


def test_install_warns_when_the_model_api_is_not_allowed(make_config, short_dir, tmp_path):
    section = f'\n[host]\nsocket = "{short_dir / "run" / "adapter.sock"}"\nruntime_dir = "{tmp_path / "rt"}"\n'
    cfg = make_config(text=BASE_CONFIG + section)
    runtime = rt.Runtime(cfg.host.runtime_dir)
    installed_node(runtime)
    said: list[str] = []
    host.install(cfg, say=said.append, run=FakeOpenClaw(runtime), exclude=lambda path: None)
    assert any("api.anthropic.com:443" in line for line in said)


def test_install_needs_core_to_run_the_gateway(make_config):
    with pytest.raises(rt.HostRuntimeError, match="launchd"):
        host.install(make_config())


def installed(cfg, **fake_args):
    runtime = rt.Runtime(cfg.host.runtime_dir)
    installed_node(runtime)
    fake = FakeOpenClaw(runtime, **fake_args)
    host.install(cfg, say=lambda line: None, run=fake, exclude=lambda path: None)
    return runtime, fake


def test_login_hands_the_token_over_on_stdin_and_the_config_stays_cores(cfg):
    runtime, fake = installed(cfg, profiles=())
    before = (cfg.host_dir / "openclaw.json").read_bytes()
    assert host.login(cfg, TOKEN + "\n", run=fake) is False  # no gateway runs: it reads the token at its start
    argv, env, given = next(call for call in fake.calls if "paste-token" in call[0])
    assert TOKEN not in " ".join(argv)
    assert given == TOKEN + "\n"
    assert env == rt.gateway_env(runtime, reference.layout_of(cfg))  # the engine's own: the config stays read-only
    assert argv[-2:] == ["--profile-id", "anthropic:klepa"]
    assert (cfg.host_dir / "openclaw.json").read_bytes() == before


def test_a_running_gateway_is_asked_to_take_the_new_token(cfg):
    _, fake = installed(cfg, gateway_runs=True)
    assert host.login(cfg, TOKEN, run=fake) is True
    argv = next(call[0] for call in fake.calls if "models.authStatus" in call[0])
    assert json.loads(argv[argv.index("--params") + 1]) == {"refresh": True, "agentId": "main"}


def test_a_token_the_terminal_wrapped_is_joined(cfg):
    _, fake = installed(cfg)
    host.login(cfg, TOKEN[:40] + "\n" + TOKEN[40:] + "\n", run=fake)
    assert next(call[2] for call in fake.calls if "paste-token" in call[0]) == TOKEN + "\n"


@pytest.mark.parametrize("token", ["", "hello", "sk-ant-oat01-too-short", "sk-ant-api03-" + "x" * 90])
def test_login_takes_only_a_setup_token(cfg, token):
    runtime = rt.Runtime(cfg.host.runtime_dir)
    fake = FakeOpenClaw(runtime)
    with pytest.raises(rt.HostRuntimeError, match="not a setup-token"):
        host.login(cfg, token, run=fake)
    assert fake.calls == []


def test_any_other_refusal_of_openclaw_is_reported(cfg):
    _, fake = installed(cfg, paste="Expected token starting with sk-ant-oat01-")
    with pytest.raises(rt.HostRuntimeError, match="did not store"):
        host.login(cfg, TOKEN, run=fake)


def test_status_shows_the_runtime_the_agent_the_model_and_cores_view(cfg):
    runtime = rt.Runtime(cfg.host.runtime_dir)
    installed_node(runtime)
    fake = FakeOpenClaw(runtime)
    host.install(cfg, say=lambda line: None, run=fake, exclude=lambda path: None)
    conn = db.connect(cfg.core_db_path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO event_log(at, kind, data) VALUES ('2026-10-04T10:00:00Z', 'host_state', ?)",
        (json.dumps({"state": "RUNNING", "reason": None}),),
    )
    conn.close()
    lines = host.status(cfg, run=fake, launchctl=fake)
    assert lines == [
        "runtime: Node v24.21.0, OpenClaw 2026.9.4",
        "gateway: not loaded (Core loads it when it runs)",
        "model token (anthropic:klepa): stored",
        "Core: not running; its last state was RUNNING since 2026-10-04T10:00:00Z",
    ]


def test_the_cli_reads_the_token_without_echo(cfg, monkeypatch, capsys, tmp_path):
    given = {}
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: TOKEN)
    monkeypatch.setattr(cli.host, "login", lambda config, token: given.update(token=token) or True)
    path = tmp_path / "config.toml"  # make_config wrote the config here
    assert cli.main(["host", "login", "--config", str(path)]) == 0
    assert given == {"token": TOKEN}
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert "uses it now" in out


def test_a_host_problem_ends_the_command_with_its_own_code(cfg, monkeypatch, capsys, tmp_path):
    def broken(config):
        raise rt.HostRuntimeError("the gateway is not installed: run klepa-core host install first")

    monkeypatch.setattr(cli.host, "status", broken)
    assert cli.main(["host", "status", "--config", str(tmp_path / "config.toml")]) == 6
    assert "host install" in capsys.readouterr().err


def test_cores_view_says_whether_core_runs(cfg):
    """The event log keeps the supervisor's last state after Core stopped: only Core's lock tells whether it runs."""
    conn = db.connect(cfg.core_db_path)
    db.migrate(conn)
    assert host._core_view(cfg) == "not running; has not supervised a gateway yet"
    conn.execute(
        "INSERT INTO event_log(at, kind, data) VALUES ('2026-10-04T10:00:00Z', 'host_state', ?)",
        (json.dumps({"state": "STOPPED", "reason": "no_heartbeat"}),),
    )
    conn.close()
    assert host._core_view(cfg) == "not running; its last state was STOPPED (no_heartbeat) since 2026-10-04T10:00:00Z"
    lock = acquire_lock(cfg.data_dir / "core.lock")
    try:
        assert host._core_view(cfg) == "STOPPED (no_heartbeat) since 2026-10-04T10:00:00Z"
    finally:
        os.close(lock)


def test_core_view_without_a_database(cfg):
    assert host._core_view(cfg) == "no database yet"
    with closing(sqlite3.connect(cfg.core_db_path)):
        pass
    assert host._core_view(cfg).startswith("unreadable")


def test_uninstall_unloads_the_gateway_but_not_while_core_runs(cfg):
    _, fake = installed(cfg)
    (cfg.host_dir / "klepa.gateway.plist").write_text("x")
    (cfg.host_dir / "applied.sha256").write_text("x")
    lock = acquire_lock(cfg.data_dir / "core.lock")  # Core runs
    try:
        with pytest.raises(rt.HostRuntimeError, match="Core is running"):
            host.uninstall(cfg, launchctl=fake)
    finally:
        os.close(lock)
    assert not [call for call in fake.calls if call[0][:2] == ["launchctl", "bootout"]]
    host.uninstall(cfg, launchctl=fake)
    assert fake.calls[-1][0] == ["launchctl", "bootout", f"gui/{os.getuid()}/klepa.gateway"]
    assert not (cfg.host_dir / "klepa.gateway.plist").exists()
    assert not (cfg.host_dir / "applied.sha256").exists()
