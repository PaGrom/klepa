"""Stage 2c-2: the background reader (plan rev 7, Task 6). A reading is a turn of a space's reader peer; Core itself
decides what such a turn may do: look at its own record's page and save its text, for the record's owner, nothing
else."""

import asyncio

import pytest

from helpers import OWNER, SPACES, reader_of
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.host.tools import ToolBox, ToolRefused
from test_pictures import StubReaders, photo, shown, view
from test_tools import KEY, NOW, signed, store


@pytest.fixture
def box(core_db, tmp_path):
    cfg, conn, _ = core_db
    persons = {member.telegram_id: member.person_id for member in cfg.members}
    names = {member.person_id: member.name for member in cfg.members}
    outbox = Outbox(conn, None, EventLog(conn), bot="family")
    tools = ToolBox(conn, KEY, persons, names, outbox, tmp_path / "incoming", clock=lambda: NOW)
    tools.readers = {reader_of(space): space for space in SPACES}
    return tools


def reading_run(conn, evidence_id, page, space, run_id="read-1"):
    """A reading, its message in the space's reader chat, and the reader's registered turn for it."""
    peer = reader_of(space)
    reading = conn.execute(
        "INSERT INTO reading(evidence_id, page, space_id, state, queued_at) VALUES (?, ?, ?, 'running', ?)",
        (evidence_id, page, space, NOW),
    ).lastrowid
    message = conn.execute(
        "INSERT INTO host_message(source_update_id, kind, chat_id, message_id, sender_id, date, created_at) "
        "VALUES (NULL, 'reading', ?, (SELECT COALESCE(MAX(message_id), 0) + 1 FROM host_message), ?, 1, ?)",
        (peer, peer, NOW),
    ).lastrowid
    conn.execute("UPDATE reading SET host_message_id=? WHERE id=?", (message, reading))
    conn.execute(
        "INSERT INTO host_run(run_id, boot_id, chat_id, sender_id, prompt_built_at, host_message_id, registered_at, "
        "expires_at) VALUES (?, 'boot', ?, ?, ?, ?, ?, ?)",
        (run_id, peer, peer, NOW, message, NOW, NOW + 600),
    )
    return reading, message


def test_a_reading_looks_at_its_own_page_for_the_records_owner(box):
    photo(box.conn, "p1", "member", private=True)  # the member's personal photo: the owner's turns never see it
    reading_run(box.conn, "p1", 1, "personal:member")
    shown(box, StubReaders())
    answer = view(box, {"id": "p1", "size": "large"}, run="read-1")
    assert answer.meta["id"] == "p1"
    row = box.conn.execute("SELECT person_id, chat_id FROM host_picture").fetchone()
    assert (row["person_id"], row["chat_id"]) == ("member", reader_of("personal:member"))


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("get", {"id": "p1"}),
        ("search", {"query": "insurance"}),
        ("send_original", {"id": "p1"}),
        ("make_pdf", {"add": ["p1"]}),
        ("view_page", {"id": "p2"}),
        ("view_page", {"id": "p1", "page": 2}),
        ("view_draft", {"draft": 1}),
    ],
)
def test_a_reading_may_do_nothing_else(box, tool, params):
    photo(box.conn, "p1", "owner")
    photo(box.conn, "p2", "owner")
    reading_run(box.conn, "p1", 1, "shared")
    shown(box, StubReaders())
    with pytest.raises(ToolRefused, match="reading"):
        box.call(tool, signed(tool, params, "read-1", "c1"))  # refused before any tool runs


def test_a_superseded_attempt_of_a_reading_may_do_nothing(box):
    photo(box.conn, "p1", "owner")
    reading, _ = reading_run(box.conn, "p1", 1, "shared")
    box.conn.execute("UPDATE reading SET host_message_id = NULL WHERE id = ?", (reading,))
    shown(box, StubReaders())
    with pytest.raises(ToolRefused, match="over"):
        asyncio.run(box.call("view_page", signed("view_page", {"id": "p1"}, "read-1", "c1")))


def test_a_reading_of_a_record_that_left_its_owners_spaces_is_refused(box):
    store(box.conn, "x", "owner", "x.jpg", mime="image/jpeg")
    box.conn.execute("UPDATE evidence SET space_id = 'personal:member', authenticated_subject = 'owner' WHERE id = 'x'")
    reading_run(box.conn, "x", 1, "shared")
    shown(box, StubReaders())
    with pytest.raises(ToolRefused, match="no such record"):
        asyncio.run(box.call("view_page", signed("view_page", {"id": "x"}, "read-1", "c1")))
    assert OWNER  # the owner sent it; a member's personal space is not theirs
