import json
import plistlib
import stat
import subprocess
from pathlib import Path

import pytest
from aiohttp import web

from helpers import free_port
from klepa_core.host import gateway as gw
from klepa_core.host import runtime as rt
from klepa_core.host.reference import HostLayout
from klepa_core.host.supervisor import GatewayUnknown

PRINT_RUNNING = """gui/501/klepa.gateway = {
\tactive count = 1
\tpath = /data/host/klepa.gateway.plist
\tstate = running
\tprogram = /runtime/node
\truns = 3
\tpid = 4242
\tlast exit code = 0
\tendpoints = {
\t\tstate = active
\t\tpid = 1
\t}
}
"""
PRINT_EXITED = PRINT_RUNNING.replace("\tstate = running", "\tstate = not running").replace("\tpid = 4242\n", "")


def done(argv, code=0, out="", err=""):
    return subprocess.CompletedProcess(argv, code, out, err)


def test_launchctl_print_is_read_from_the_agents_own_lines():
    state = gw.parse_print(done([], 0, PRINT_RUNNING))
    expected = gw.AgentState(loaded=True, process=gw.GatewayProcess(4242, 3), last_exit=0, runs=3, state="running")
    assert state == expected
    exited = gw.AgentState(loaded=True, last_exit=0, runs=3, state="not running")
    assert gw.parse_print(done([], 0, PRINT_EXITED)) == exited
    killed = PRINT_EXITED.replace("\tlast exit code = 0\n", "\tlast terminating signal = Killed: 9\n")
    assert gw.parse_print(done([], 0, killed)).crashed
    missing = done([], 113, "", 'Could not find service "klepa.gateway" in domain for user gui: 501')
    assert gw.parse_print(missing) == gw.AgentState(loaded=False)


@pytest.mark.parametrize(
    "answer",
    [done([], 5, "", "Input/output error"), done([], 0, PRINT_RUNNING.replace("\tpid = 4242\n", ""))],
)
def test_an_unclear_launchd_answer_is_unknown_never_a_stopped_gateway(answer):
    with pytest.raises(GatewayUnknown):
        gw.parse_print(answer)


class FakeLaunchd:
    """launchctl as Core uses it, for one agent."""

    def __init__(self, *, refuse_bootstrap: int = 0) -> None:
        self.loaded = False
        self.runs = 0
        self.refuse = refuse_bootstrap
        self.calls: list[list[str]] = []
        self.plists: list[dict] = []

    def __call__(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        verb = argv[1]
        if verb == "print":
            if not self.loaded:
                return done(argv, 113, "", "Could not find service")
            return done(argv, 0, PRINT_RUNNING.replace("runs = 3", f"runs = {self.runs}"))
        if verb == "bootstrap":
            if self.refuse:
                self.refuse -= 1
                return done(argv, 5, "", "Bootstrap failed: 5: Input/output error")
            self.plists.append(plistlib.loads(Path(argv[3]).read_bytes()))
            self.loaded, self.runs = True, self.runs + 1
            return done(argv)
        if verb == "bootout":
            self.loaded = False
            return done(argv)
        raise AssertionError(argv)

    def verbs(self):
        return [call[1] for call in self.calls if call[1] != "print"]


@pytest.fixture
def setup(tmp_path):
    runtime = rt.Runtime(tmp_path / "runtime")
    runtime.openclaw_entry.parent.mkdir(parents=True)
    runtime.openclaw_entry.write_text("// openclaw\n")
    layout = HostLayout(runtime_dir=runtime.root, host_dir=tmp_path / "data" / "host")
    layout.host_dir.parent.mkdir(mode=0o700)
    reference = {"gateway.port": 19300, "gateway.reload.mode": "off"}
    return runtime, layout, reference


def control(setup, launchd, *, validate=None, port=19300, ready_seconds=2.0, token=True):
    runtime, layout, reference = setup
    gateway = gw.LaunchdGateway(
        runtime,
        layout,
        dict(reference),
        port,
        run=launchd,
        uid=501,
        ready_seconds=ready_seconds,
        validate=validate or (lambda: None),
        model_access=lambda: token,
    )
    gateway.retry_seconds = 0.0
    return gateway


def test_preparing_writes_private_files_and_says_when_they_changed(setup):
    runtime, layout, reference = setup
    assert gw.prepare_host(runtime, layout, reference) is True
    token = layout.gateway_token.read_bytes()
    assert len(token) == 64
    for path in (layout.host_dir, layout.state_dir, layout.workspace, layout.logs_dir):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(layout.config_path.stat().st_mode) == 0o600
    assert json.loads(layout.config_path.read_text()) == {"gateway": {"port": 19300, "reload": {"mode": "off"}}}
    assert gw.prepare_host(runtime, layout, reference) is False
    assert layout.gateway_token.read_bytes() == token  # made once
    assert gw.prepare_host(runtime, layout, reference | {"gateway.port": 19400}) is True
    (runtime.adapter_dir / "index.ts").write_text("// someone else's code\n")
    assert gw.prepare_host(runtime, layout, reference | {"gateway.port": 19400}) is True
    assert (runtime.adapter_dir / "index.ts").read_bytes() == rt.packaged("adapter", "index.ts").read_bytes()


async def test_start_loads_the_agent_with_the_spec_settings(setup):
    launchd = FakeLaunchd()
    gateway = control(setup, launchd)
    assert await gateway.start() is None
    assert launchd.verbs() == ["bootstrap"]
    plist = launchd.plists[0]
    assert plist["Label"] == "klepa.gateway"
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["EnvironmentVariables"]["OPENCLAW_NO_RESPAWN"] == "1"
    assert plist["ProgramArguments"][-2:] == ["gateway", "run"]
    assert gateway.plist_path.parent == setup[1].host_dir  # not in ~/Library/LaunchAgents: only Core starts it
    assert await gateway.process() == gw.GatewayProcess(4242, 1)


async def test_a_running_gateway_with_current_files_is_left_alone(setup):
    launchd = FakeLaunchd()
    gateway = control(setup, launchd)
    await gateway.start()
    assert await gateway.start() is None
    assert launchd.verbs() == ["bootstrap"]
    gateway.reference["gateway.reload.mode"] = "hybrid"  # Core's table changed: the gateway must run the new one
    assert await gateway.start() is None
    assert launchd.verbs() == ["bootstrap", "bootout", "bootstrap"]
    moved = rt.Runtime(setup[0].root.parent / "runtime-new")  # a new Node or OpenClaw: the agent must name it
    moved.openclaw_entry.parent.mkdir(parents=True)
    moved.openclaw_entry.write_text("// openclaw\n")
    gateway.runtime = moved
    assert await gateway.start() is None
    assert launchd.verbs() == ["bootstrap", "bootout", "bootstrap", "bootout", "bootstrap"]


async def test_files_changed_behind_cores_back_are_loaded_at_the_next_start(setup):
    """`host install` rewrites the gateway's files while it runs; the next start of Core must load them, not keep
    the old gateway, whose heartbeat would fail the new checks."""
    runtime, layout, reference = setup
    launchd = FakeLaunchd()
    await control(setup, launchd).start()
    changed = reference | {"gateway.reload.mode": "hybrid"}
    gw.prepare_host(runtime, layout, changed)  # what host install does
    assert await control((runtime, layout, changed), launchd).start() is None  # a new Core with the new table
    assert launchd.verbs() == ["bootstrap", "bootout", "bootstrap"]


async def test_a_config_openclaw_refuses_is_never_started(setup):
    def refuse():
        raise rt.HostRuntimeError("OpenClaw refuses the rendered config: tools.profile")

    launchd = FakeLaunchd()
    assert (await control(setup, launchd, validate=refuse).start()).startswith("config: OpenClaw refuses")
    assert launchd.verbs() == []


async def test_a_missing_runtime_is_named(setup):
    runtime, _, _ = setup
    runtime.openclaw_entry.unlink()
    launchd = FakeLaunchd()
    assert "host install" in await control(setup, launchd).start()
    assert launchd.verbs() == []


async def test_bootstrap_is_tried_again_and_then_reported(setup):
    launchd = FakeLaunchd(refuse_bootstrap=2)
    assert await control(setup, launchd).start() is None
    assert launchd.verbs() == ["bootstrap"] * 3
    launchd = FakeLaunchd(refuse_bootstrap=99)
    assert (await control(setup, launchd).start()).startswith("launchctl bootstrap: Bootstrap failed: 5")


async def test_stop_unloads_the_agent(setup):
    launchd = FakeLaunchd()
    gateway = control(setup, launchd)
    await gateway.start()
    await gateway.stop()
    assert launchd.verbs()[-1] == "bootout"
    assert await gateway.process() is None


def log_line(*parts):
    record = {str(i): part for i, part in enumerate(parts)}
    record["_meta"] = {"logLevelName": "WARN"}
    return json.dumps(record) + "\n"


def test_a_refused_hook_registration_is_found_in_the_log_since_the_last_start(tmp_path):
    log = tmp_path / "gateway.log"
    blocked = (
        '[plugins] typed hook "before_agent_run" blocked because non-bundled plugins must set '
        "plugins.entries.klepa-adapter.hooks.allowConversationAccess=true (plugin=klepa-adapter, source=/x/index.ts)"
    )
    assert gw.hook_problem(log) is None  # no log yet
    log.write_text(
        log_line('{"subsystem":"gateway"}', "loading configuration…") + log_line('{"subsystem":"plugins"}', blocked)
    )
    assert gw.hook_problem(log) == 'typed hook "before_agent_run" blocked'
    with log.open("a") as handle:
        handle.write(log_line('{"subsystem":"gateway"}', "loading configuration…"))
        handle.write(log_line('{"subsystem":"gateway"}', "gateway ready"))
    assert gw.hook_problem(log) is None  # that was the previous start
    with log.open("a") as handle:
        handle.write(log_line('{"subsystem":"plugins"}', 'typed hook "x" blocked (plugin=someone-else, source=/y)'))
    assert gw.hook_problem(log) is None  # another plugin's problem
    with log.open("a") as handle:
        handle.write(log_line("{}", 'unknown typed hook "before_dispach" ignored (plugin=klepa-adapter, source=/x)'))
    assert gw.hook_problem(log) == "unknown typed hook"


async def health_server(port, startup, ready):
    async def startupz(request):
        return web.json_response({"status": startup}, status=200 if startup == "started" else 503)

    async def readyz(request):
        return web.json_response({"ready": ready}, status=200 if ready else 503)

    app = web.Application()
    app.router.add_get("/startupz", startupz)
    app.router.add_get("/readyz", readyz)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


@pytest.mark.parametrize(
    ("startup", "ready", "problem"),
    [("started", True, None), ("starting", False, "startupz"), ("started", False, "readyz")],
)
async def test_ready_asks_startupz_then_readyz(setup, startup, ready, problem):
    port = free_port()
    runner = await health_server(port, startup, ready)
    try:
        assert await control(setup, FakeLaunchd(), port=port, ready_seconds=0.6).ready() == problem
    finally:
        await runner.cleanup()


async def test_ready_reports_a_hook_openclaw_refused(setup):
    _, layout, _ = setup
    layout.logs_dir.mkdir(parents=True)
    layout.gateway_log.write_text(
        log_line("{}", "loading configuration…")
        + log_line("{}", 'typed hook "before_prompt_build" blocked by x (plugin=klepa-adapter, source=/x)')
    )
    port = free_port()
    runner = await health_server(port, "started", True)
    try:
        assert await control(setup, FakeLaunchd(), port=port).ready() == 'typed hook "before_prompt_build" blocked'
    finally:
        await runner.cleanup()


def test_unknown_launchctl_answers_do_not_crash_the_parser():
    odd = "gui/501/klepa.gateway = {\n\tpid = abc\n\truns = many\n\tlast exit code = (never exited)\n}\n"
    assert gw.parse_print(done([], 0, odd)) == gw.AgentState(loaded=True)


async def test_a_crashed_gateway_is_a_new_process_and_a_clean_exit_is_none(setup):
    class Printer:
        def __init__(self, text):
            self.text = text

        def __call__(self, argv, **kwargs):
            return done(argv, 0, self.text)

    killed = PRINT_EXITED.replace("\tlast exit code = 0\n", "\tlast terminating signal = Killed: 9\n")
    assert await control(setup, Printer(killed)).process() == gw.GatewayProcess(0, 3)
    failed = PRINT_EXITED.replace("last exit code = 0", "last exit code = 1")
    assert await control(setup, Printer(failed)).process() == gw.GatewayProcess(0, 3)
    assert await control(setup, Printer(PRINT_EXITED)).process() is None


async def test_ready_names_a_missing_model_token(setup):
    port = free_port()
    runner = await health_server(port, "started", True)
    try:
        problem = await control(setup, FakeLaunchd(), port=port, token=False).ready()
    finally:
        await runner.cleanup()
    assert problem == "no model token: run klepa-core host login"


def test_preparing_keeps_the_folder_out_of_time_machine_every_time(setup):
    runtime, layout, reference = setup
    excluded: list[Path] = []
    warnings: list[str] = []

    def exclude(path):
        excluded.append(path)
        return "tmutil refused"

    for _ in range(2):
        gw.prepare_host(runtime, layout, reference, exclude=exclude, warn=warnings.append)
    assert excluded == [layout.host_dir, layout.host_dir]
    assert warnings == ["the gateway's folder is not excluded from Time Machine: tmutil refused"] * 2
    assert stat.S_IMODE(layout.home.stat().st_mode) == 0o700
