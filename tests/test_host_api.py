import asyncio
import contextlib
import json

import aiohttp
import pytest

from helpers import MEMBER, OWNER, STRANGER, wait_until
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.host import api as host_api_module
from klepa_core.host.api import MAX_SOURCE_CHARS, HostApi
from klepa_core.host.outbox import HELD_ID_BASE, HostOutbox
from klepa_core.host.queue import HostQueue
from klepa_core.host.supervisor import PROBE_BLOCK
from klepa_core.host.turns import TurnRegistry

TOKEN = "123:" + "F" * 43
UNDICI_HEADERS = {"sec-fetch-mode": "cors", "accept": "*/*", "accept-language": "*", "user-agent": "node"}
PEER = 2**51 + 7


class Gate:
    """Stands in for the supervisor."""

    def __init__(self):
        self.serving = True
        self.releasing = True
        self.polls = 0
        self.blocked = 0

    def may_serve(self, host_message_id, kind):
        return self.serving

    def may_release(self):
        return self.releasing

    def on_poll(self):
        self.polls += 1

    def on_probe_blocked(self):
        self.blocked += 1


class FakeAlerts:
    def __init__(self):
        self.raised = []

    def raise_(self, name, **fields):
        self.raised.append(name)
        return True


@pytest.fixture
async def gatekeeper(core_db, api):
    cfg, conn, journal = core_db
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), PEER)
    gate = Gate()
    events = EventLog(conn)
    outbox = HostOutbox(
        conn, api, gate, queue, TurnRegistry(conn, queue), Outbox(conn, api, events), events, cfg.locale
    )
    alerts = FakeAlerts()
    host_api = HostApi(TOKEN, 0, [OWNER, MEMBER], queue, gate, outbox, api, events, alerts=alerts)
    await host_api.start()
    yield host_api, queue, journal, outbox, gate, alerts, conn
    await host_api.stop()


async def call(host_api, method, params=None, *, token=TOKEN, headers=None, data=None):
    url = f"http://127.0.0.1:{host_api.port}/bot{token}/{method}"
    async with aiohttp.ClientSession() as session:
        kwargs = {"data": data} if data is not None else {"json": params or {}}
        async with session.post(url, headers=headers, **kwargs) as resp:
            return resp.status, await resp.json()


def issue(queue, journal, update_id, sender, message_id, text="hi"):
    message = {"message_id": message_id, "date": 1, "chat": {"id": sender}, "from": {"id": sender}, "text": text}
    journal.append_batch([{"update_id": update_id, "message": message}], "t")
    queue.enqueue(update_id, sender, message_id, sender, 1)


async def test_a_wrong_token_is_unauthorized(gatekeeper):
    host_api, *_ = gatekeeper
    status, body = await call(host_api, "getMe", token="123:" + "G" * 43)
    assert (status, body["error_code"]) == (401, 401)


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Host": "localhost"},
        {"Origin": "http://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Dest": "empty"},
        {"Sec-Fetch-User": "?1"},
    ],
)
async def test_other_hosts_and_browsers_are_refused(gatekeeper, headers):
    host_api, *_ = gatekeeper
    if headers.get("Host") == "localhost":
        headers = {"Host": f"localhost:{host_api.port}"}
    assert (await call(host_api, "getMe", headers=headers))[0] == 403


async def test_the_headers_of_nodes_fetch_pass(gatekeeper):
    # undici, the fetch of Node that OpenClaw uses, sends Sec-Fetch-Mode on every request; browsers also send
    # Sec-Fetch-Site and Sec-Fetch-Dest, and Origin across sites.
    host_api, *_ = gatekeeper
    assert (await call(host_api, "deleteWebhook", headers=UNDICI_HEADERS))[0] == 200


@pytest.mark.parametrize(
    "method",
    [
        "editMessageText",
        "editMessageReplyMarkup",
        "deleteMessage",
        "pinChatMessage",
        "setMessageReaction",
        "forwardMessage",
        "copyMessage",
        "sendPhoto",
        "sendDocument",
        "getFile",
        "setWebhook",
        "logOut",
    ],
)
async def test_methods_outside_the_allow_list_are_refused_and_logged(gatekeeper, fake_tg, method):
    host_api, *_, conn = gatekeeper
    status, body = await call(host_api, method, {"chat_id": OWNER, "message_id": 1, "text": "x"})
    assert (status, body["ok"]) == (400, False)
    assert fake_tg.calls == []
    row = conn.execute("SELECT data FROM event_log WHERE kind='host_call'").fetchone()
    assert json.loads(row[0]) == {"method": method, "status": 400}


async def test_file_downloads_do_not_exist_here(gatekeeper):
    host_api, *_ = gatekeeper
    async with aiohttp.ClientSession() as session:
        url = f"http://127.0.0.1:{host_api.port}/file/bot{TOKEN}/documents/file_1"
        async with session.get(url) as resp:
            assert resp.status == 404


async def test_local_methods_are_answered_here(gatekeeper, fake_tg):
    host_api, *_ = gatekeeper
    for method in ("deleteWebhook", "deleteMyCommands", "setMyCommands"):
        assert await call(host_api, method, {"commands": []}) == (200, {"ok": True, "result": True})
    assert fake_tg.calls == []


async def test_get_me_asks_telegram_once(gatekeeper, fake_tg):
    host_api, *_ = gatekeeper
    first = await call(host_api, "getMe")
    assert first == await call(host_api, "getMe")
    assert first[1]["result"]["username"] == "test_bot"
    assert fake_tg.calls == ["getMe"]


async def test_get_updates_serves_what_the_supervisor_allows(gatekeeper):
    host_api, queue, journal, _, gate, _, _ = gatekeeper
    issue(queue, journal, 1, OWNER, 10, "hello")
    gate.serving = False
    assert await call(host_api, "getUpdates", {"timeout": 0}) == (200, {"ok": True, "result": []})
    gate.serving = True
    _, body = await call(host_api, "getUpdates", {"offset": 0, "timeout": 0})
    assert [update["message"]["text"] for update in body["result"]] == ["hello"]
    assert gate.polls == 2


async def test_a_long_poll_returns_as_soon_as_a_message_comes(gatekeeper):
    host_api, queue, journal, *_ = gatekeeper
    poll = asyncio.create_task(call(host_api, "getUpdates", {"timeout": 5}))
    await asyncio.sleep(0.2)
    issue(queue, journal, 1, OWNER, 10, "late")
    _, body = await asyncio.wait_for(poll, 2)
    assert [update["message"]["text"] for update in body["result"]] == ["late"]


async def test_a_second_long_poll_gets_409_and_an_alert(gatekeeper):
    host_api, *_, alerts, _ = gatekeeper
    first = asyncio.create_task(call(host_api, "getUpdates", {"timeout": 2}))
    await wait_until(lambda: host_api._poll is not None)
    status, body = await call(host_api, "getUpdates", {"timeout": 0})
    assert (status, body["error_code"]) == (409, 409)
    assert alerts.raised == ["host_conflict"]
    assert (await first)[0] == 200


async def test_a_poller_that_went_away_frees_the_poll(gatekeeper):
    host_api, *_, alerts, _ = gatekeeper
    session = aiohttp.ClientSession()
    url = f"http://127.0.0.1:{host_api.port}/bot{TOKEN}/getUpdates"
    first = asyncio.create_task(session.post(url, json={"timeout": 10}))
    await wait_until(lambda: host_api._poll is not None)
    first.cancel()
    await session.close()  # the host process died with its poll open
    await asyncio.sleep(0.1)
    assert (await call(host_api, "getUpdates", {"timeout": 0}))[0] == 200
    assert alerts.raised == []


async def test_send_message_keeps_only_the_allowed_fields(gatekeeper, fake_tg):
    host_api, queue, journal, outbox, *_ = gatekeeper
    issue(queue, journal, 1, OWNER, 10)
    await call(host_api, "getUpdates", {"timeout": 0})
    status, body = await call(
        host_api,
        "sendMessage",
        {
            "chat_id": OWNER,
            "text": "<b>answer</b>",
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]},
            "entities": [{"type": "bold", "offset": 0, "length": 1}],
            "message_effect_id": "1",
            "business_connection_id": "b",
            "link_preview_options": {"is_disabled": False},
            "reply_parameters": {"message_id": 10, "quote": "hi"},
        },
    )
    assert status == 200
    assert body["result"]["message_id"] > HELD_ID_BASE
    assert body["result"]["text"] == "answer"
    await outbox.release_due()
    [sent] = fake_tg.sent
    assert sent["params"] == {
        "chat_id": OWNER,
        "text": "<b>answer</b>",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
        "reply_parameters": {"message_id": 10, "allow_sending_without_reply": True},
    }


@pytest.mark.parametrize(
    ("chat_id", "issued", "description"),
    [
        (STRANGER, True, "chat not found"),
        (-100123, True, "chat not found"),
        (MEMBER, False, "no open conversation"),
    ],
)
async def test_send_message_needs_a_members_chat_with_an_open_conversation(gatekeeper, chat_id, issued, description):
    host_api, queue, journal, *_ = gatekeeper
    if issued:
        issue(queue, journal, 1, MEMBER, 10)
        await call(host_api, "getUpdates", {"timeout": 0})
    status, body = await call(host_api, "sendMessage", {"chat_id": chat_id, "text": "x"})
    assert status == 400
    assert description in body["description"]


async def test_replies_point_only_into_the_same_chat(gatekeeper):
    host_api, queue, journal, *_ = gatekeeper
    issue(queue, journal, 1, OWNER, 10)
    issue(queue, journal, 2, MEMBER, 20)
    await call(host_api, "getUpdates", {"timeout": 0})
    ok, body = await call(
        host_api, "sendMessage", {"chat_id": OWNER, "text": "a", "reply_parameters": {"message_id": 10}}
    )
    own = body["result"]["message_id"]
    cases = [
        {"reply_parameters": {"message_id": 20}},  # a message of the other chat
        {"reply_parameters": {"message_id": 10, "chat_id": MEMBER}},  # a quote from the other chat
        {"reply_parameters": {"message_id": 999}},
        {"reply_to_message_id": 20},
    ]
    results = [await call(host_api, "sendMessage", {"chat_id": OWNER, "text": "b", **case}) for case in cases]
    assert ok == 200
    assert [status for status, _ in results] == [400, 400, 400, 400]
    assert (await call(host_api, "sendMessage", {"chat_id": OWNER, "text": "c", "reply_to_message_id": own}))[0] == 200
    status, _ = await call(host_api, "sendMessage", {"chat_id": MEMBER, "text": "d", "reply_to_message_id": own})
    assert status == 400


@pytest.mark.parametrize(
    ("params", "description"),
    [
        ({"text": "x", "parse_mode": "MarkdownV2"}, "parse_mode"),
        ({"text": "x" * 4097}, "too long"),
        ({"text": "<b></b>", "parse_mode": "HTML"}, "empty"),
        ({"text": 5}, "empty"),
    ],
)
async def test_bad_texts_are_refused(gatekeeper, params, description):
    host_api, queue, journal, *_ = gatekeeper
    issue(queue, journal, 1, OWNER, 10)
    await call(host_api, "getUpdates", {"timeout": 0})
    status, body = await call(host_api, "sendMessage", {"chat_id": OWNER, **params})
    assert status == 400
    assert description in body["description"]


async def test_plain_text_is_escaped_before_it_is_sent_as_html(gatekeeper, fake_tg):
    host_api, queue, journal, outbox, *_ = gatekeeper
    issue(queue, journal, 1, OWNER, 10)
    await call(host_api, "getUpdates", {"timeout": 0})
    await call(host_api, "sendMessage", {"chat_id": OWNER, "text": "1 < 2 & <b>x</b>"})
    await outbox.release_due()
    assert fake_tg.sent[0]["params"]["text"] == "1 &lt; 2 &amp; &lt;b&gt;x&lt;/b&gt;"


async def test_form_and_query_parameters_work_and_files_are_refused(gatekeeper):
    host_api, queue, journal, *_ = gatekeeper
    issue(queue, journal, 1, OWNER, 10)
    await call(host_api, "getUpdates", {"timeout": 0})
    status, _ = await call(
        host_api, "sendMessage", data={"chat_id": str(OWNER), "text": "x", "reply_parameters": '{"message_id": 10}'}
    )
    assert status == 200
    form = aiohttp.FormData()
    form.add_field("chat_id", str(OWNER))
    form.add_field("photo", b"\x89PNG", filename="x.png")
    assert (await call(host_api, "sendMessage", data=form))[0] == 400


async def test_the_probe_chat_is_answered_but_never_reaches_telegram(gatekeeper, fake_tg):
    host_api, queue, _, outbox, gate, _, conn = gatekeeper
    queue.add_probe()
    await call(host_api, "getUpdates", {"timeout": 0})
    status, _ = await call(host_api, "sendMessage", {"chat_id": PEER, "text": "Your message could not be sent: x"})
    assert gate.blocked == 0  # not the probe's block
    await call(host_api, "sendMessage", {"chat_id": PEER, "text": f"Your message could not be sent: {PROBE_BLOCK}"})
    assert gate.blocked == 1
    await call(host_api, "sendChatAction", {"chat_id": PEER, "action": "typing"})
    await outbox.release_due()
    assert status == 200
    assert conn.execute("SELECT COUNT(*) FROM outbound").fetchone()[0] == 0
    assert fake_tg.sent == []
    assert "sendChatAction" not in fake_tg.calls


async def test_typing_reaches_telegram_only_in_an_open_conversation(gatekeeper, fake_tg):
    host_api, queue, journal, _, gate, *_ = gatekeeper
    assert (await call(host_api, "sendChatAction", {"chat_id": OWNER, "action": "typing"}))[0] == 200
    issue(queue, journal, 1, OWNER, 10)
    await call(host_api, "getUpdates", {"timeout": 0})
    gate.releasing = False
    await call(host_api, "sendChatAction", {"chat_id": OWNER, "action": "typing"})
    assert "sendChatAction" not in fake_tg.calls
    gate.releasing = True
    await call(host_api, "sendChatAction", {"chat_id": OWNER, "action": "typing"})
    assert fake_tg.calls.count("sendChatAction") == 1


async def test_connections_beyond_the_limit_are_closed_and_the_host_gets_in_again_later(gatekeeper, monkeypatch):
    host_api, *_ = gatekeeper
    monkeypatch.setattr(host_api_module, "MAX_CONNECTIONS", 3)
    idle = [await asyncio.open_connection("127.0.0.1", host_api.port) for _ in range(3)]
    await asyncio.sleep(0.1)
    reader, writer = await asyncio.open_connection("127.0.0.1", host_api.port)
    assert await asyncio.wait_for(reader.read(), 2) == b""  # closed at once
    writer.close()
    for _, idle_writer in idle:
        idle_writer.close()
    await asyncio.sleep(0.1)
    assert (await call(host_api, "getMe"))[0] in (200, 502)


async def test_stop_ends_a_long_poll_at_once(gatekeeper):
    host_api, *_ = gatekeeper
    poll = asyncio.create_task(call(host_api, "getUpdates", {"timeout": 30}))
    await wait_until(lambda: host_api._poll is not None)
    started = asyncio.get_running_loop().time()
    await host_api.stop()
    assert asyncio.get_running_loop().time() - started < 2
    with contextlib.suppress(aiohttp.ClientError):
        await asyncio.wait_for(poll, 2)


async def test_a_huge_text_is_refused_before_it_is_parsed(gatekeeper):
    host_api, queue, journal, *_ = gatekeeper
    issue(queue, journal, 1, OWNER, 10)
    await call(host_api, "getUpdates", {"timeout": 0})
    status, body = await call(host_api, "sendMessage", {"chat_id": OWNER, "text": "a-" * MAX_SOURCE_CHARS})
    assert (status, "too long" in body["description"]) == (400, True)


async def test_typing_goes_to_telegram_at_most_every_few_seconds(gatekeeper, fake_tg):
    host_api, queue, journal, *_ = gatekeeper
    issue(queue, journal, 1, OWNER, 10)
    await call(host_api, "getUpdates", {"timeout": 0})
    for _ in range(3):
        await call(host_api, "sendChatAction", {"chat_id": OWNER, "action": "typing"})
    assert fake_tg.calls.count("sendChatAction") == 1


async def test_a_chat_with_too_many_waiting_messages_refuses_more(gatekeeper, monkeypatch):
    host_api, queue, journal, _, gate, *_ = gatekeeper
    monkeypatch.setattr(host_api_module, "MAX_WAITING_PER_CHAT", 2)
    issue(queue, journal, 1, OWNER, 10)
    await call(host_api, "getUpdates", {"timeout": 0})
    gate.releasing = False
    statuses = [(await call(host_api, "sendMessage", {"chat_id": OWNER, "text": "x"}))[0] for _ in range(3)]
    assert statuses == [200, 200, 400]


async def test_a_refusal_is_logged_once_a_minute(gatekeeper):
    host_api, *_, conn = gatekeeper
    for _ in range(3):
        await call(host_api, "editMessageText", {"chat_id": OWNER, "message_id": 1, "text": "x"})
    assert conn.execute("SELECT COUNT(*) FROM event_log WHERE kind='host_call'").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("text", "issued", "reason"),
    [("x" * 5000, True, "too long"), ("hello", False, "no open conversation")],
)
async def test_an_answer_refused_before_it_is_taken_raises_an_alert(gatekeeper, text, issued, reason):
    host_api, queue, journal, *_, alerts, _ = gatekeeper
    if issued:
        issue(queue, journal, 1, OWNER, 10)
        await call(host_api, "getUpdates", {"timeout": 0})
    status, body = await call(host_api, "sendMessage", {"chat_id": OWNER, "text": text})
    assert status == 400
    assert reason in body["description"]
    assert alerts.raised == ["host_send_refused"]  # the person may be left without an answer
