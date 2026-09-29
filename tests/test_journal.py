import sqlite3

import pytest

from klepa_core.journal import InboundJournal


def u(update_id, **fields):
    return {"update_id": update_id, "message": {"message_id": update_id, **fields}}


def test_append_returns_only_new_ids(tmp_path):
    journal = InboundJournal(tmp_path / "j" / "inbound.db")
    assert journal.append_batch([u(5), u(6)], "t") == [5, 6]
    assert journal.append_batch([u(6), u(7)], "t") == [7]


def test_pending_mark_and_offset(tmp_path):
    journal = InboundJournal(tmp_path / "inbound.db")
    assert journal.next_offset() is None
    journal.append_batch([u(5), u(6)], "t")
    journal.mark(5, "done")
    assert [update_id for update_id, _ in journal.pending()] == [6]
    assert journal.state(5) == "done"
    assert journal.next_offset() == 7


def test_journal_survives_reopen(tmp_path):
    path = tmp_path / "inbound.db"
    journal = InboundJournal(path)
    journal.append_batch([u(9, text="hi")], "t")
    journal.close()
    again = InboundJournal(path)
    assert again.pending()[0][1]["message"]["text"] == "hi"


def test_mark_many_marks_all_or_nothing(tmp_path):
    journal = InboundJournal(tmp_path / "inbound.db")
    journal.append_batch([u(1), u(2), u(3)], "t")
    journal.conn.execute(
        "CREATE TRIGGER power_loss BEFORE UPDATE ON inbound_update WHEN NEW.update_id = 2 "
        "BEGIN SELECT RAISE(ABORT, 'power loss'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="power loss"):
        journal.mark_many([1, 2, 3], "done")
    assert [journal.state(i) for i in (1, 2, 3)] == ["new", "new", "new"]


def test_last_received_at(tmp_path):
    journal = InboundJournal(tmp_path / "inbound.db")
    assert journal.last_received_at() is None
    journal.append_batch([u(1)], "2026-10-05T07:00:00.000+00:00")
    journal.append_batch([u(2)], "2026-10-05T08:00:00.000+00:00")
    assert journal.last_received_at() == "2026-10-05T08:00:00.000+00:00"
