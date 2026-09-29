import pytest

from helpers import OWNER
from klepa_core import db
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox


@pytest.fixture
def outbox_factory(tmp_path):
    def make(api, clock=None):
        conn = db.connect(tmp_path / "core.db")
        db.migrate(conn)
        kwargs = {"clock": clock} if clock else {}
        return Outbox(conn, api, EventLog(conn), **kwargs)

    return make


def state(outbox, key):
    return outbox.conn.execute(
        "SELECT state, attempts, telegram_message_id FROM outbound WHERE idempotency_key=?", (key,)
    ).fetchone()


async def test_confirmed_with_message_id_and_idempotent_key(fake_tg, api, outbox_factory):
    outbox = outbox_factory(api)
    assert outbox.enqueue_text("k1", OWNER, "📄 got it", reply_to=3)
    assert not outbox.enqueue_text("k1", OWNER, "📄 got it", reply_to=3)
    assert await outbox.send_due() == 1
    row = state(outbox, "k1")
    assert row["state"] == "CONFIRMED"
    assert row["telegram_message_id"] == fake_tg.sent[-1]["message"]["message_id"]
    assert len(fake_tg.sent) == 1


async def test_not_sent_waits_and_retries(fake_tg, api, outbox_factory):
    now = [1000.0]
    outbox = outbox_factory(api, clock=lambda: now[0])
    outbox.enqueue_text("k2", OWNER, "x")
    await fake_tg.stop()
    await outbox.send_due()
    assert state(outbox, "k2")["state"] == "RETRY_WAIT"
    assert await outbox.send_due() == 0  # not due yet
    await fake_tg.start()  # same port
    now[0] += 1000
    assert await outbox.send_due() == 1
    assert state(outbox, "k2")["state"] == "CONFIRMED"


async def test_429_waits_retry_after(fake_tg, api, outbox_factory):
    now = [1000.0]
    outbox = outbox_factory(api, clock=lambda: now[0])
    outbox.enqueue_text("k3", OWNER, "x")
    fake_tg.fail("sendMessage", status=429, description="Too Many Requests", retry_after=30)
    await outbox.send_due()
    assert state(outbox, "k3")["state"] == "RETRY_WAIT"
    now[0] += 10
    assert await outbox.send_due() == 0
    now[0] += 25
    assert await outbox.send_due() == 1


async def test_ambiguous_becomes_unknown_and_is_not_retried(fake_tg, api, outbox_factory):
    outbox = outbox_factory(api)
    outbox.enqueue_text("k4", OWNER, "x")
    fake_tg.fail("sendMessage", drop=True)
    await outbox.send_due()
    assert state(outbox, "k4")["state"] == "UNKNOWN"
    await outbox.send_due()
    assert fake_tg.calls.count("sendMessage") == 1


async def test_bad_request_fails(fake_tg, api, outbox_factory):
    outbox = outbox_factory(api)
    outbox.enqueue_text("k5", OWNER, "x")
    fake_tg.fail("sendMessage", status=400, description="Bad Request: chat not found")
    await outbox.send_due()
    assert state(outbox, "k5")["state"] == "FAILED"


def test_recover_marks_sending_unknown(outbox_factory):
    outbox = outbox_factory(api=None)
    outbox.enqueue_text("k6", OWNER, "x")
    outbox.conn.execute("UPDATE outbound SET state='SENDING'")
    assert outbox.recover() == 1
    assert state(outbox, "k6")["state"] == "UNKNOWN"
