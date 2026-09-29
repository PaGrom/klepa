import asyncio

import pytest

from klepa_core import db
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.gatekeeper.receipts import ReceiptBatcher, files_word, receipt_text
from klepa_core.journal import InboundJournal


@pytest.mark.parametrize(
    "n, words",
    [(1, "1 файл"), (2, "2 файла"), (4, "4 файла"), (5, "5 файлов"), (11, "11 файлов"), (14, "14 файлов"),
     (21, "21 файл"), (22, "22 файла"), (25, "25 файлов")],
)
def test_files_word(n, words):
    assert files_word(n) == words


def test_receipt_text():
    assert receipt_text(["voice"]) == "🎧 получила голосовое"
    assert receipt_text(["file"]) == "📄 получила"
    assert receipt_text(["voice", "voice"]) == "🎧 получила голосовые: 2"
    assert receipt_text(["file", "photo", "voice"]) == "📄 получила 3 файла"


async def test_batcher_sends_one_receipt_per_chat_after_quiet_window(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    journal = InboundJournal(tmp_path / "inbound.db")
    journal.append_batch([{"update_id": i} for i in (1, 2, 3, 4)], "t")
    batcher = ReceiptBatcher(Outbox(conn, None, EventLog(conn)), journal, window_seconds=0.1)
    batcher.add(111111, 1, 10, "file")
    batcher.add(111111, 2, 11, "file")
    batcher.add(222222, 3, 20, "voice")
    batcher.add(111111, 2, 11, "file")  # the same message again is ignored
    assert batcher.has_update(2) and batcher.pending_count() == 3
    await asyncio.sleep(0.3)
    rows = conn.execute("SELECT idempotency_key, chat_id, payload FROM outbound ORDER BY id").fetchall()
    assert [(r["idempotency_key"], r["chat_id"]) for r in rows] == [("receipt:111111:10", 111111),
                                                                  ("receipt:222222:20", 222222)]
    assert '"text": "📄 получила 2 файла"' in rows[0]["payload"]
    assert [update_id for update_id, _ in journal.pending()] == [4]


async def test_hold_keeps_a_slow_album_in_one_batch(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    journal = InboundJournal(tmp_path / "inbound.db")
    journal.append_batch([{"update_id": i} for i in (1, 2, 3)], "t")
    batcher = ReceiptBatcher(Outbox(conn, None, EventLog(conn)), journal, window_seconds=0.1)
    batcher.add(111111, 1, 10, "photo")
    batcher.hold(111111)  # the next photo takes longer than the window to download
    await asyncio.sleep(0.25)
    assert conn.execute("SELECT COUNT(*) FROM outbound").fetchone()[0] == 0
    batcher.add(111111, 2, 11, "photo")
    batcher.hold(111111)  # a third download fails and will be retried later
    batcher.release(111111)
    await asyncio.sleep(0.25)
    payloads = [row["payload"] for row in conn.execute("SELECT payload FROM outbound")]
    assert len(payloads) == 1 and '"text": "📄 получила 2 файла"' in payloads[0]


async def test_cancel_all_drops_batches_without_receipts(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    journal = InboundJournal(tmp_path / "inbound.db")
    journal.append_batch([{"update_id": 1}], "t")
    batcher = ReceiptBatcher(Outbox(conn, None, EventLog(conn)), journal, window_seconds=0.1)
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
    batcher = ReceiptBatcher(Outbox(conn, None, EventLog(conn)), journal, window_seconds=0.1)
    batcher.add(111111, 1, 10, "photo")
    batcher.hold(111111)  # a download starts ...
    batcher.add(111111, 2, 10, "photo")  # ... and brings the same message under a new update_id
    assert batcher.has_update(2)
    await asyncio.sleep(0.25)
    payloads = [row["payload"] for row in conn.execute("SELECT payload FROM outbound")]
    assert len(payloads) == 1 and '"text": "📄 получила",' in payloads[0]
    assert journal.pending() == []
