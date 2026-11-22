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
from klepa_core.host.peers import probe_peer, stand_in

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
        self.owned = False  # OpenClaw 2026.10: a running gateway owns the state, and token commands refuse
        self.loaded = True  # whether launchd has Core's gateway agent to boot out
        self.busy = 0  # commands answered "busy" while another offline command holds the state
        self.claimed: str | None = None  # the state's manager, once claimed
        self.doctor = (0, "Upgraded agent database schema: v19 -> v27.\n")  # what `doctor --fix` exits and prints

    def __call__(self, argv, *, env=None, timeout=600.0, input=None):
        argv = [str(part) for part in argv]
        self.calls.append((argv, env, input))
        tail = argv[2:]
        if argv[:2] == [str(self.runtime.node), "--version"]:
            return subprocess.CompletedProcess(argv, 0, "v24.22.3\n", "")
        if argv[0] == str(self.runtime.npm):
            self.runtime.openclaw_entry.parent.mkdir(parents=True, exist_ok=True)
            self.runtime.openclaw_entry.write_text("// openclaw\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if tail == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, "OpenClaw 2026.10.33 (3a9d69d)\n", "")
        if "validate" in tail:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"valid": True, "warnings": []}), "")
        if "doctor" in tail:
            if self.owned:  # Doctor's repairs need the state, which a running gateway owns
                refusal = "operation requires exclusive offline state ownership. Inspect openclaw gateway status."
                return subprocess.CompletedProcess(argv, 1, "", refusal)
            return subprocess.CompletedProcess(argv, self.doctor[0], self.doctor[1], "")
        if "ownership" in tail:
            command = tail[tail.index("ownership") + 1]
            if command not in ("status", "claim"):  # OpenClaw knows only these two; `ownership --json` is refused
                refusal = {"ok": False, "error": {"message": f'OpenClaw does not recognize option "{command}".'}}
                return subprocess.CompletedProcess(argv, 0, json.dumps(refusal), "")
            if "claim" in tail:
                if self.owned:  # OpenClaw 2026.10 refuses a claim while a gateway runs
                    refusal = {"error": "Cannot claim shared-state ownership while a Gateway is running"}
                    return subprocess.CompletedProcess(argv, 1, json.dumps(refusal), "")
                self.claimed = "klepa-core"
            answer = {"status": "external", "ownership": {"managerId": self.claimed} if self.claimed else None}
            return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")
        if self.owned and ("list" in tail or "paste-token" in tail):
            refusal = {
                "error": "operation requires exclusive offline state ownership. Inspect openclaw gateway status."
            }
            return subprocess.CompletedProcess(argv, 1, json.dumps(refusal), "")
        if self.busy and ("list" in tail or "paste-token" in tail):
            self.busy -= 1
            return subprocess.CompletedProcess(argv, 1, json.dumps({"error": "OpenClaw state database is busy"}), "")
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
            if argv[1] == "bootout" and self.loaded:
                self.owned = self.loaded = False  # the gateway is gone, and with it the state's owner
                return subprocess.CompletedProcess(argv, 0, "", "")
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
    assert host.login(cfg, TOKEN + "\n", run=fake) == "later"  # no gateway runs: it reads the token at its start
    argv, env, given = next(call for call in fake.calls if "paste-token" in call[0])
    assert TOKEN not in " ".join(argv)
    assert given == TOKEN + "\n"
    assert env == rt.gateway_env(runtime, reference.layout_of(cfg))  # the engine's own: the config stays read-only
    assert argv[-2:] == ["--profile-id", "anthropic:klepa"]
    assert (cfg.host_dir / "openclaw.json").read_bytes() == before


def test_a_running_gateway_is_asked_to_take_the_new_token(cfg):
    _, fake = installed(cfg, gateway_runs=True)
    assert host.login(cfg, TOKEN, run=fake) == "now"
    argv = next(call[0] for call in fake.calls if "models.authStatus" in call[0])
    assert json.loads(argv[argv.index("--params") + 1]) == {"refresh": True, "agentId": "main"}


def test_a_token_the_terminal_wrapped_is_joined(cfg):
    _, fake = installed(cfg)
    host.login(cfg, TOKEN[:40] + "\n" + TOKEN[40:] + "\n", run=fake)
    assert next(call[2] for call in fake.calls if "paste-token" in call[0]) == TOKEN + "\n"


def test_a_token_copied_with_its_frame_is_taken_out_of_it(cfg):
    _, fake = installed(cfg)
    framed = "\u2502 " + TOKEN[:40] + " \u2502\n\u2502 " + TOKEN[40:] + " \u2502\n"
    host.login(cfg, framed, run=fake)
    assert next(call[2] for call in fake.calls if "paste-token" in call[0]) == TOKEN + "\n"


@pytest.mark.parametrize("extra", ['"', "\u2026", "?", "Your token: "])
def test_a_paste_with_more_than_the_token_is_refused(cfg, extra):
    _, fake = installed(cfg)
    calls = len(fake.calls)
    with pytest.raises(rt.HostRuntimeError, match="only the token"):
        host.login(cfg, extra + TOKEN, run=fake)
    assert len(fake.calls) == calls


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
        "runtime: Node v24.22.3, OpenClaw 2026.10.33",
        "gateway: not loaded (Core loads it when it runs)",
        "model token (anthropic:klepa): stored",
        "Core: not running; its last state was RUNNING since 2026-10-04T10:00:00Z",
    ]


def test_the_cli_reads_the_token_without_echo(cfg, monkeypatch, capsys, tmp_path):
    given = {}
    monkeypatch.setattr(cli, "_read_token", lambda: TOKEN)
    monkeypatch.setattr(cli.host, "login", lambda config, token: given.update(token=token) or "now")
    path = tmp_path / "config.toml"  # make_config wrote the config here
    assert cli.main(["host", "login", "--config", str(path)]) == 0
    assert given == {"token": TOKEN}
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert "uses it now" in out
    assert f"{len(TOKEN)} characters" in out  # what the owner can check against the token they copied


def test_a_token_from_a_pipe_is_read_whole(cfg, monkeypatch, capsys, tmp_path):
    """pbpaste | klepa-core host login …: the token never appears on a screen or in a prompt."""
    import io

    given = {}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(TOKEN[:40] + "\n" + TOKEN[40:] + "\n"))
    monkeypatch.setattr(cli.host, "login", lambda config, token: given.update(token=token) or "later")
    assert cli.main(["host", "login", "--config", str(tmp_path / "config.toml")]) == 0
    assert "".join(given["token"].split()) == TOKEN


def test_a_paste_on_a_terminal_is_read_whole_and_not_shown(monkeypatch):
    """A terminal or the display that printed the token can break it into lines; getpass took the first line only,
    stored a token cut short, and the rest of the paste ran as a command in the shell."""
    import pty
    import threading
    import time

    master, slave = pty.openpty()
    monkeypatch.setattr(cli.sys, "stdin", os.fdopen(slave, "r"))
    paste = (TOKEN[:50] + "\n" + TOKEN[50:] + "\n").encode()
    threading.Thread(target=lambda: (time.sleep(0.3), os.write(master, paste)), daemon=True).start()
    assert "".join(cli._read_token().split()) == TOKEN
    os.set_blocking(master, False)
    try:
        shown = os.read(master, 4096)
    except BlockingIOError:
        shown = b""
    assert TOKEN[:20].encode() not in shown
    assert TOKEN[50:70].encode() not in shown
    os.close(master)


def test_a_line_typed_later_is_not_part_of_the_paste():
    read, write = os.pipe()
    os.write(write, b"sk-ant-oat01-first\n")
    assert cli.read_paste(read, settle=0.05) == "sk-ant-oat01-first\n"
    os.write(write, b"later\n")
    os.close(write)
    assert os.read(read, 100) == b"later\n"
    os.close(read)


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


def test_allowed_lists_the_ids_a_gateway_admits_and_whose_each_is(cfg, capsys, tmp_path):
    """Review 1's M11: a gateway somebody else runs admits people by stand-ins, which only Core can work out."""
    assert cli.main(["host", "allowed", "--config", str(tmp_path / "config.toml")]) == 0
    out = capsys.readouterr().out
    key = cfg.adapter_key_path.read_bytes()
    listed = [line.split(maxsplit=1) for line in out.splitlines() if line.startswith("  ")]
    expected = [*(stand_in(key, member.person_id) for member in cfg.members), probe_peer(key)]
    assert [int(peer) for peer, _ in listed] == expected
    assert [whose for _, whose in listed] == [*(member.person_id for member in cfg.members), "the live probe"]
    assert not [member for member in cfg.members if str(member.telegram_id) in out]  # never a Telegram id


def test_a_running_gateway_is_stopped_for_the_token_and_core_told_why(cfg):
    """OpenClaw 2026.10 stores a token only while no gateway owns its state: login stops Core's gateway, stores the
    token, and leaves a mark, so that Core starts the gateway again without an alert."""
    _, fake = installed(cfg)
    fake.owned, fake.busy = True, 2  # and Core's own start may hold the state for a moment
    assert host.login(cfg, TOKEN, run=fake) == "restarted"
    verbs = [call[0][1] for call in fake.calls if call[0][0] == "launchctl"]
    assert verbs == ["bootout"]
    assert (cfg.host_dir / "login.pending").read_text().startswith("stopped")
    assert "anthropic:klepa" in fake.profiles


def test_a_state_another_openclaw_owns_is_named_and_nothing_is_stopped(cfg):
    _, fake = installed(cfg)
    fake.owned, fake.loaded = True, False  # not Core's gateway: an openclaw run by hand, say
    with pytest.raises(rt.HostRuntimeError, match="another OpenClaw"):
        host.login(cfg, TOKEN, run=fake)
    assert not (cfg.host_dir / "login.pending").exists()


def test_a_token_that_fails_after_the_stop_still_lets_core_start_the_gateway(cfg):
    """The gateway is down already: Core starts it again with the token it had, quietly."""
    _, fake = installed(cfg)
    fake.owned = True
    fake.paste = "Error: token rejected"
    with pytest.raises(rt.HostRuntimeError, match="did not store"):
        host.login(cfg, TOKEN, run=fake)
    assert (cfg.host_dir / "login.pending").read_text().startswith("stopped")


def test_installing_again_while_the_gateway_runs_keeps_cores_claim(cfg):
    """OpenClaw 2026.10 refuses an ownership claim while a gateway runs: a state Core already manages needs none."""
    runtime = rt.Runtime(cfg.host.runtime_dir)
    installed_node(runtime)
    fake = FakeOpenClaw(runtime)
    host.install(cfg, say=lambda line: None, run=fake, exclude=lambda path: None)
    fake.owned = True  # Core runs its gateway now
    host.install(cfg, say=lambda line: None, run=fake, exclude=lambda path: None)
    claims = [call for call in fake.calls if "claim" in call[0]]
    assert len(claims) == 1


def doctor_calls(fake):
    return [call for call in fake.calls if "doctor" in call[0]]


def older_state(cfg, version="2026.9.4"):
    """A state an older OpenClaw left: its agent database, and the version Core last made it ready for."""
    folder = cfg.host_dir
    for part in ("state", "agents", "main", "agent"):
        folder = folder / part
        folder.mkdir(mode=0o700, exist_ok=True)
    if version is not None:
        (cfg.host_dir / "state.version").write_text(version + "\n")


def test_a_new_state_is_marked_ready_without_doctor(cfg):
    _, fake = installed(cfg)
    assert (cfg.host_dir / "state.version").read_text().strip() == rt.OPENCLAW_VERSION
    assert not doctor_calls(fake)


@pytest.mark.parametrize("version", ["2026.9.4", None])
def test_an_older_state_is_migrated_by_doctor_with_the_gateway_stopped(cfg, version):
    cfg.host_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    older_state(cfg, version)
    runtime = rt.Runtime(cfg.host.runtime_dir)
    installed_node(runtime)
    fake = FakeOpenClaw(runtime)
    fake.claimed = "klepa-core"  # the older install claimed the state
    fake.owned = True  # and its gateway still runs
    said: list[str] = []
    host.install(cfg, say=said.append, run=fake, exclude=lambda path: None)
    [doctor] = doctor_calls(fake)
    assert doctor[0][-3:] == ["doctor", "--fix", "--non-interactive"]
    assert doctor[1]["OPENCLAW_STATE_DIR"] == str(cfg.host_dir / "state")
    order = [
        " ".join(call[0][:2]) if call[0][0] == "launchctl" else "doctor"
        for call in fake.calls
        if call[0][0] == "launchctl" or "doctor" in call[0]
    ]
    assert order == ["launchctl bootout", "doctor"]
    log = cfg.host_dir / "logs" / f"doctor-{rt.OPENCLAW_VERSION}.log"
    assert "v19 -> v27" in log.read_text()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert (cfg.host_dir / "state.version").read_text().strip() == rt.OPENCLAW_VERSION
    assert any(rt.OPENCLAW_VERSION in line for line in said)


def test_a_migration_needs_core_stopped(cfg):
    cfg.host_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    older_state(cfg)
    runtime = rt.Runtime(cfg.host.runtime_dir)
    installed_node(runtime)
    fake = FakeOpenClaw(runtime)
    lock = acquire_lock(cfg.data_dir / "core.lock")
    try:
        with pytest.raises(rt.HostRuntimeError, match="service uninstall"):
            host.install(cfg, say=lambda line: None, run=fake, exclude=lambda path: None)
    finally:
        os.close(lock)
    assert not doctor_calls(fake)
    assert not [call for call in fake.calls if call[0][0] == "launchctl"]
    assert (cfg.host_dir / "state.version").read_text().strip() == "2026.9.4"


def test_a_failed_migration_leaves_the_state_unmarked_and_names_its_log(cfg):
    cfg.host_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    older_state(cfg)
    runtime = rt.Runtime(cfg.host.runtime_dir)
    installed_node(runtime)
    fake = FakeOpenClaw(runtime)
    fake.doctor = (1, "Doctor could not repair the agent database\n")
    with pytest.raises(rt.HostRuntimeError, match=f"doctor-{rt.OPENCLAW_VERSION}.log"):
        host.install(cfg, say=lambda line: None, run=fake, exclude=lambda path: None)
    assert (cfg.host_dir / "state.version").read_text().strip() == "2026.9.4"
    assert "could not repair" in (cfg.host_dir / "logs" / f"doctor-{rt.OPENCLAW_VERSION}.log").read_text()


def test_installing_again_on_a_ready_state_runs_no_doctor_and_stops_nothing(cfg):
    _, fake = installed(cfg)
    older_state(cfg, rt.OPENCLAW_VERSION)
    fake.owned = True  # Core runs its gateway now
    host.install(cfg, say=lambda line: None, run=fake, exclude=lambda path: None)
    assert not doctor_calls(fake)
    assert not [call for call in fake.calls if call[0][0] == "launchctl"]
