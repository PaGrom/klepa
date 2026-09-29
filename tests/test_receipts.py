import asyncio

from klepa_core import db
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.gatekeeper.receipts import ReceiptBatcher
from klepa_core.journal import InboundJournal
from klepa_core.locale import load_locale

EN = load_locale("en")


async def test_batcher_sends_one_receipt_per_chat_after_quiet_window(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    journal = InboundJournal(tmp_path / "inbound.db")
    journal.append_batch([{"update_id": i} for i in (1, 2, 3, 4)], "t")
    batcher = ReceiptBatcher(Outbox(conn, None, EventLog(conn)), journal, window_seconds=0.1, locale=EN)
    batcher.add(111111, 1, 10, "file")
    batcher.add(111111, 2, 11, "file")
    batcher.add(222222, 3, 20, "voice")
    batcher.add(111111, 2, 11, "file")  # the same message again is ignored
    assert batcher.has_update(2)
    assert batcher.pending_count() == 3
    await asyncio.sleep(0.3)
    rows = conn.execute("SELECT idempotency_key, chat_id, payload FROM outbound ORDER BY id").fetchall()
    assert [(r["idempotency_key"], r["chat_id"]) for r in rows] == [
        ("receipt:111111:10", 111111),
        ("receipt:222222:20", 222222),
    ]
    assert '"text": "📄 got 2 files"' in rows[0]["payload"]
    assert [update_id for update_id, _ in journal.pending()] == [4]


async def test_hold_keeps_a_slow_album_in_one_batch(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    journal = InboundJournal(tmp_path / "inbound.db")
    journal.append_batch([{"update_id": i} for i in (1, 2, 3)], "t")
    batcher = ReceiptBatcher(Outbox(conn, None, EventLog(conn)), journal, window_seconds=0.1, locale=EN)
    batcher.add(111111, 1, 10, "photo")
    batcher.hold(111111)  # the next photo takes longer than the window to download
    await asyncio.sleep(0.25)
    assert conn.execute("SELECT COUNT(*) FROM outbound").fetchone()[0] == 0
    batcher.add(111111, 2, 11, "photo")
    batcher.hold(111111)  # a third download fails and will be retried later
    batcher.release(111111)
    await asyncio.sleep(0.25)
    payloads = [row["payload"] for row in conn.execute("SELECT payload FROM outbound")]
    assert len(payloads) == 1
    assert '"text": "📄 got 2 files"' in payloads[0]


async def test_cancel_all_drops_batches_without_receipts(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    journal = InboundJournal(tmp_path / "inbound.db")
    journal.append_batch([{"update_id": 1}], "t")
    batcher = ReceiptBatcher(Outbox(conn, None, EventLog(conn)), journal, window_seconds=0.1, locale=EN)
    batcher.add(111111, 1, 10, "file")
    batcher.cancel_all()
    await asyncio.sleep(0.2)
    assert conn.execute("SELECT COUNT(*) FROM outbound").fetchone()[0] == 0
    assert journal.state(1) == "new"


async def test_repeated_message_after_hold_still_closes_the_batch(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    journal = InboundJournal(tmp_path / "inbound.db")
    journal.append_batch([{"update_id": i} for i in (1, 2)], "t")
    batcher = ReceiptBatcher(Outbox(conn, None, EventLog(conn)), journal, window_seconds=0.1, locale=EN)
    batcher.add(111111, 1, 10, "photo")
    batcher.hold(111111)  # a download starts ...
    batcher.add(111111, 2, 10, "photo")  # ... and brings the same message under a new update_id
    assert batcher.has_update(2)
    await asyncio.sleep(0.25)
    payloads = [row["payload"] for row in conn.execute("SELECT payload FROM outbound")]
    assert len(payloads) == 1
    assert '"text": "📄 got it",' in payloads[0]
    assert journal.pending() == []
