import json

import pytest

from helpers import MEMBER, OWNER, Clock
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.host.outbox import HELD_ID_BASE, HostOutbox
from klepa_core.host.queue import HostQueue
from klepa_core.host.supervisor import HostTiming
from klepa_core.host.turns import TurnRegistry

PEER = 2**51 + 7


class FakeAlerts:
    def __init__(self):
        self.raised = []

    def raise_(self, name, **fields):
        self.raised.append((name, fields))
        return True


class Gate:
    """Stands in for the supervisor: whether the host's sends may leave now."""

    def __init__(self, allowed=True):
        self.allowed = allowed

    def may_release(self):
        return self.allowed


def everything(host_message_id, kind):
    return True


@pytest.fixture
def setup(core_db, api):
    cfg, conn, journal = core_db
    clock = Clock()
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), PEER, clock=clock)
    turns = TurnRegistry(conn, queue, clock=clock)
    gate = Gate()
    notices = Outbox(conn, api, EventLog(conn))
    outbox = HostOutbox(
        conn,
        api,
        gate,
        queue,
        turns,
        notices,
        EventLog(conn),
        cfg.locale,
        clock=clock,
        timing=HostTiming(),
        alerts=FakeAlerts(),
    )
    return outbox, queue, journal, conn, gate, clock


def issue(queue, journal, update_id, sender, message_id):
    message = {"message_id": message_id, "date": 1, "chat": {"id": sender}, "from": {"id": sender}, "text": "hi"}
    journal.append_batch([{"update_id": update_id, "message": message}], "t")
    queue.enqueue(update_id, sender, message_id, sender, 1)
    queue.serve(None, 100, everything)


def rows(conn):
    return conn.execute("SELECT * FROM outbound WHERE origin='host' ORDER BY id").fetchall()


def test_accept_answers_at_once_with_an_id_of_cores_own_and_records_the_answer(setup):
    outbox, queue, journal, conn, gate, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    message_id = outbox.accept(OWNER, "hello", None)
    assert message_id > HELD_ID_BASE
    [row] = rows(conn)
    assert (row["state"], row["bot"], row["chat_id"]) == ("PENDING", "family", OWNER)
    assert queue.oldest_unanswered(OWNER) is None
    assert outbox.waiting(OWNER) == 1
    assert "host_send_held" not in EventLog(conn).kinds()
    gate.allowed = False
    outbox.accept(OWNER, "held", None)
    assert "host_send_held" in EventLog(conn).kinds()  # the trace of holding (scenario 38)


async def test_sends_leave_in_order_only_while_allowed_and_keep_no_text(setup, fake_tg):
    outbox, queue, journal, conn, gate, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    gate.allowed = False
    outbox.accept(OWNER, "one", None)
    outbox.accept(OWNER, "<b>two</b>", None)
    assert await outbox.release_due() == 0
    assert fake_tg.sent == []
    gate.allowed = True
    await outbox.release_due()
    await outbox.release_due()
    assert [item["params"]["text"] for item in fake_tg.sent] == ["one", "<b>two</b>"]
    params = fake_tg.sent[0]["params"]
    assert (params["parse_mode"], params["link_preview_options"]) == ("HTML", {"is_disabled": True})
    assert [(row["state"], row["payload"]) for row in rows(conn)] == [("CONFIRMED", "{}"), ("CONFIRMED", "{}")]


async def test_a_chat_waits_for_its_first_message_while_other_chats_go_on(setup, fake_tg):
    outbox, queue, journal, _, _, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    issue(queue, journal, 2, MEMBER, 20)
    fake_tg.fail("sendMessage", status=429, description="Too Many Requests", retry_after=60)
    outbox.accept(OWNER, "first", None)
    outbox.accept(OWNER, "second", None)
    outbox.accept(MEMBER, "other chat", None)
    await outbox.release_due()
    await outbox.release_due()
    assert [item["params"]["text"] for item in fake_tg.sent] == ["other chat"]


async def test_a_reply_to_a_held_message_gets_its_telegram_id(setup, fake_tg):
    outbox, queue, journal, _, _, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    first = outbox.accept(OWNER, "first", 10)
    await outbox.release_due()
    outbox.accept(OWNER, "second", first)
    await outbox.release_due()
    replies = [item["params"]["reply_parameters"]["message_id"] for item in fake_tg.sent]
    assert replies == [10, fake_tg.sent[0]["message"]["message_id"]]


async def test_held_too_long_the_chats_queue_is_dropped_with_one_apology(setup, fake_tg):
    outbox, queue, journal, conn, gate, clock = setup
    issue(queue, journal, 1, OWNER, 10)
    gate.allowed = False
    outbox.accept(OWNER, "one", None)
    clock.advance(300)
    outbox.accept(OWNER, "two", None)
    clock.advance(300)
    await outbox.release_due()
    assert [(row["state"], row["last_error"], row["payload"]) for row in rows(conn)] == [
        ("FAILED", "held_expired", "{}"),
        ("FAILED", "held_expired", "{}"),
    ]
    apologies = conn.execute("SELECT * FROM outbound WHERE idempotency_key LIKE 'held_dropped:%'").fetchall()
    assert [json.loads(row["payload"])["text"] for row in apologies] == [
        "I couldn't answer in time. Please send your message again."
    ]
    outbox.accept(OWNER, "three", None)
    clock.advance(600)
    await outbox.release_due()
    assert len(conn.execute("SELECT * FROM outbound WHERE idempotency_key LIKE 'held_dropped:%'").fetchall()) == 2
    gate.allowed = True
    await outbox.release_due()
    assert [item["params"]["text"] for item in fake_tg.sent] == []  # the held text never went


async def test_a_send_telegram_refuses_fails_with_an_apology_and_an_alert(setup, fake_tg):
    outbox, queue, journal, conn, _, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    fake_tg.fail("sendMessage", status=400, description="Bad Request: chat not found")
    outbox.accept(OWNER, "one", None)
    await outbox.release_due()
    [row] = rows(conn)
    assert (row["state"], row["last_error"], row["payload"]) == ("FAILED", "400", "{}")
    assert "host_send_failed" in EventLog(conn).kinds()
    assert outbox.alerts.raised == [("host_send_failed", {"error": "400"})]
    assert conn.execute("SELECT COUNT(*) FROM outbound WHERE idempotency_key LIKE 'held_dropped:%'").fetchone()[0] == 1


async def test_a_retry_that_lasts_past_the_limit_is_dropped_too(setup, fake_tg):
    outbox, queue, journal, conn, _, clock = setup
    issue(queue, journal, 1, OWNER, 10)
    fake_tg.fail("sendMessage", status=429, description="Too Many Requests", retry_after=1)
    outbox.accept(OWNER, "one", None)
    await outbox.release_due()
    assert rows(conn)[0]["state"] == "RETRY_WAIT"
    clock.advance(3 * 3600)  # the lid closed for three hours
    await outbox.release_due()
    assert (rows(conn)[0]["state"], rows(conn)[0]["last_error"]) == ("FAILED", "held_expired")
    assert [item["params"]["text"] for item in fake_tg.sent] == []


async def test_an_unexpected_error_never_leaves_a_send_blocking_its_chat(setup, monkeypatch):
    outbox, queue, journal, conn, _, _ = setup
    issue(queue, journal, 1, OWNER, 10)

    async def broken(*args, **kwargs):
        raise KeyError("message_id")

    monkeypatch.setattr(outbox.api, "send_message", broken)
    outbox.accept(OWNER, "one", None)
    await outbox.release_due()
    assert (rows(conn)[0]["state"], rows(conn)[0]["payload"]) == ("UNKNOWN", "{}")
    assert "host_send_error" in EventLog(conn).kinds()


def test_owns_only_its_own_ids_in_the_same_chat(setup):
    outbox, queue, journal, _, _, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    message_id = outbox.accept(OWNER, "one", None)
    assert outbox.owns(OWNER, message_id)
    assert not outbox.owns(MEMBER, message_id)
    assert not outbox.owns(OWNER, 10)
    assert not outbox.owns(OWNER, message_id + 1)


def test_finished_sends_lose_their_text_on_start(setup, core_db, api):
    _, queue, _, conn, gate, clock = setup
    cfg = core_db[0]
    conn.execute(
        "INSERT INTO outbound(idempotency_key, origin, method, chat_id, payload, state, created_at, updated_at) "
        "VALUES ('host:x', 'host', 'sendMessage', ?, '{\"text\": \"secret\"}', 'UNKNOWN', 't', 't')",
        (OWNER,),
    )
    turns = TurnRegistry(conn, queue, clock=clock)
    HostOutbox(conn, api, gate, queue, turns, Outbox(conn, api, EventLog(conn)), EventLog(conn), cfg.locale)
    assert conn.execute("SELECT payload FROM outbound WHERE idempotency_key='host:x'").fetchone()[0] == "{}"
