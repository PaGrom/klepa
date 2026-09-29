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
