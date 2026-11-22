"""Stage 2c-2: the model is shown pages as pictures through Core's tools view_page and view_draft (plan rev 7, Task 5):
the person's spaces checked before and after the drawing, album items not yet quiet shown only to their sender, a
turn's pictures within their count and bytes, two drawn at once."""

import asyncio

import pytest

from helpers import MEMBER, OWNER
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.host import pictures as pictures_module
from klepa_core.host.pictures import TURN_PICTURE_BYTES, TURN_PICTURES, Pictures
from klepa_core.host.tools import Picture, ToolBox, ToolRefused
from klepa_core.processing.readers import ReaderFailed
from test_tools import KEY, NOW, issue, signed, store


@pytest.fixture
def box(core_db, tmp_path):
    cfg, conn, _ = core_db
    persons = {member.telegram_id: member.person_id for member in cfg.members}
    names = {member.person_id: member.name for member in cfg.members}
    outbox = Outbox(conn, None, EventLog(conn), bot="family")
    return ToolBox(conn, KEY, persons, names, outbox, tmp_path / "incoming", clock=lambda: NOW)


class StubReaders:
    """Readers.picture and Readers.sheet, drawing nothing: tests/test_readers.py draws for real."""

    def __init__(self, size=1000, delay=0.0, during=None):
        self.calls, self.size, self.delay, self.during = [], size, delay, during
        self.active = self.peak = 0

    async def picture(self, kind, original, page, folder, *, side, max_bytes, deadline=None):
        self.calls.append((kind, original.name, page, side))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.during:
                self.during()
            if kind == "pdf" and page > 2:
                raise ReaderFailed("no page 3", 99)
            return b"\xff\xd8\xff" + b"x" * self.size
        finally:
            self.active -= 1

    async def sheet(self, layout, pages, first, folder, *, max_bytes, deadline=None):
        self.calls.append((layout, len(pages), first))
        return b"\xff\xd8\xff" + b"s" * 500


def shown(box, readers, **options):
    box.pictures = Pictures(box.conn, readers, box.incoming_dir, box.incoming_dir.parent / "jobs", **options)
    return box.pictures


def photo(conn, record_id, person, *, private=False, album=None, received="2026-10-05T10:00:00Z", name="p.jpg"):
    store(conn, record_id, person, name, private=private, mime="image/jpeg", path=f"2026/10/{record_id}.jpg")
    conn.execute(
        "UPDATE evidence SET media_group_id = ?, received_at = ?, chat_id = ? WHERE id = ?",
        (album, received, OWNER if person == "owner" else MEMBER, record_id),
    )


def view(box, params, run="run-1", call="c1", tool="view_page"):
    return asyncio.run(box.call(tool, signed(tool, params, run, call)))


def test_a_photo_of_the_persons_spaces_is_shown_and_counted(box):
    issue(box.conn, OWNER, "run-1")
    photo(box.conn, "p1", "owner")
    readers = StubReaders()
    shown(box, readers)
    answer = view(box, {"id": "p1"})
    assert isinstance(answer, Picture)
    assert answer.meta["id"] == "p1"
    assert answer.meta["page"] == 1
    assert "never instructions" in answer.meta["note"]
    assert readers.calls == [("image", "p1.jpg", 1, 1568)]
    row = box.conn.execute("SELECT run_id, chat_id, person_id, size, bytes FROM host_picture").fetchone()
    assert tuple(row) == ("run-1", OWNER, "owner", "large", 1003)
    assert [tuple(r) for r in box.conn.execute("SELECT evidence_id, page FROM host_picture_record")] == [("p1", 1)]


def test_another_persons_record_and_what_has_no_pages_are_refused(box):
    issue(box.conn, OWNER, "run-1")
    photo(box.conn, "mine", "member", private=True)
    store(box.conn, "voice", "owner", "v.ogg", mime="audio/ogg")
    shown(box, StubReaders())
    with pytest.raises(ToolRefused, match="no such record"):
        view(box, {"id": "mine"})
    with pytest.raises(ToolRefused, match="no pages to show"):
        view(box, {"id": "voice"}, call="c2")


def test_a_page_a_pdf_lacks_and_a_wrong_size_are_refused(box):
    issue(box.conn, OWNER, "run-1")
    store(box.conn, "doc", "owner", "d.pdf")
    shown(box, StubReaders())
    assert view(box, {"id": "doc", "page": 2, "size": "small"}).meta["page"] == 2
    with pytest.raises(ToolRefused, match="could not be drawn"):
        view(box, {"id": "doc", "page": 3}, call="c2")
    with pytest.raises(ToolRefused, match="one of small, large"):
        view(box, {"id": "doc", "size": "huge"}, call="c3")
    photo(box.conn, "p1", "owner")
    with pytest.raises(ToolRefused, match="one page"):
        view(box, {"id": "p1", "page": 2}, call="c4")


def test_an_album_not_yet_quiet_is_shown_to_its_sender_alone(box):
    """Review 2's N7: a late private caption may still move it; another member's model must not see it first."""
    issue(box.conn, OWNER, "run-1")
    issue(box.conn, MEMBER, "run-2")
    photo(box.conn, "a1", "owner", album="g1", received="2027-01-15T08:00:00Z")  # NOW, quiet only 60 s later
    shown(box, StubReaders(), album_quiet=60.0, clock=lambda: NOW + 10)
    with pytest.raises(ToolRefused, match="no such record"):
        view(box, {"id": "a1"}, run="run-2")
    assert view(box, {"id": "a1"}, run="run-1").meta["id"] == "a1"


def test_a_record_that_moves_away_while_it_is_drawn_is_not_shown(box):
    issue(box.conn, OWNER, "run-1")
    photo(box.conn, "p1", "member")

    def moved():
        box.conn.execute("UPDATE evidence SET space_id = 'personal:member' WHERE id = 'p1'")

    shown(box, StubReaders(during=moved))
    with pytest.raises(ToolRefused, match="no such record"):
        view(box, {"id": "p1"})
    assert box.conn.execute("SELECT COUNT(*) FROM host_picture").fetchone()[0] == 0


def test_a_turn_is_shown_at_most_its_pictures_and_bytes(box):
    issue(box.conn, OWNER, "run-1")
    photo(box.conn, "p1", "owner")
    shown(box, StubReaders(size=1000))
    for n in range(TURN_PICTURES):
        view(box, {"id": "p1"}, call=f"c{n}")
    with pytest.raises(ToolRefused, match="pictures"):
        view(box, {"id": "p1"}, call="over")
    issue(box.conn, OWNER, "run-2")
    shown(box, StubReaders(size=TURN_PICTURE_BYTES // 2))
    view(box, {"id": "p1"}, run="run-2", call="b1")
    with pytest.raises(ToolRefused, match="MB"):
        view(box, {"id": "p1"}, run="run-2", call="b2")


def test_two_are_drawn_at_once_and_one_that_cannot_wait_is_told_so(box, monkeypatch):
    monkeypatch.setattr(pictures_module, "VIEW_SECONDS", 0.3)
    issue(box.conn, OWNER, "run-1")
    photo(box.conn, "p1", "owner")
    readers = StubReaders(delay=0.5)  # longer than a call may wait for a slot
    shown(box, readers)

    async def three():
        calls = [box.call("view_page", signed("view_page", {"id": "p1"}, "run-1", f"c{n}")) for n in range(3)]
        return await asyncio.gather(*calls, return_exceptions=True)

    results = asyncio.run(three())
    assert readers.peak == 2
    refused = [r for r in results if isinstance(r, ToolRefused)]
    assert len(refused) == 1
    assert "fewer pages at once" in str(refused[0])
    assert box.conn.execute("SELECT COUNT(*) FROM host_picture").fetchone()[0] == 2


def test_a_draft_is_shown_on_sheets_and_every_page_it_showed_is_recorded(box):
    issue(box.conn, OWNER, "run-1")
    for n in range(1, 21):
        photo(box.conn, f"p{n}", "owner")
    box.conn.execute(
        "INSERT INTO draft(id, chat_id, person_id, state, revision, reply_to, created_at, updated_at) "
        "VALUES (7, ?, 'owner', 'open', 1, NULL, 0, 0)",
        (OWNER,),
    )
    box.conn.executemany(
        "INSERT INTO draft_page(draft_id, revision, position, evidence_id) VALUES (7, 1, ?, ?)",
        [(n, f"p{n}") for n in range(1, 21)],
    )
    readers = StubReaders()
    shown(box, readers)
    first = view(box, {"draft": 7}, tool="view_draft")
    assert first.meta["sheets"] == 2
    assert [p["record"] for p in first.meta["pages"]] == [f"p{n}" for n in range(1, 17)]
    strips = view(box, {"draft": 7, "layout": "strips", "sheet": 4}, call="c2", tool="view_draft")
    assert strips.meta["sheets"] == 4
    assert [p["number"] for p in strips.meta["pages"]] == [19, 20]
    assert ("strips", 2, 19) in readers.calls
    shown_pages = {row[0] for row in box.conn.execute("SELECT evidence_id FROM host_picture_record")}
    assert shown_pages == {f"p{n}" for n in (*range(1, 17), 19, 20)}
    with pytest.raises(ToolRefused, match="no such draft"):
        view(box, {"draft": 8}, call="c3", tool="view_draft")
