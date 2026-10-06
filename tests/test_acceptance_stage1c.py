"""Acceptance scenarios for stage 1c, Core's side (docs/architecture.md: Testing).

The host is the fake OpenClaw of tests/fakehost.py with the fake adapter of tests/fakeadapter.py. Plan 1c-2 runs
the same scenarios against the real gateway.
"""

import asyncio
import contextlib
import dataclasses
import json

import aiohttp
import pytest

from fakeadapter import FakeAdapter, session_key
from fakehost import FakeHost
from helpers import (
    BASE_CONFIG,
    MEMBER,
    OWNER,
    free_port,
    query,
    sent_texts,
    wait_until,
    with_host,
    with_service_bot,
)
from klepa_core import db
from klepa_core.app import init_layout, run_service
from klepa_core.host.adapter import sign_body
from klepa_core.host.outbox import HELD_ID_BASE
from klepa_core.host.supervisor import HostTiming

# Seconds instead of minutes, with room for a slow CI runner: the adapter beats every 0.1 s.
FAST = HostTiming(
    first_heartbeat=5.0, probe=5.0, release_within=1.0, hold_after=3.0, drop_after=30.0, not_polling=60.0, tick=0.05
)
SILENT = 1.4  # longer than release_within, shorter than hold_after: sends wait, no HOLD yet
STAGE1 = "For now I only accept files: documents, photos and voice messages."
HOLD = "I'm under maintenance right now. I saved your message and will answer later."
DROPPED = "I couldn't answer in time. Please send your message again."
NO_COMMANDS = "I have no commands. Just write to me in words."


@dataclasses.dataclass
class Stand:
    adapter: FakeAdapter
    host: FakeHost
    proxy_url: str
    core: asyncio.Task
    stop: asyncio.Event


@pytest.fixture
def host_cfg(make_config, install, fake_tg, service_tg, short_dir):
    api_port = free_port()
    proxy_port = free_port()
    while proxy_port == api_port:
        proxy_port = free_port()
    text = with_service_bot(BASE_CONFIG, install["service_token_file"], service_tg.url)
    cfg = make_config(api_root=fake_tg.url, text=with_host(text, short_dir / "adapter.sock", api_port, proxy_port))
    init_layout(cfg)
    conn = db.connect(cfg.core_db_path)
    conn.execute("INSERT INTO service_binding(person_id, chat_id, bound_at) VALUES ('owner', ?, 't')", (OWNER,))
    conn.close()
    return cfg


def start_core(cfg, timing=FAST):
    stop = asyncio.Event()
    core = asyncio.create_task(
        run_service(cfg, stop, copy_interval=0.05, retry_seconds=0.2, documents_grace=0.0, host_timing=timing)
    )
    return core, stop


@contextlib.asynccontextmanager
async def stand(cfg, *, timing=FAST, beat=True, poll=True, conversation_hooks=True, via_proxy=False, poll_timeout=1):
    """Core, the fake adapter beating and the fake host polling; everything stops on the way out."""
    core, stop = start_core(cfg, timing)
    await wait_until(lambda: cfg.host.socket_path.exists() and cfg.host_token_path.exists(), timeout=10)
    proxy_url = f"http://127.0.0.1:{cfg.host.proxy_port}"
    adapter = FakeAdapter(cfg.host.socket_path, cfg.adapter_key_path.read_bytes())
    host = FakeHost(
        f"http://127.0.0.1:{cfg.host.api_port}",
        cfg.host_token_path.read_text().strip(),
        adapter,
        proxy=proxy_url if via_proxy else None,
        conversation_hooks=conversation_hooks,
        poll_timeout=poll_timeout,
    )
    fakes = asyncio.Event()
    tasks = []
    if beat:
        tasks.append(asyncio.create_task(adapter.beat(fakes)))
    if poll:
        tasks.append(asyncio.create_task(host.run(fakes)))
    current = Stand(adapter, host, proxy_url, core, stop)
    try:
        yield current
    finally:
        fakes.set()
        for task in tasks:
            task.cancel()  # a long poll may still be open
        await asyncio.gather(*tasks, return_exceptions=True)
        current.stop.set()  # a test may have started Core again
        await asyncio.wait_for(current.core, 20)


def host_states(cfg):
    return [
        json.loads(row["data"]) for row in query(cfg, "SELECT data FROM event_log WHERE kind='host_state' ORDER BY id")
    ]


def host_state(cfg):
    states = host_states(cfg)
    return states[-1]["state"] if states else None


def alerts(service_tg):
    return [text for text in sent_texts(service_tg) if text.startswith("⚠️")]


async def running(cfg):
    await wait_until(lambda: host_state(cfg) == "RUNNING", timeout=10)


async def test_text_reaches_the_host_only_after_the_live_probe_and_the_answer_comes_back(host_cfg, fake_tg):
    async with stand(host_cfg) as s:
        await running(host_cfg)
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: "STUB: hello" in sent_texts(fake_tg), timeout=10)
    probe, model_probe, served = (update["message"] for update in s.host.updates)
    assert probe["chat"]["id"] >= 2**51  # the probe peer: no member, no real chat
    assert model_probe["chat"]["id"] == probe["chat"]["id"]
    assert model_probe["text"].startswith("Klepa start check")  # the second probe goes to the model
    assert set(served) == {"message_id", "from", "chat", "date", "text"}
    assert (served["from"]["id"], served["text"]) == (OWNER, "hello")
    assert [item["params"]["chat_id"] for item in fake_tg.sent] == [OWNER]  # the probe's block message never left


async def test_without_the_adapter_the_host_gets_nothing_and_people_get_the_hold_reply(host_cfg, fake_tg, service_tg):
    # Scenario 16: the plugin is not loaded.
    async with stand(host_cfg, beat=False) as s:
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: host_state(host_cfg) == "STOPPED", timeout=10)
        await wait_until(lambda: HOLD in sent_texts(fake_tg))
        await wait_until(lambda: alerts(service_tg) != [])
    assert s.host.updates == []
    assert alerts(service_tg)[0].startswith("⚠️ The host did not pass its check (no_heartbeat)")


async def test_hooks_without_conversation_access_never_let_the_host_run(host_cfg, fake_tg):
    # Scenario 16: allowConversationAccess is missing, so before_prompt_build and before_agent_run never run.
    async with stand(host_cfg, conversation_hooks=False) as s:
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: host_state(host_cfg) == "STOPPED", timeout=10)
    assert host_states(host_cfg)[-1]["reason"] == "probe"
    assert s.host.texts() == ["Klepa start probe"]


async def test_unsigned_adapter_messages_change_nothing(host_cfg):
    # Scenario 14: a control message without the adapter's signature, or with another key's.
    async with stand(host_cfg) as s:
        await running(host_cfg)
        forged = {
            "type": "turn_start",
            "boot_id": s.adapter.boot_id,
            "seq": 1_000_000,
            "run_id": "forged",
            "sender_id": str(OWNER),
            "chat_id": str(OWNER),
            "session_key": session_key(OWNER),
        }
        body = json.dumps(forged).encode()
        assert (await s.adapter.post(body, None))[0] == 401
        assert (await s.adapter.post(body, sign_body(b"x" * 32, body)))[0] == 401
        beat = json.dumps({"type": "heartbeat", "boot_id": "intruder-boot", "seq": 1}).encode()
        assert (await s.adapter.post(beat, None))[0] == 401
        await asyncio.sleep(0.2)
        assert host_state(host_cfg) == "RUNNING"
    assert query(host_cfg, "SELECT * FROM host_run WHERE run_id='forged'") == []


async def test_commands_never_reach_the_host(host_cfg, fake_tg):
    # Scenario 43.
    async with stand(host_cfg) as s:
        await running(host_cfg)
        for command in ("/queue steer", "/verbose on", "/model opus"):
            fake_tg.add_text(OWNER, command)
        fake_tg.add_text(OWNER, "after the commands")
        await wait_until(lambda: "after the commands" in s.host.texts(), timeout=10)
        await wait_until(lambda: sent_texts(fake_tg).count(NO_COMMANDS) == 3)
    assert not any(text.startswith("/") for text in s.host.texts())


async def test_the_host_interface_is_narrow(host_cfg, fake_tg, service_tg):
    # Scenario 44.
    async with stand(host_cfg) as s:
        await running(host_cfg)
        s.host.answering = False
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: "hello" in s.host.texts(), timeout=10)
        message_id = s.host.updates[-1]["message"]["message_id"]
        wrong = FakeHost(s.host.url, "123:" + "W" * 43, s.adapter)
        assert (await wrong.call("getMe"))[0] == 401
        assert (await s.host.call("getMe", headers={"Host": "evil.example"}))[0] == 403
        assert (await s.host.call("getMe", headers={"Origin": "http://evil.example"}))[0] == 403
        conflicts = []
        for _ in range(20):  # the fake host's own long poll is open nearly all the time
            conflicts.append((await s.host.call("getUpdates", {"timeout": 0}))[0])
            if conflicts[-1] == 409:
                break
        assert conflicts[-1] == 409
        for method in ("editMessageText", "deleteMessage", "pinChatMessage", "setMessageReaction", "sendPhoto"):
            assert (await s.host.call(method, {"chat_id": OWNER, "message_id": message_id, "text": "x"}))[0] == 400
        keyboard = {"inline_keyboard": [[{"text": "Pay", "url": "https://evil.example"}]]}
        assert (await s.host.send(OWNER, "answer", reply_markup=keyboard))[0] == 200
        quote = {"message_id": message_id, "chat_id": MEMBER}
        assert (await s.host.send(OWNER, "quoted", reply_parameters=quote))[0] == 400
        await wait_until(lambda: "answer" in sent_texts(fake_tg))
        await wait_until(lambda: any("Two clients poll" in text for text in alerts(service_tg)))
    [sent] = [item["params"] for item in fake_tg.sent if item["params"]["text"] == "answer"]
    assert "reply_markup" not in sent
    assert sent["link_preview_options"] == {"is_disabled": True}
    calls = [json.loads(row["data"]) for row in query(host_cfg, "SELECT data FROM event_log WHERE kind='host_call'")]
    assert {"method": "editMessageText", "status": 400} in calls
    assert {"method": "getUpdates", "status": 409} in calls
    assert {"method": "sendMessage", "status": 200} in calls


async def test_sends_wait_while_the_adapter_is_silent_and_leave_in_order_when_it_returns(host_cfg, fake_tg):
    # Scenario 38, the part on Core's side: nothing leaves until the heartbeat is back.
    async with stand(host_cfg) as s:
        await running(host_cfg)
        s.host.answering = False
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: "hello" in s.host.texts(), timeout=10)
        s.adapter.beating = False
        await asyncio.sleep(SILENT)
        first = await s.host.send(OWNER, "part one")
        second = await s.host.send(OWNER, "part two")
        assert first[1]["result"]["message_id"] > HELD_ID_BASE
        assert second[1]["result"]["message_id"] > first[1]["result"]["message_id"]
        await asyncio.sleep(0.3)
        assert "part one" not in sent_texts(fake_tg)
        s.adapter.beating = True
        await wait_until(lambda: sent_texts(fake_tg)[-2:] == ["part one", "part two"], timeout=5)
    assert len(query(host_cfg, "SELECT * FROM event_log WHERE kind='host_send_held'")) == 2  # the trace of holding


async def test_sends_held_too_long_are_dropped_and_the_person_is_asked_to_repeat(host_cfg, fake_tg):
    async with stand(host_cfg, timing=dataclasses.replace(FAST, hold_after=30.0, drop_after=1.0)) as s:
        await running(host_cfg)
        s.host.answering = False
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: "hello" in s.host.texts(), timeout=10)
        s.adapter.beating = False
        await asyncio.sleep(SILENT)
        assert (await s.host.send(OWNER, "late answer"))[0] == 200
        await wait_until(lambda: DROPPED in sent_texts(fake_tg), timeout=5)
        s.adapter.beating = True
        await asyncio.sleep(0.5)
    assert "late answer" not in sent_texts(fake_tg)
    assert sent_texts(fake_tg).count(DROPPED) == 1


async def test_a_silent_adapter_puts_the_host_on_hold_and_it_comes_back(host_cfg, fake_tg, service_tg):
    async with stand(host_cfg) as s:
        await running(host_cfg)
        s.adapter.beating = False
        await wait_until(lambda: host_state(host_cfg) == "HOLD", timeout=5)
        fake_tg.add_text(OWNER, "are you there?")
        fake_tg.add_text(OWNER, "hello?")
        await wait_until(lambda: HOLD in sent_texts(fake_tg), timeout=5)
        await wait_until(lambda: any("has been silent" in text for text in alerts(service_tg)))
        s.adapter.beating = True
        await wait_until(lambda: "hello?" in s.host.texts(), timeout=5)
    assert sent_texts(fake_tg).count(HOLD) == 1  # once per ten minutes per chat
    assert s.host.texts()[-2:] == ["are you there?", "hello?"]


async def test_a_gateway_restart_is_quiet_and_the_waiting_turn_registers(host_cfg, fake_tg, service_tg):
    # Scenario 41: a new boot_id, no false alerts; the host's own queue repeats the unanswered turn.
    async with stand(host_cfg) as s:
        await running(host_cfg)
        s.host.answering = False
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: "hello" in s.host.texts(), timeout=10)
        s.adapter.restart()
        s.host.answering = True
        await wait_until(lambda: [state["state"] for state in host_states(host_cfg)][-2:] == ["STARTING", "RUNNING"])
        await s.adapter.prompt_built("recovered-run", OWNER)
        _, turn = await s.adapter.turn_start("recovered-run", OWNER)
    assert turn["outcome"] == "pass"  # the repeated turn reaches the model
    [row] = query(host_cfg, "SELECT host_message_id FROM host_run WHERE run_id='recovered-run'")
    assert row["host_message_id"] is not None
    assert alerts(service_tg) == []


async def test_a_held_send_survives_a_core_restart_and_leaves_once(host_cfg, fake_tg):
    # Scenario 38: Core stops between answering "sent" and sending.
    async with stand(host_cfg) as s:
        await running(host_cfg)
        s.host.answering = False
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: "hello" in s.host.texts(), timeout=10)
        s.adapter.beating = False
        await asyncio.sleep(SILENT)
        assert (await s.host.send(OWNER, "kept"))[0] == 200
    assert "kept" not in sent_texts(fake_tg)
    async with stand(host_cfg):
        await wait_until(lambda: "kept" in sent_texts(fake_tg), timeout=10)
        await asyncio.sleep(0.3)
    assert sent_texts(fake_tg).count("kept") == 1


async def test_pause_and_resume_from_the_service_bot(host_cfg, fake_tg, service_tg):
    def last_status(button):
        for item in reversed(service_tg.sent):
            row = (item["params"].get("reply_markup") or {}).get("inline_keyboard", [[]])[0]
            if [b["text"] for b in row] == ["Status", button]:
                return item, row[1]
        return None

    async with stand(host_cfg) as s:
        await running(host_cfg)
        service_tg.add_text(OWNER, "/status")
        await wait_until(lambda: last_status("Pause") is not None)
        status, pause = last_status("Pause")
        assert status["params"]["text"].startswith("✅ All good · host: running")
        service_tg.press(OWNER, status["message"], pause["callback_data"])
        await wait_until(lambda: host_state(host_cfg) == "HOLD")
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: HOLD in sent_texts(fake_tg), timeout=5)
        await wait_until(lambda: last_status("Resume") is not None)
        paused, resume = last_status("Resume")
        assert "host: paused" in paused["params"]["text"]
        service_tg.press(OWNER, paused["message"], resume["callback_data"])
        await wait_until(lambda: "hello" in s.host.texts(), timeout=10)


async def test_the_hosts_http_goes_through_the_egress_proxy(host_cfg, fake_tg):
    async with stand(host_cfg, via_proxy=True) as s:
        await running(host_cfg)
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: "STUB: hello" in sent_texts(fake_tg), timeout=10)
        async with (
            aiohttp.ClientSession() as session,
            session.get("http://telemetry.example/v1", proxy=s.proxy_url) as resp,
        ):
            assert resp.status == 403
        reader, writer = await asyncio.open_connection("127.0.0.1", host_cfg.host.proxy_port)
        writer.write(b"CONNECT api.telegram.org:443 HTTP/1.1\r\nHost: api.telegram.org:443\r\n\r\n")
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 403")  # no way around the gatekeeper
        writer.close()
    denied = [
        json.loads(row["data"]) for row in query(host_cfg, "SELECT data FROM event_log WHERE kind='egress_denied'")
    ]
    assert [(item["port"], item["reason"]) for item in denied] == [(80, "not_allowed"), (443, "not_allowed")]


async def test_while_core_is_down_the_host_gets_nothing_and_then_the_backlog_comes_in_order(host_cfg, fake_tg):
    # Scenario 12, with the host polling through the egress proxy the whole time. Core also stops promptly with the
    # host's long poll and its tunnel open, and logs its stop.
    async with stand(host_cfg, via_proxy=True, poll_timeout=30) as s:
        await running(host_cfg)
        await asyncio.sleep(0.3)  # the host's 30-second poll is open, through the proxy
        loop = asyncio.get_running_loop()
        started = loop.time()
        s.stop.set()
        await asyncio.wait_for(s.core, 10)
        assert loop.time() - started < 5
        assert query(host_cfg, "SELECT kind FROM event_log ORDER BY id DESC LIMIT 1")[0]["kind"] == "core_stopped"
        fake_tg.add_text(OWNER, "one")
        fake_tg.add_text(OWNER, "two")
        await asyncio.sleep(0.5)
        assert "one" not in s.host.texts()  # nobody serves the host while Core is down
        s.core, s.stop = start_core(host_cfg)
        await wait_until(lambda: s.host.texts()[-2:] == ["one", "two"], timeout=15)


@pytest.mark.parametrize("content", [b"", b"\xff\xfe" * 30])  # left empty by a crash; not even text
async def test_broken_host_secrets_turn_the_host_off_and_core_runs_on(host_cfg, fake_tg, service_tg, content):
    host_cfg.host_token_path.parent.mkdir(mode=0o700, exist_ok=True)
    host_cfg.host_token_path.write_bytes(content)
    host_cfg.host_token_path.chmod(0o600)
    core, stop = start_core(host_cfg)
    try:
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: STAGE1 in sent_texts(fake_tg), timeout=10)  # Core answers text itself
        await wait_until(lambda: any("secrets:KeyFileError" in text for text in alerts(service_tg)), timeout=10)
    finally:
        stop.set()
        await asyncio.wait_for(core, 20)
    assert "host_off" in [row["kind"] for row in query(host_cfg, "SELECT kind FROM event_log")]


async def test_an_answer_telegram_refuses_gets_an_apology_and_the_owner_hears_of_it(host_cfg, fake_tg, service_tg):
    async with stand(host_cfg) as s:
        await running(host_cfg)
        s.host.answering = False
        fake_tg.add_text(OWNER, "hello")
        await wait_until(lambda: "hello" in s.host.texts(), timeout=10)
        fake_tg.fail("sendMessage", status=403, description="Forbidden: bot was blocked by the user")
        assert (await s.host.send(OWNER, "answer"))[0] == 200  # the host heard "sent"
        await wait_until(lambda: DROPPED in sent_texts(fake_tg), timeout=5)
        await wait_until(lambda: any("Telegram refused an answer" in text for text in alerts(service_tg)), timeout=5)
