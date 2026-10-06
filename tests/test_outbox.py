import functools
import json

import pytest

from helpers import OWNER
from klepa_core import db
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox, tell_failed_original


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


async def test_each_bot_sends_only_its_own_messages(fake_tg, api, outbox_factory):
    family = outbox_factory(api)
    service = Outbox(family.conn, api, EventLog(family.conn), bot="service")
    markup = {"inline_keyboard": [[{"text": "Status", "callback_data": "x"}]]}
    family.enqueue_text("f1", OWNER, "family text")
    service.enqueue_text("s1", OWNER, "service text", reply_markup=markup)
    assert await service.send_due() == 1
    assert [item["params"]["text"] for item in fake_tg.sent] == ["service text"]
    assert fake_tg.sent[0]["params"]["reply_markup"] == markup
    assert service.message_id("s1") == fake_tg.sent[0]["message"]["message_id"]
    assert service.message_id("missing") is None
    assert await family.send_due() == 1
    assert [item["params"]["text"] for item in fake_tg.sent] == ["service text", "family text"]
    assert service.events.kinds().count("service_sent") == 1  # every service send is logged, family sends are not


def document(path, data, name="Διαβατήριο scan.pdf"):
    import hashlib

    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mime": "application/pdf",
        "name": name,
    }


async def test_an_original_goes_out_as_its_stored_bytes_with_its_name(fake_tg, api, outbox_factory, tmp_path):
    original = tmp_path / "x.pdf"
    original.write_bytes(b"%PDF original")
    outbox = outbox_factory(api)
    assert outbox.enqueue_document("original:r:1", OWNER, document(original, b"%PDF original"))
    assert await outbox.send_due() == 1
    [sent] = fake_tg.documents
    assert (sent["name"], sent["data"], int(sent["params"]["chat_id"])) == (
        "Διαβατήριο scan.pdf",
        b"%PDF original",
        OWNER,
    )
    assert state(outbox, "original:r:1")["state"] == "CONFIRMED"


async def test_a_file_changed_after_it_was_stored_is_never_sent(fake_tg, api, outbox_factory, tmp_path):
    """Spec 6.5 and scenario 46: the bytes are read once and checked against the record; another file at the same
    path is refused, never sent."""
    original = tmp_path / "x.pdf"
    original.write_bytes(b"%PDF a swapped file")
    outbox = outbox_factory(api)
    outbox.enqueue_document("original:r:1", OWNER, document(original, b"%PDF original"))
    assert await outbox.send_due() == 0
    assert fake_tg.documents == []
    assert state(outbox, "original:r:1")["state"] == "FAILED"


async def test_a_send_that_breaks_fails_alone_and_the_outbox_goes_on(fake_tg, api, outbox_factory, tmp_path):
    """A name that cannot go into a request, here with a line break, fails that send only: one row never stops
    Core, which would leave it UNKNOWN after the restart and stop again at the next request for it."""
    original = tmp_path / "x.pdf"
    original.write_bytes(b"%PDF original")
    outbox = outbox_factory(api)
    outbox.enqueue_document("original:r:1", OWNER, document(original, b"%PDF original", name="line\nbreak.pdf"))
    outbox.enqueue_text("k", OWNER, "after it")
    assert await outbox.send_due() == 1
    assert state(outbox, "original:r:1")["state"] == "FAILED"
    assert [item["params"]["text"] for item in fake_tg.sent] == ["after it"]
    assert "outbound_failed" in outbox.events.kinds()


async def test_the_person_hears_when_their_original_could_not_be_sent(fake_tg, api, outbox_factory, tmp_path):
    """The model has already said the file is on its way; a send that ends FAILED is told to the person, once, in
    Core's words."""
    original = tmp_path / "x.pdf"
    original.write_bytes(b"%PDF a swapped file")
    outbox = outbox_factory(api)
    outbox.on_failed = functools.partial(tell_failed_original, outbox, "could not send {name}")
    outbox.enqueue_document("original:r:1", OWNER, document(original, b"%PDF original"))
    outbox.enqueue_text("k", OWNER, "a text")
    fake_tg.fail("sendMessage", status=400, description="Bad Request: chat not found")
    await outbox.send_due()
    assert state(outbox, "k")["state"] == "FAILED"  # Core's own text that failed is not told
    notices = outbox.conn.execute("SELECT idempotency_key, chat_id, payload FROM outbound WHERE state='PENDING'")
    assert [(row["idempotency_key"], row["chat_id"], json.loads(row["payload"])["text"]) for row in notices] == [
        ("failed:original:r:1", OWNER, "could not send Διαβατήριο scan.pdf")
    ]
