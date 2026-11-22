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


# ---- the worker ----
class Gate:
    """The supervisor as the reader asks it."""

    def __init__(self):
        self.running = True
        self.paused = False

    def may_release(self):
        return self.running and not self.paused


class Resets:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    async def __call__(self, peer):
        self.calls.append(peer)
        return self.ok


class Alerts:
    def __init__(self):
        self.raised = []

    def raise_(self, name, **fields):
        self.raised.append(name)
        return True


@pytest.fixture
def worker(core_db):
    from helpers import Clock, host_peers
    from klepa_core.host.queue import HostQueue
    from klepa_core.host.reader import Reader

    cfg, conn, journal = core_db
    clock = Clock()
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), host_peers(cfg, 2**51 + 7), clock=clock)
    gate, resets, alerts = Gate(), Resets(), Alerts()
    reader = Reader(conn, queue, gate, resets, EventLog(conn), alerts=alerts, album_quiet=60.0, clock=clock)
    return reader, conn, clock, gate, resets, alerts, queue


def states(conn):
    return {row["evidence_id"] + f"#{row['page']}": row["state"] for row in conn.execute("SELECT * FROM reading")}


def saved(conn, reading_id, evidence_id, page=1):
    conn.execute(
        "INSERT INTO evidence_text(evidence_id, page, text, extractor, created_at, saved_by_reading) "
        "VALUES (?, ?, 'read', 'model', 0, ?)",
        (evidence_id, page, reading_id),
    )


def test_a_reading_is_issued_while_the_host_runs_and_ends_with_its_turn(worker):
    reader, conn, _clock, gate, resets, _, _queue = worker
    photo(conn, "p1", "owner")
    reader.queue_reading("p1", 1)
    gate.running = False
    asyncio.run(reader.step())
    assert states(conn) == {"p1#1": "queued"}
    gate.running = True
    asyncio.run(reader.step())
    assert states(conn) == {"p1#1": "running"}
    reading, message = conn.execute("SELECT id, host_message_id FROM reading").fetchone()
    assert conn.execute("SELECT chat_id FROM host_message WHERE id=?", (message,)).fetchone()[0] == reader_of("shared")
    saved(conn, reading, "p1")
    reader.on_reply(message, ok=True, kind=None)
    asyncio.run(reader.step())
    assert states(conn) == {"p1#1": "done"}
    assert resets.calls == [reader_of("shared")]  # the session is reset before the peer's next reading
    assert conn.execute("SELECT answered_at FROM host_message WHERE id=?", (message,)).fetchone()[0] is not None


def test_a_reading_that_saved_nothing_is_tried_again_with_a_new_message_then_fails_and_is_queued_at_start(worker):
    reader, conn, _clock, _, _resets, _alerts, _ = worker
    photo(conn, "p1", "owner")
    reader.queue_reading("p1", 1)
    messages = []
    for _ in range(3):
        asyncio.run(reader.step())
        message = conn.execute("SELECT host_message_id FROM reading").fetchone()[0]
        messages.append(message)
        reader.on_reply(message, ok=True, kind=None)  # the turn reached the model and saved nothing
        asyncio.run(reader.step())
    assert len(set(messages)) == 3
    assert states(conn) == {"p1#1": "failed"}
    reader.recover()
    assert states(conn) == {"p1#1": "queued"}


def test_a_busy_provider_backs_the_reading_off(worker):
    reader, conn, clock, _, _, _, _ = worker
    photo(conn, "p1", "owner")
    reader.queue_reading("p1", 1)
    asyncio.run(reader.step())
    message = conn.execute("SELECT host_message_id FROM reading").fetchone()[0]
    reader.on_reply(message, ok=False, kind="rate_limit")
    asyncio.run(reader.step())
    assert states(conn) == {"p1#1": "queued"}
    asyncio.run(reader.step())
    assert states(conn) == {"p1#1": "queued"}  # not before its back-off
    clock.advance(61)
    asyncio.run(reader.step())
    assert states(conn) == {"p1#1": "running"}


def test_a_turn_the_gateway_never_starts_counts_apart_and_ends_held_with_an_alert(worker, monkeypatch):
    from klepa_core.host import reader as reader_module

    reader, conn, clock, _, _, alerts, _ = worker
    photo(conn, "p1", "owner")
    reader.queue_reading("p1", 1)
    for _ in range(reader_module.UNCOUNTED_ATTEMPTS):
        asyncio.run(reader.step())
        clock.advance(reader_module.READ_SECONDS + 1)
        reader.tick_running(reader_module.READ_SECONDS + 1)
        asyncio.run(reader.step())
    assert states(conn) == {"p1#1": "failed"}
    assert "reading_stuck" in alerts.raised
    assert conn.execute("SELECT attempts FROM reading").fetchone()[0] == 0


def test_a_peer_whose_reset_failed_gets_no_reading(worker):
    reader, conn, _clock, _, resets, alerts, _ = worker
    photo(conn, "p1", "owner")
    photo(conn, "p2", "owner")
    reader.queue_reading("p1", 1)
    reader.queue_reading("p2", 1)
    asyncio.run(reader.step())
    reading, message = conn.execute("SELECT id, host_message_id FROM reading WHERE evidence_id='p1'").fetchone()
    saved(conn, reading, "p1")
    reader.on_reply(message, ok=True, kind=None)
    resets.ok = False
    for _ in range(5):
        asyncio.run(reader.step())
    assert states(conn)["p2#1"] == "queued"
    assert "reader_reset_failed" in alerts.raised
    resets.ok = True
    asyncio.run(reader.step())
    asyncio.run(reader.step())
    assert states(conn)["p2#1"] == "running"


def test_people_go_first_and_photos_before_deep_pages(worker):
    reader, conn, clock, _, _, _, _ = worker
    store(conn, "scan", "owner", "s.pdf")
    for page in (1, 2, 3):
        reader.queue_reading("scan", page)
    photo(conn, "p1", "owner")
    reader.queue_reading("p1", 1)
    conn.execute(
        "INSERT INTO host_run(run_id, boot_id, chat_id, sender_id, prompt_built_at, registered_at, expires_at) "
        "VALUES ('person', 'b', ?, ?, ?, ?, ?)",
        (OWNER, OWNER, clock(), clock(), clock() + 600),
    )
    asyncio.run(reader.step())
    assert "running" not in states(conn).values()  # a person's turn is live
    conn.execute("UPDATE host_run SET ended_at = ? WHERE run_id = 'person'", (clock(),))
    asyncio.run(reader.step())
    running = [key for key, state in states(conn).items() if state == "running"]
    assert running in (["scan#1"], ["p1#1"])  # first pages first: a page 2 waits for every page 1


def test_an_album_not_yet_quiet_is_read_later(worker):
    reader, conn, clock, _, _, _, _ = worker
    photo(conn, "a1", "owner", album="g1", received="2027-01-15T08:00:00Z")  # helpers.Clock starts here
    reader.queue_reading("a1", 1)
    asyncio.run(reader.step())
    assert states(conn) == {"a1#1": "queued"}
    clock.advance(61)
    asyncio.run(reader.step())
    assert states(conn) == {"a1#1": "running"}
