"""Acceptance scenarios of stage 1c against the REAL OpenClaw gateway, run by Core under launchd (spec 13.2:
scenarios 16, 40, 41, 43; docs/architecture.md: Testing).

They need an installed runtime and run only when KLEPA_TEST_RUNTIME names it (the folder `host install` fills,
for example "$HOME/Library/Application Support/Klepa/openclaw"); CI has none. Every test loads its own launchd
agent under a label of its own, from a plist in the test's folder, and unloads it at the end. The runtime is only
read: the test's own runtime folder links to its Node and OpenClaw. The model's token is a dummy and the egress
proxy lets no model call out; stage 1 blocks every turn before the model anyway.
"""

import asyncio
import contextlib
import dataclasses
import json
import os
import secrets
import signal
import subprocess
from pathlib import Path

import pytest

from helpers import BASE_CONFIG, MEMBER, OWNER, free_port, query, wait_until, with_service_bot
from klepa_core import db
from klepa_core.app import init_layout, run_service
from klepa_core.host import gateway as gw
from klepa_core.host import install as host
from klepa_core.host import reference
from klepa_core.host import runtime as rt

RUNTIME = os.environ.get("KLEPA_TEST_RUNTIME")
pytestmark = pytest.mark.skipif(not RUNTIME, reason="needs KLEPA_TEST_RUNTIME: an installed host runtime")
STAGE1 = "For now I only accept files: documents, photos and voice messages."
NO_COMMANDS = "I have no commands. Just write to me in words."
DUMMY_TOKEN = "sk-ant-oat01-TEST-ONLY-NOT-A-TOKEN-" + "x" * 64


@dataclasses.dataclass
class Live:
    cfg: object
    telegram: object
    alerts: object
    core: asyncio.Task
    stop: asyncio.Event
    label: str

    def states(self):
        rows = query(self.cfg, "SELECT data FROM event_log WHERE kind='host_state' ORDER BY id")
        return [json.loads(row["data"]) for row in rows]

    def state(self):
        states = self.states()
        return states[-1]["state"] if states else None

    def agent(self) -> gw.AgentState:
        return gw.parse_print(gw._run(["launchctl", "print", f"gui/{os.getuid()}/{self.label}"]))

    def sent(self):
        return [item["params"].get("text", "") for item in self.telegram.sent]


def linked_runtime(folder: Path) -> rt.Runtime:
    """A runtime of the test's own that reads the installed Node and OpenClaw."""
    real = rt.Runtime(Path(RUNTIME))
    runtime = rt.Runtime(folder)
    folder.mkdir(mode=0o700)
    for path in (real.node_dir, real.openclaw_dir):
        (folder / path.name).symlink_to(path)
    return runtime


@contextlib.asynccontextmanager
async def live_stand(make_config, install, fake_tg, service_tg, short_dir, monkeypatch, *, table_change=None):
    label = f"klepa.gateway.test-{secrets.token_hex(4)}"
    monkeypatch.setattr(gw, "LABEL", label)
    monkeypatch.setattr(gw, "_run", rt._run)  # the real launchctl, for this agent only
    runtime = linked_runtime(install["tmp"] / "runtime")
    section = (
        f'\n[host]\nsocket = "{short_dir / "run" / "adapter.sock"}"\nruntime_dir = "{runtime.root}"\n'
        f"api_port = {free_port()}\nproxy_port = {free_port()}\ngateway_port = {free_port()}\n"
    )
    text = with_service_bot(BASE_CONFIG, install["service_token_file"], service_tg.url) + section
    cfg = make_config(api_root=fake_tg.url, text=text)
    init_layout(cfg)
    conn = db.connect(cfg.core_db_path)
    conn.execute("INSERT INTO service_binding(person_id, chat_id, bound_at) VALUES ('owner', ?, 't')", (OWNER,))
    conn.close()
    if table_change is not None:
        monkeypatch.setattr(reference, "FIXED", reference.FIXED | table_change)
    host.install(cfg, say=lambda line: None, exclude=lambda path: None)
    host.login(cfg, DUMMY_TOKEN)
    stop = asyncio.Event()
    core = asyncio.create_task(run_service(cfg, stop, copy_interval=0.2, retry_seconds=1.0, documents_grace=0.0))
    current = Live(cfg, fake_tg, service_tg, core, stop, label)
    try:
        yield current
    finally:
        current.stop.set()  # a test may have started Core again
        await asyncio.wait_for(current.core, 30)
        rt._run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        await wait_until(lambda: not current.agent().loaded, timeout=30)  # the gateway drains for a few seconds


@pytest.fixture
def stand_args(make_config, install, fake_tg, service_tg, short_dir, monkeypatch):
    return make_config, install, fake_tg, service_tg, short_dir, monkeypatch


async def test_the_real_gateway_passes_the_gate_and_answers_with_cores_text(stand_args):
    async with live_stand(*stand_args) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        assert live.agent().process is not None
        live.telegram.add_text(MEMBER, "hello")
        await wait_until(lambda: STAGE1 in live.sent(), timeout=40)
        assert not [text for text in live.sent() if "could not be sent" in text]
        assert [alert for alert in live.alerts.sent if "host" in alert["params"]["text"]] == []


async def test_a_crashed_gateway_is_checked_again_before_it_gets_messages(stand_args):
    """Scenario 41: launchd brings a killed gateway back; Core sees a new process and gates it again."""
    async with live_stand(*stand_args) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        before = live.agent().process
        os.kill(before.pid, signal.SIGKILL)
        await wait_until(lambda: live.state() == "STARTING", timeout=20)
        assert live.states()[-1]["reason"] in ("new_process", "new_boot")
        live.telegram.add_text(MEMBER, "while it restarts")
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        assert live.agent().process.pid != before.pid
        await wait_until(lambda: STAGE1 in live.sent(), timeout=40)
        assert live.sent().count(STAGE1) == 1


async def test_a_gateway_unloaded_behind_cores_back_comes_back_with_an_alert(stand_args):
    async with live_stand(*stand_args) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        rt._run(["launchctl", "bootout", f"gui/{os.getuid()}/{live.label}"])
        await wait_until(lambda: any("stopped by itself" in a["params"]["text"] for a in live.alerts.sent), timeout=30)
        await wait_until(lambda: live.state() == "RUNNING" and live.agent().process is not None, timeout=90)


async def test_without_conversation_access_the_gateway_never_gets_messages(stand_args):
    """Scenario 16, a plugin loaded without allowConversationAccess: OpenClaw drops the turn hooks, Core sees it in
    the gateway's log and stops the gateway."""
    change = {"plugins.entries.klepa-adapter.hooks.allowConversationAccess": False}
    async with live_stand(*stand_args, table_change=change) as live:
        await wait_until(lambda: live.state() == "STOPPED", timeout=120)
        reason = live.states()[-1]["reason"]
        assert "blocked" in reason or reason in (
            "probe",
            "heartbeat:policy:plugins.entries.klepa-adapter.hooks.allowConversationAccess",
        )
        live.telegram.add_text(MEMBER, "anyone there?")
        await asyncio.sleep(3)
        assert STAGE1 not in live.sent()
        assert not live.agent().loaded


async def test_the_model_has_no_tools(stand_args):
    """Scenario 40: `sessions_history`, `session_status`, `x_search`, `view_image`, `browser`, `message` and `exec`
    are not there for the model, by the gateway's own account: in stage 1 it has no tool at all."""
    async with live_stand(*stand_args) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        cfg = live.cfg
        runtime, layout = rt.Runtime(cfg.host.runtime_dir), reference.layout_of(cfg)
        key = cfg.adapter_key_path.read_bytes()
        from klepa_core.host.adapter import probe_peer

        session = f"agent:main:telegram:direct:{probe_peer(key)}"  # the probe's session exists after the gate
        params = json.dumps({"sessionKey": session})
        answer = rt.openclaw_json(runtime, layout, "gateway", "call", "tools.effective", "--params", params)
        assert answer["profile"] == "minimal"
        assert answer["groups"] == []  # stage 1 gives the model no tool at all, so none of spec 13.2's either


async def test_core_restarts_while_its_gateway_runs(stand_args):
    """Core goes away for a minute and comes back. Its gateway kept running, polling a gatekeeper that was gone; it
    passes the start gate again within the gate's budget, without a restart of its own, and answers."""
    async with live_stand(*stand_args) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        process = live.agent().process
        live.stop.set()
        await asyncio.wait_for(live.core, 30)
        await asyncio.sleep(60)  # the host's poller backs off against a gatekeeper that is gone
        seen = len(live.states())
        live.stop = asyncio.Event()
        live.core = asyncio.create_task(
            run_service(live.cfg, live.stop, copy_interval=0.2, retry_seconds=1.0, documents_grace=0.0)
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        await wait_until(lambda: any(s["state"] == "RUNNING" for s in live.states()[seen:]), timeout=90)
        assert [s["state"] for s in live.states()[seen:]] == ["RUNNING"]  # no failed gate on the way
        assert loop.time() - started < 30
        assert live.agent().process == process
        live.telegram.add_text(MEMBER, "after Core came back")
        await wait_until(lambda: STAGE1 in live.sent(), timeout=40)


async def test_a_members_command_never_reaches_the_host(stand_args):
    """Scenario 43: Core answers commands itself; the gateway never sees them."""
    async with live_stand(*stand_args) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        live.telegram.add_text(MEMBER, "/model")
        await wait_until(lambda: NO_COMMANDS in live.sent(), timeout=30)
        log = (live.cfg.host_dir / "logs" / "gateway.log").read_text()
        assert "/model" not in log


async def test_a_new_token_reaches_the_running_gateway(stand_args):
    """The owner logs in again while Core runs the gateway: the token is stored, the config stays Core's, and the
    running gateway is asked to take it at once."""
    async with live_stand(*stand_args) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        config = (live.cfg.host_dir / "openclaw.json").read_bytes()
        assert await asyncio.to_thread(host.login, live.cfg, DUMMY_TOKEN.replace("x", "y")) is True
        assert (live.cfg.host_dir / "openclaw.json").read_bytes() == config
        assert live.state() == "RUNNING"


async def test_an_unmarked_openclaw_cannot_write_the_gateways_state(stand_args):
    """The ownership claim (spec 4.6): another OpenClaw process pointed at the engine's state, without the external
    supervision mark, is refused before it writes; the engine's own wrapper is not."""
    async with live_stand(*stand_args) as live:
        cfg = live.cfg
        runtime, layout = rt.Runtime(cfg.host.runtime_dir), reference.layout_of(cfg)
        env = rt.gateway_env(runtime, layout)
        del env["OPENCLAW_SUPERVISOR_MODE"]
        argv = rt.openclaw_argv(runtime, "models", "auth", "paste-token", "--provider", "anthropic")
        result = rt._run([*argv, "--profile-id", "anthropic:intruder"], env=env, input=DUMMY_TOKEN + "\n")
        assert result.returncode != 0
        listed = rt.openclaw_json(runtime, layout, "models", "auth", "list", "--provider", "anthropic")
        assert [profile["id"] for profile in listed["profiles"]] == ["anthropic:klepa"]


def test_the_live_tests_leave_no_agent_behind():
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    assert "klepa.gateway.test-" not in out
