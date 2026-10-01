import asyncio
import time

import pytest

from helpers import MEMBER, OWNER, STRANGER, wait_until
from klepa_core import db
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.health import Health, Probe
from klepa_core.host.queue import HostQueue
from klepa_core.host.supervisor import GatewayState, Supervisor
from klepa_core.journal import InboundJournal
from klepa_core.servicebot import BUTTON_TTL_SECONDS, ServiceBot, bind_owner_chat, new_bind_code


@pytest.fixture
def setup(make_config):
    cfg = make_config()
    conn = db.connect(cfg.core_db_path)
    db.migrate(conn)
    db.seed(conn, cfg)
    return cfg, conn


def test_bind_codes_are_long_and_unique():
    codes = {new_bind_code() for _ in range(100)}
    assert len(codes) == 100
    assert all(len(code) >= 22 for code in codes)


async def test_binding_needs_the_code_from_the_owner_and_a_terminal_yes(setup, service_tg, service_api):
    cfg, conn = setup
    service_tg.add_text(STRANGER, "/start CODE")
    service_tg.add_text(MEMBER, "/start CODE")
    service_tg.add_text(OWNER, "/start CODE", chat_id=-100123, chat_type="group")
    service_tg.add_text(OWNER, "/start WRONG")
    service_tg.add_text(OWNER, "/start CODE")
    questions = []

    def confirm(question):
        questions.append(question)
        return True

    chat = await bind_owner_chat(cfg, service_api, conn, "CODE", confirm, deadline_seconds=5, poll_timeout=1)
    assert chat == OWNER
    assert db.owner_service_chat(conn) == OWNER
    assert len(questions) == 1
    assert str(OWNER) in questions[0]
    assert await service_api.get_updates(None, 0) == []  # the code was acknowledged: Core never sees it


async def test_binding_declined_in_the_terminal_stores_nothing(setup, service_tg, service_api):
    cfg, conn = setup
    service_tg.add_text(OWNER, "/start CODE")
    chat = await bind_owner_chat(cfg, service_api, conn, "CODE", lambda q: False, deadline_seconds=5, poll_timeout=1)
    assert chat is None
    assert db.owner_service_chat(conn) is None


async def test_binding_gives_up_after_the_deadline(setup, service_tg, service_api):
    cfg, conn = setup
    chat = await bind_owner_chat(cfg, service_api, conn, "CODE", lambda q: True, deadline_seconds=0.5, poll_timeout=0)
    assert chat is None


EXPIRED = "This button no longer works."


@pytest.fixture
def running(setup, service_api, tmp_path):
    cfg, conn = setup
    conn.execute("INSERT INTO service_binding(person_id, chat_id, bound_at) VALUES ('owner', ?, 't')", (OWNER,))
    events = EventLog(conn)
    outbox = Outbox(conn, service_api, events, bot="service")
    health = Health(cfg, conn, InboundJournal(tmp_path / "inbound.db"))
    now = [time.time()]

    async def healthy():
        return Probe(True)

    bot = ServiceBot(cfg, service_api, conn, outbox, health, events, probe=healthy, clock=lambda: now[0])
    return bot, outbox, events, now


async def deliver(service_api, bot):
    updates = await service_api.get_updates(None, 0)
    for update in updates:
        await bot.handle(update)
    if updates:
        await service_api.get_updates(updates[-1]["update_id"] + 1, 0)


async def test_status_button_works_once_and_only_for_the_owner(running, service_tg, service_api):
    bot, outbox, _, _ = running
    assert await bot.send_status("daily:2026-10-05")
    assert not await bot.send_status("daily:2026-10-05")  # once per key
    await outbox.send_due()
    first = service_tg.sent[-1]
    button = first["params"]["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Status"
    assert first["params"]["text"].startswith("✅ All good")
    service_tg.press(MEMBER, first["message"], button["callback_data"])  # not the owner
    service_tg.press(OWNER, first["message"], button["callback_data"])
    service_tg.press(OWNER, first["message"], button["callback_data"])  # the same button again
    await deliver(service_api, bot)
    await outbox.send_due()
    assert len(service_tg.sent) == 2
    assert [answer.get("text") for answer in service_tg.answered] == [EXPIRED, None, EXPIRED]


async def test_moved_group_and_expired_presses_do_nothing(running, service_tg, service_api):
    bot, outbox, _, now = running
    assert await bot.send_status("daily:2026-10-05")
    await outbox.send_due()
    first = service_tg.sent[-1]
    data = first["params"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    other = await service_api.send_message(OWNER, "a message the button does not belong to")
    service_tg.press(OWNER, other, data)
    service_tg.press(OWNER, {**first["message"], "chat": {"id": -100123, "type": "group"}}, data)
    await deliver(service_api, bot)
    now[0] += BUTTON_TTL_SECONDS + 1
    service_tg.press(OWNER, first["message"], data)  # the right message, a week too late
    await deliver(service_api, bot)
    await outbox.send_due()
    assert len(service_tg.sent) == 2  # the line and the other message: no second status
    assert [answer.get("text") for answer in service_tg.answered] == [EXPIRED] * 3


async def test_words_get_the_fixed_reply_and_strangers_nothing(running, service_tg, service_api):
    bot, outbox, events, _ = running
    service_tg.add_text(STRANGER, "/status")
    service_tg.add_text(STRANGER, "/status")  # logged once an hour per sender, not per message
    service_tg.add_text(MEMBER, "/status")
    service_tg.add_text(OWNER, "/status", chat_id=-100123, chat_type="group")  # the owner, but not in private
    service_tg.add_text(OWNER, "how are you?")
    service_tg.add_text(OWNER, "/status")
    await deliver(service_api, bot)
    await outbox.send_due()
    texts = [item["params"]["text"] for item in service_tg.sent]
    assert texts[0].startswith("The mechanic arrives")
    assert texts[1].startswith("✅ All good")
    assert len(texts) == 2
    assert all(item["params"]["chat_id"] == OWNER for item in service_tg.sent)
    assert events.kinds().count("service_update_rejected") == 3  # the stranger, the member, the owner's group


async def test_an_update_that_fails_is_skipped_and_the_bot_goes_on(running, service_tg, monkeypatch):
    bot, outbox, events, _ = running
    handle = bot.handle
    calls = []

    async def flaky(update):
        calls.append(update["update_id"])
        if len(calls) == 1:
            raise RuntimeError("a bug in a handler")
        await handle(update)

    monkeypatch.setattr(bot, "handle", flaky)
    service_tg.add_text(OWNER, "/status")
    service_tg.add_text(OWNER, "/status")
    stop = asyncio.Event()
    task = asyncio.create_task(bot.poll_forever(stop))
    try:
        await wait_until(lambda: outbox.conn.execute("SELECT COUNT(*) FROM outbound").fetchone()[0] == 1)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert events.kinds().count("service_update_failed") == 1
    assert len(calls) == 2


async def test_pause_and_resume_buttons_hold_and_restart_the_host(running, setup, service_tg, service_api, tmp_path):
    bot, outbox, events, _ = running
    cfg, conn = setup
    queue = HostQueue(conn, InboundJournal(tmp_path / "host-inbound.db"), cfg.members_by_telegram_id(), 2**51 + 7)
    bot.supervisor = Supervisor(conn, queue, events)
    bot.health.host_state = bot.supervisor.describe
    assert await bot.send_status("status:1")
    await outbox.send_due()
    first = service_tg.sent[-1]
    buttons = first["params"]["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == ["Status", "Pause"]
    service_tg.press(MEMBER, first["message"], buttons[1]["callback_data"])  # not the owner: nothing happens
    await deliver(service_api, bot)
    assert not bot.supervisor.paused
    service_tg.press(OWNER, first["message"], buttons[1]["callback_data"])
    await deliver(service_api, bot)
    await outbox.send_due()
    assert (bot.supervisor.state, bot.supervisor.paused) == (GatewayState.HOLD, True)
    second = service_tg.sent[-1]
    assert "host: paused" in second["params"]["text"]
    buttons = second["params"]["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == ["Status", "Resume"]
    service_tg.press(OWNER, second["message"], buttons[1]["callback_data"])
    await deliver(service_api, bot)
    assert (bot.supervisor.state, bot.supervisor.paused) == (GatewayState.STARTING, False)
