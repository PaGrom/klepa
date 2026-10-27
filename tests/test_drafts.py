"""Drafts of documents from photos (spec 6.1 item 4, make_pdf): which batches make one, the order the model sets, the
PDF that follows its answer, and the PDF's versions. Core's renderer is a stand-in here; the acceptance tests run it.
Since the owner's choice of 2026-10-27 there is no sheet to check and no button."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from helpers import MEMBER, OWNER, Clock, host_peers, wait_until
from klepa_core.events import EventLog
from klepa_core.evidence import EvidenceStore
from klepa_core.gatekeeper.drafts import HOLD_SECONDS, NOTE, OFFER_SECONDS, DraftRefused, Drafts
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.host.queue import HostQueue
from klepa_core.processing.readers import OutputTooLarge, ReaderFailed

SPACES = ("shared", "personal:owner")


def asks(revision):
    """The end of the mark of a draft the batch made, while the model has neither ordered it nor closed it."""
    return (
        f"revision {revision}. Pass klepa__make_pdf with the draft, revision {revision} and order: the numbers of the "
        "pages in the order their content shows (how a text runs on, headings, signatures, page numbers), each page "
        "once, leaving out a photo that shows the same page as another; Core sends the person the PDF after your "
        "answer. In your answer say which photos you left out as repeats, and if what the pages say shows a page is "
        "missing (a gap in the page numbers, a text that breaks off), say which, so the person can send it. If the "
        "images show people, places or things, and the person's words do not ask for a document, pass not_a_document "
        f"with revision {revision} instead, and no PDF is made; little or no text is no sign of that, and when unsure, "
        "make the PDF; never name these numbers or records to the person⟧"
    )


class Renderer:
    """Core's renderer as the drafts call it: what it was given, and bytes that name the pages in order."""

    def __init__(self):
        self.calls = []
        self.fail = None

    async def to_png(self, source, target, job, *, max_side=None):
        self.calls.append(("png", source.name, max_side))
        target.write_bytes(b"png of " + source.read_bytes())

    async def pages(self, action, inputs, job, *, max_bytes):
        assert all(path.parent == job for path in inputs)  # copies in the job's own folder, never the originals
        if self.fail is not None:
            raise self.fail
        self.calls.append((action, [path.read_bytes() for path in inputs]))
        return f"{action}:".encode() + b",".join(path.read_bytes() for path in inputs)


@pytest.fixture
def d(core_db):
    cfg, conn, journal = core_db
    clock = Clock()
    events = EventLog(conn)
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), host_peers(cfg, 2**51 + 7), clock=clock)
    queue.drafts = True
    outbox = Outbox(conn, None, events, clock=clock)
    store = EvidenceStore(conn, cfg.incoming_dir, cfg.documents_dir, b"k" * 32, cfg.timezone, events)
    renderer = Renderer()
    drafts = Drafts(
        conn,
        queue,
        outbox,
        store,
        renderer,
        cfg.locale,
        events,
        jobs_dir=cfg.jobs_dir,
        hold_seconds=0.0,  # the PDF waits for the model's answer in the tests about that only
        clock=clock,
    )
    return SimpleNamespace(
        cfg=cfg, conn=conn, clock=clock, queue=queue, outbox=outbox, drafts=drafts, renderer=renderer
    )


def image(
    d,
    record_id,
    content,
    *,
    chat_id=OWNER,
    message_id=10,
    kind="photo",
    name=None,
    state="processed",
    text=None,
    space="shared",
    album=None,
    received=None,
):
    """An image as the store keeps it, its file in the incoming folder, read with this text."""
    person = {OWNER: "owner", MEMBER: "member"}[chat_id]
    suffix = ".jpg" if name is None else "." + name.rpartition(".")[2].lower()
    path = f"2026/10/{record_id}{suffix}"
    (d.cfg.incoming_dir / "2026/10").mkdir(parents=True, exist_ok=True)
    (d.cfg.incoming_dir / path).write_bytes(content)
    mime = "image/jpeg" if kind == "photo" else None
    received_at = "t" if received is None else datetime.fromtimestamp(received, UTC).isoformat()
    d.conn.execute(
        "INSERT INTO evidence(id, space_id, kind, original_name, disk_name, incoming_path, mime, size, sha256, "
        "received_at, message_date, channel, chat_id, message_id, update_id, authenticated_subject, ingest_key, "
        "state, media_group_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1700000000, 'telegram', ?, ?, ?, ?, ?, ?, ?)",
        (
            record_id,
            space,
            kind,
            name,
            record_id,
            path,
            mime,
            len(content),
            hashlib.sha256(content).hexdigest(),
            received_at,
            chat_id,
            message_id,
            message_id,
            person,
            f"test:{record_id}",
            state,
            album,
        ),
    )
    d.conn.execute(
        "INSERT INTO job(evidence_id, kind, state, pages, created_at) VALUES (?, 'image', 'done', 1, 1)", (record_id,)
    )
    if text:
        d.conn.execute(
            "INSERT INTO evidence_text(evidence_id, page, text, extractor, created_at) VALUES (?, 1, ?, 'test', 1)",
            (record_id, text),
        )


def album(d, *contents, chat_id=OWNER, first=10, prefix="ev"):
    """A batch of photos, read: their records are `<prefix><n>` and each says `text <content>`."""
    items = []
    for offset, content in enumerate(contents):
        record_id = f"{prefix}{first + offset}"
        image(d, record_id, content, chat_id=chat_id, message_id=first + offset, text=f"text {content.decode()}")
        items.append((first + offset, first + offset, record_id))
    return d.queue.enqueue_attachment(chat_id, items)


def outbound(d, key):
    row = d.conn.execute("SELECT * FROM outbound WHERE idempotency_key=?", (key,)).fetchone()
    return None if row is None else (row["method"], row["state"], json.loads(row["payload"]))


def sent(d, key, telegram_message_id):
    """Telegram took the send and gave it this id."""
    d.conn.execute(
        "UPDATE outbound SET state='CONFIRMED', telegram_message_id=? WHERE idempotency_key=?",
        (telegram_message_id, key),
    )


def draft_of(d, host_message_id):
    return d.conn.execute("SELECT draft_id FROM host_message WHERE id=?", (host_message_id,)).fetchone()[0]


def pages(d, draft_id):
    return [
        row[0]
        for row in d.conn.execute(
            "SELECT p.evidence_id FROM draft_page p JOIN draft r ON r.id = p.draft_id AND r.revision = p.revision "
            "WHERE r.id=? ORDER BY p.position",
            (draft_id,),
        )
    ]


def ordered(d, draft, order, *, revision=None, add=None, reply_to=None):
    """The model's make_pdf for the person."""
    return d.drafts.change(
        "owner", OWNER, SPACES, draft_id=draft, order=order, add=add, reply_to=reply_to, revision=revision
    )


def state(d, draft):
    return d.conn.execute("SELECT state FROM draft WHERE id=?", (draft,)).fetchone()[0]


def served(d):
    return d.queue.serve(None, 100, lambda *_: True)


# ---- which batches make a draft -------------------------------------------------------------------------------------
async def test_two_photos_of_a_batch_make_a_draft_in_the_order_they_came_once_their_reading_ended(d):
    message = album(d, b"one", b"two", b"three")
    d.conn.execute("UPDATE job SET state='running' WHERE evidence_id='ev12'")
    d.drafts.tick()
    assert draft_of(d, message) is None  # still read: not looked at yet
    d.conn.execute("UPDATE job SET state='done' WHERE evidence_id='ev12'")
    d.drafts.tick()
    draft = draft_of(d, message)
    assert pages(d, draft) == ["ev10", "ev11", "ev12"]
    row = d.conn.execute("SELECT * FROM draft WHERE id=?", (draft,)).fetchone()
    assert (row["chat_id"], row["person_id"], row["state"], row["revision"], row["reply_to"], row["ordered"]) == (
        OWNER,
        "owner",
        "open",
        1,
        10,
        None,
    )


async def test_one_photo_makes_no_draft_and_neither_does_a_cancelled_batch(d):
    one = album(d, b"single")
    cancelled = album(d, b"a", b"b", first=20)
    d.conn.execute("UPDATE host_message SET cancelled_at=1, answered_at=1 WHERE id=?", (cancelled,))
    d.drafts.tick()
    assert (draft_of(d, one), draft_of(d, cancelled)) == (None, None)
    assert d.conn.execute("SELECT COUNT(*) FROM draft").fetchone()[0] == 0
    checked = d.conn.execute("SELECT draft_checked FROM host_message ORDER BY id").fetchall()
    assert [row[0] for row in checked] == [1, 1]  # looked at once


async def test_a_photo_sent_twice_in_one_batch_is_one_page(d):
    message = album(d, b"same", b"other", b"same")
    d.drafts.tick()
    assert pages(d, draft_of(d, message)) == ["ev10", "ev11"]


async def test_files_that_are_not_images_are_no_pages(d):
    image(d, "ev10", b"scan one", kind="file", name="scan.PNG", text="first")
    image(d, "ev11", b"%PDF", kind="file", name="report.pdf")
    image(d, "ev12", b"scan two", kind="file", name="IMG_1.heic", text="second")
    message = d.queue.enqueue_attachment(OWNER, [(10, 10, "ev10"), (11, 11, "ev11"), (12, 12, "ev12")])
    d.drafts.tick()
    assert pages(d, draft_of(d, message)) == ["ev10", "ev12"]


async def test_the_batch_waits_for_the_host_until_it_was_looked_at_and_its_text_names_the_draft(d):
    message = album(d, b"one", b"two")
    assert served(d) == []
    d.drafts.tick()
    [update] = served(d)
    draft = draft_of(d, message)
    assert update["message"]["text"].endswith(
        f"⟦document draft {draft} · 2 pages made from the images above, in the order they came: 1 record ev10, 2 "
        f"record ev11; {asks(1)}"
    )


async def test_without_drafts_the_queue_does_not_wait(d):
    d.queue.drafts = False
    album(d, b"one", b"two")
    assert len(served(d)) == 1


async def test_a_batch_given_to_the_host_before_drafts_makes_none(d):
    d.queue.drafts = False
    message = album(d, b"one", b"two")
    served(d)
    d.queue.drafts = True
    d.drafts.tick()
    assert draft_of(d, message) is None
    assert d.conn.execute("SELECT draft_checked FROM host_message WHERE id=?", (message,)).fetchone()[0] == 1


async def test_a_photo_sent_alone_later_is_offered_to_the_draft_and_the_model_adds_it(d):
    """The owner's word of 2026-10-27: the person sends the missing page later, and the PDF is made anew. The photo
    joins nothing itself: the model adds it, where it goes, when the person's words or the page say it belongs."""
    first = album(d, b"one", b"three")
    d.drafts.tick()
    draft = draft_of(d, first)
    ordered(d, draft, [1, 2], revision=1)
    await d.drafts.run_once()  # version 1
    d.clock.advance(OFFER_SECONDS - 5)
    more = album(d, b"two", first=20)
    d.drafts.tick()
    assert draft_of(d, more) == draft
    assert pages(d, draft) == ["ev10", "ev11"]  # nothing joined
    texts = [update["message"]["text"] for update in served(d)]
    assert texts[-1].endswith(
        f"⟦document draft {draft} · the chat's latest document, of 2 pages: 1 record ev10, 2 record ev11; revision 2. "
        "If the image above is a page of it, as the person's words or what it shows say (a page that was missing, "
        "say), pass klepa__make_pdf with the draft, revision 2, add with the image's record, and order with every page "
        "in the order their content shows, the image's number being 3; Core then sends the new PDF after your answer. "
        "Otherwise leave the draft as it is; never name these numbers or records to the person⟧"
    )
    result = ordered(d, draft, [1, 3, 2], revision=2, add=["ev20"], reply_to=20)
    assert result["records"] == ["ev10", "ev20", "ev11"]
    await d.drafts.run_once()
    assert d.renderer.calls[-1] == ("pdf", [b"one", b"two", b"three"])
    assert outbound(d, f"draft_pdf:{draft}:2")[2]["reply_to"] == 20  # a reply to the photo


async def test_a_photo_sent_alone_after_an_hour_or_by_another_person_is_offered_nothing(d):
    first = album(d, b"one", b"two")
    d.drafts.tick()
    theirs = album(d, b"three", chat_id=MEMBER, first=20, prefix="m")
    d.drafts.tick()
    assert draft_of(d, theirs) is None
    d.clock.advance(OFFER_SECONDS + 1)
    lone = album(d, b"four", first=30)
    d.drafts.tick()
    assert draft_of(d, lone) is None
    assert pages(d, draft_of(d, first)) == ["ev10", "ev11"]


async def test_an_album_sent_soon_after_is_a_document_of_its_own(d):
    """The final review's M2: a passport, then a visa form a few minutes later, are two documents."""
    first = album(d, b"passport one", b"passport two")
    d.drafts.tick()
    d.clock.advance(60)
    second = album(d, b"form one", b"form two", first=20)
    d.drafts.tick()
    assert draft_of(d, second) not in (None, draft_of(d, first))
    assert pages(d, draft_of(d, first)) == ["ev10", "ev11"]


# ---- the PDF after the model's answer --------------------------------------------------------------------------------
async def test_the_pdf_of_the_order_the_model_set_follows_its_answer_as_a_reply_to_the_first_photo(d):
    """The owner's choice of 2026-10-27: no sheet, no buttons; the PDF comes, built from copies of the originals."""
    d.drafts.hold = HOLD_SECONDS
    message = album(d, b"one", b"two", b"three")
    d.drafts.tick()
    served(d)  # the host has the batch
    draft = draft_of(d, message)
    await d.drafts.run_once()
    assert d.renderer.calls == []  # the model has not ordered it
    result = ordered(d, draft, [3, 1, 2], revision=1)
    assert result == {"draft": draft, "pages": 3, "records": ["ev12", "ev10", "ev11"], "pdf": NOTE}
    await d.drafts.run_once()
    assert d.renderer.calls == []  # the model has not answered
    d.queue.mark_answered(OWNER, prefer=message)
    await d.drafts.run_once()
    assert d.renderer.calls == [("pdf", [b"three", b"one", b"two"])]
    version = d.conn.execute("SELECT * FROM document_version WHERE draft_id=?", (draft,)).fetchone()
    record = d.conn.execute("SELECT * FROM evidence WHERE id=?", (version["evidence_id"],)).fetchone()
    assert (version["version"], version["revision"]) == (1, 2)
    assert (record["kind"], record["mime"], record["channel"], record["space_id"]) == (
        "file",
        "application/pdf",
        "core",
        "shared",
    )
    assert record["original_name"] == "Document 2027-01-15.pdf"  # the day the draft was made, in Core's time zone
    assert json.loads(record["derived_from"]) == ["ev12", "ev10", "ev11"]
    assert (d.cfg.incoming_dir / record["incoming_path"]).read_bytes() == b"pdf:three,one,two"
    texts = d.conn.execute("SELECT page, text FROM evidence_text WHERE evidence_id=? ORDER BY page", (record["id"],))
    assert [tuple(row) for row in texts] == [(1, "text three"), (2, "text one"), (3, "text two")]
    method, _, payload = outbound(d, f"draft_pdf:{draft}:1")
    assert (method, payload["reply_to"], payload["caption"], payload["name"]) == (
        "sendDocument",
        10,
        "📄 Done, pages: 3.",
        "Document 2027-01-15.pdf",
    )
    assert payload["sha256"] == hashlib.sha256(b"pdf:three,one,two").hexdigest()
    assert state(d, draft) == "done"
    assert not list(d.cfg.jobs_dir.iterdir())  # the copies went with the job's folder
    assert d.conn.execute("SELECT COUNT(*) FROM outbound").fetchone()[0] == 1  # the PDF alone: no sheet
    await d.drafts.run_once()
    assert len(d.renderer.calls) == 1  # once


async def test_the_model_leaves_out_a_repeat_and_the_pdf_has_each_page_once(d):
    """The owner's word: two photos of one page are one page; the model leaves the repeat out and says so."""
    message = album(d, b"one", b"one again", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    result = ordered(d, draft, [1, 3], revision=1)
    assert result["records"] == ["ev10", "ev12"]
    await d.drafts.run_once()
    assert d.renderer.calls == [("pdf", [b"one", b"two"])]


async def test_a_draft_the_model_never_orders_makes_no_pdf(d):
    """Only the model says a batch is a document and in which order: a batch it said nothing of makes nothing."""
    message = album(d, b"one", b"two")
    d.drafts.tick()
    served(d)
    d.queue.mark_answered(OWNER, prefer=message)
    d.clock.advance(HOLD_SECONDS + 1)
    await d.drafts.run_once()
    assert d.renderer.calls == []
    assert state(d, draft_of(d, message)) == "open"


async def test_a_pdf_the_model_ordered_and_never_answered_for_comes_after_ten_minutes(d):
    d.drafts.hold = HOLD_SECONDS
    message = album(d, b"one", b"two")
    d.drafts.tick()
    served(d)
    ordered(d, draft_of(d, message), [2, 1], revision=1)
    await d.drafts.run_once()
    assert d.renderer.calls == []
    d.clock.advance(HOLD_SECONDS + 1)
    await d.drafts.run_once()
    assert d.renderer.calls == [("pdf", [b"two", b"one"])]


async def test_the_hold_lasts_while_a_run_of_the_model_in_the_chat_is_alive(d):
    """The review's I2 and the owner's word: a model ordering many pages of small text, or a provider OpenClaw retries,
    may take long; and a change for the person's words waits for the answer of the run that made it."""
    assert HOLD_SECONDS == 600.0
    d.drafts.hold = HOLD_SECONDS
    message = album(d, b"one", b"two")
    d.drafts.tick()
    served(d)
    d.conn.execute(
        "INSERT INTO host_run(run_id, boot_id, chat_id, sender_id, prompt_built_at, host_message_id, registered_at, "
        "expires_at) VALUES ('run-1', 'boot', ?, ?, ?, ?, ?, ?)",
        (OWNER, OWNER, d.clock(), message, d.clock(), d.clock() + HOLD_SECONDS + 300),
    )
    ordered(d, draft_of(d, message), [1, 2], revision=1)
    d.clock.advance(HOLD_SECONDS + 1)
    await d.drafts.run_once()
    assert d.renderer.calls == []  # the run is alive
    d.conn.execute("UPDATE host_run SET ended_at=?", (d.clock(),))
    await d.drafts.run_once()
    assert d.renderer.calls == []  # its answer is on its way, until the run's registration expires
    d.clock.advance(300)
    await d.drafts.run_once()
    assert d.renderer.calls == [("pdf", [b"one", b"two"])]


async def test_the_hold_lasts_while_a_run_on_a_later_message_of_the_chat_is_alive(d):
    """The gateway may join the batch's long text and the person's next message in one run, paired with the later
    message: its answer answers the batch too, so the PDF waits for it."""
    d.drafts.hold = HOLD_SECONDS
    message = album(d, b"one", b"two")
    d.drafts.tick()
    served(d)
    later = d.conn.execute(
        "INSERT INTO host_message(kind, chat_id, message_id, sender_id, date, created_at) "
        "VALUES ('text', ?, 99, ?, 1, ?)",
        (OWNER, OWNER, d.clock()),
    ).lastrowid
    d.conn.execute(
        "INSERT INTO host_update(update_id, host_message_id, issued_at) VALUES (9999, ?, ?)", (later, d.clock())
    )
    d.conn.execute(
        "INSERT INTO host_run(run_id, boot_id, chat_id, sender_id, prompt_built_at, host_message_id, registered_at, "
        "expires_at, ended_at) VALUES ('run-1', 'boot', ?, ?, ?, ?, ?, ?, ?)",
        (OWNER, OWNER, d.clock(), later, d.clock(), d.clock() + HOLD_SECONDS + 300, d.clock()),
    )
    ordered(d, draft_of(d, message), [1, 2], revision=1)
    d.clock.advance(HOLD_SECONDS + 1)
    await d.drafts.run_once()
    assert d.renderer.calls == []  # the batch waits for that run's answer
    d.queue.mark_answered(OWNER, prefer=later)
    await d.drafts.run_once()
    assert d.renderer.calls == [("pdf", [b"one", b"two"])]


async def test_a_heic_page_is_turned_into_a_png_first(d):
    image(d, "ev10", b"heic one", kind="file", name="IMG_1.HEIC")
    image(d, "ev11", b"jpeg two")
    message = d.queue.enqueue_attachment(OWNER, [(10, 10, "ev10"), (11, 11, "ev11")])
    d.drafts.tick()
    ordered(d, draft_of(d, message), [1, 2], revision=1)
    await d.drafts.run_once()
    assert d.renderer.calls == [("png", "page-1.heic", 3508), ("pdf", [b"png of heic one", b"jpeg two"])]


async def test_a_pdf_that_cannot_be_drawn_tells_the_person_and_keeps_the_photos(d):
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    ordered(d, draft, [1, 2], revision=1)
    d.renderer.fail = ReaderFailed("pages exited with 3")
    await d.drafts.run_once()
    assert state(d, draft) == "failed"
    method, _, payload = outbound(d, f"draft_failed:{draft}:2")
    assert (method, payload["text"], payload["reply_to"]) == (
        "sendMessage",
        "I couldn't make the PDF. The photos are saved.",
        10,
    )
    assert d.conn.execute("SELECT COUNT(*) FROM evidence WHERE state='processed'").fetchone()[0] == 2
    event = d.conn.execute("SELECT data FROM event_log WHERE kind='draft_failed'").fetchone()[0]
    assert json.loads(event) == {"draft": draft, "revision": 2, "stage": "pdf", "error": "pages exited with 3"}


async def test_a_button_of_a_sheet_sent_before_no_longer_works(d):
    """The sheets Core sent before 2026-10-27 keep their buttons on the person's screen."""
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    for what in ("draft_done", "draft_order", "draft_no"):
        assert d.drafts.press(f"{what}:{draft}:1", OWNER).text == "This button no longer works."
    assert state(d, draft) == "open"


# ---- make_pdf -------------------------------------------------------------------------------------------------------
async def test_photos_added_are_numbered_after_the_pages_and_a_new_draft_starts_from_photos(d):
    message = album(d, b"one", b"two")
    image(d, "ev30", b"later")
    image(d, "ev31", b"other later")
    d.drafts.tick()
    draft = draft_of(d, message)
    result = ordered(d, draft, None, add=["ev30", "ev10"])
    assert result == {
        "draft": draft,
        "pages": 3,
        "records": ["ev10", "ev11", "ev30"],
        "left_out": "1 photo(s) of the same bytes as another page",
        "pdf": NOTE,
    }
    result = ordered(d, draft, [4, 1, 2], add=["ev31"])
    assert result["records"] == ["ev31", "ev10", "ev11"]  # the added photo first, and ev30 left out
    fresh = d.drafts.change("owner", OWNER, SPACES, draft_id=None, order=None, add=["ev31", "ev30"], reply_to=50)
    assert fresh["draft"] != draft
    assert pages(d, fresh["draft"]) == ["ev31", "ev30"]
    await d.drafts.run_once()
    assert outbound(d, f"draft_pdf:{fresh['draft']}:1")[2]["reply_to"] == 50


@pytest.mark.parametrize(
    ("kwargs", "refusal"),
    [
        ({"order": [1, 3]}, "the pages are numbered 1 to 2"),
        ({"order": [0]}, "the pages are numbered 1 to 2"),
        ({"order": [1, 1]}, "a page appears twice in order"),
        ({"order": []}, "a document needs one page at least"),
        ({"add": ["ev99"]}, "no such record"),
        ({"add": ["theirs"]}, "no such record"),
        ({"add": ["pdf"]}, "only photos and images can be pages"),
        ({"draft_id": None, "order": [1]}, "order needs a draft; for a new one, give its photos in add"),
        ({"draft_id": None}, "give the photos of the new document in add"),
    ],
)
async def test_changes_that_cannot_be_made_are_refused(d, kwargs, refusal):
    message = album(d, b"one", b"two")
    image(d, "theirs", b"not yours", chat_id=MEMBER, message_id=60, space="personal:member")
    image(d, "pdf", b"%PDF", kind="file", name="report.pdf")
    d.drafts.tick()
    change = {"draft_id": draft_of(d, message), "order": None, "add": None} | kwargs
    with pytest.raises(DraftRefused, match=f"^{refusal}$"):
        d.drafts.change("owner", OWNER, SPACES, reply_to=None, **change)
    assert d.conn.execute("SELECT MAX(revision) FROM draft").fetchone()[0] == 1


async def test_another_persons_draft_and_a_draft_being_built_are_not_changed(d):
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    with pytest.raises(DraftRefused, match=r"^no such draft$"):
        d.drafts.change(
            "member", MEMBER, ("shared", "personal:member"), draft_id=draft, order=[1], add=None, reply_to=None
        )
    d.conn.execute("UPDATE draft SET state='building'")
    with pytest.raises(DraftRefused, match="being made"):
        ordered(d, draft, [1])


async def test_numbers_of_an_older_revision_are_refused_with_the_draft_as_it_is(d):
    """The review's I1: the draft changed while the model read it, or the turn was retried after a restart."""
    message = album(d, b"one", b"two", b"three")
    d.drafts.tick()
    draft = draft_of(d, message)
    ordered(d, draft, [1, 2, 3], revision=1)
    with pytest.raises(DraftRefused) as refused:
        ordered(d, draft, [3, 1, 2], revision=1)
    assert str(refused.value) == (
        "the draft has changed: it is revision 2 now, its pages 1 record ev10, 2 record ev11, 3 record ev12"
    )
    assert ordered(d, draft, [3, 1, 2], revision=2)["records"] == ["ev12", "ev10", "ev11"]


# ---- versions -------------------------------------------------------------------------------------------------------
async def test_a_change_after_the_pdf_builds_version_2_and_only_the_latest_is_found(d):
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    ordered(d, draft, [1, 2], revision=1)
    await d.drafts.run_once()
    ordered(d, draft, [2, 1], reply_to=60)  # the person's words
    await d.drafts.run_once()
    versions = d.conn.execute("SELECT version, evidence_id FROM document_version ORDER BY version").fetchall()
    assert [row["version"] for row in versions] == [1, 2]
    second = d.conn.execute("SELECT * FROM evidence WHERE id=?", (versions[1]["evidence_id"],)).fetchone()
    assert second["original_name"] == "Document 2027-01-15, version 2.pdf"
    assert (d.cfg.incoming_dir / second["incoming_path"]).read_bytes() == b"pdf:two,one"
    indexed = {row[0] for row in d.conn.execute("SELECT DISTINCT evidence_id FROM evidence_fts")}
    assert versions[1]["evidence_id"] in indexed
    assert versions[0]["evidence_id"] not in indexed  # the older version stays, and leaves search
    assert d.conn.execute("SELECT COUNT(*) FROM evidence WHERE id=?", (versions[0]["evidence_id"],)).fetchone()[0] == 1
    assert outbound(d, f"draft_pdf:{draft}:2")[2]["reply_to"] == 60


async def test_a_document_with_a_personal_page_is_personal(d):
    image(d, "ev10", b"shared page", text="a")
    image(d, "ev11", b"private page", space="personal:owner", text="b")
    message = d.queue.enqueue_attachment(OWNER, [(10, 10, "ev10"), (11, 11, "ev11")])
    d.drafts.tick()
    ordered(d, draft_of(d, message), [1, 2], revision=1)
    await d.drafts.run_once()
    space = d.conn.execute("SELECT e.space_id FROM document_version v JOIN evidence e ON e.id = v.evidence_id")
    assert space.fetchone()[0] == "personal:owner"


async def test_a_pdf_too_large_for_telegram_is_not_made_and_the_person_hears_why(d):
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    ordered(d, draft, [1, 2], revision=1)
    d.renderer.fail = OutputTooLarge("document.pdf is larger than 50 MB")
    await d.drafts.run_once()
    assert state(d, draft) == "failed"
    payload = outbound(d, f"draft_failed:{draft}:2")[2]
    assert payload["text"].startswith("I couldn't make the PDF: it would be larger than 50 MB")
    assert payload["reply_to"] == 10
    assert d.conn.execute("SELECT COUNT(*) FROM document_version").fetchone()[0] == 0


async def test_a_build_a_crash_interrupted_is_done_once_after_the_restart(d):
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    ordered(d, draft, [1, 2], revision=1)
    await d.drafts._due()
    await d.drafts._build(d.conn.execute("SELECT * FROM draft").fetchone())  # the record is made...
    d.conn.execute("DELETE FROM document_version")  # ...and the crash came before the version was written
    d.conn.execute("UPDATE draft SET state='building'")
    d.conn.execute("DELETE FROM outbound WHERE idempotency_key LIKE 'draft_pdf:%'")
    await d.drafts.run_once()
    assert d.renderer.calls.count(("pdf", [b"one", b"two"])) == 1  # not drawn again
    assert d.conn.execute("SELECT COUNT(*) FROM evidence WHERE channel='core'").fetchone()[0] == 1
    assert d.conn.execute("SELECT version FROM document_version").fetchone()[0] == 1
    assert outbound(d, f"draft_pdf:{draft}:1") is not None


async def test_a_file_put_in_place_of_the_built_pdf_is_never_sent(d, api, fake_tg):
    """Scenario 46: the PDF Core built goes out, or nothing does; never another file at its path."""
    d.outbox.api = api
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    ordered(d, draft, [1, 2], revision=1)
    await d.drafts.run_once()
    record = d.conn.execute("SELECT e.* FROM document_version v JOIN evidence e ON e.id = v.evidence_id").fetchone()
    path = d.cfg.incoming_dir / record["incoming_path"]
    path.unlink()
    path.write_bytes(b"%PDF-1.4 another document")
    await d.outbox.send_due()
    assert fake_tg.documents == []
    assert outbound(d, f"draft_pdf:{draft}:1")[1] == "FAILED"


def test_a_draft_holds_as_many_pages_as_the_renderer_draws_and_a_heic_page_is_a4():
    from klepa_core.gatekeeper import drafts
    from klepa_core.processing import pages as renderer

    assert (drafts.MAX_PAGES, drafts.PDF_SIDE) == (renderer.MAX_PAGES, renderer.MAX_SIDE)


async def test_one_drafts_unexpected_error_fails_it_alone_and_the_next_draft_gets_its_pdf(d):
    first = album(d, b"one", b"two")
    second = album(d, b"three", b"four", chat_id=MEMBER, first=20, prefix="m")
    d.drafts.tick()
    ordered(d, draft_of(d, first), [1, 2], revision=1)
    d.drafts.change(
        "member",
        MEMBER,
        ("shared", "personal:member"),
        draft_id=draft_of(d, second),
        order=[1, 2],
        add=None,
        reply_to=None,
        revision=1,
    )
    original = d.renderer.pages

    async def breaks_once(action, inputs, job, *, max_bytes):
        d.renderer.pages = original
        raise RuntimeError("a bug")

    d.renderer.pages = breaks_once
    await d.drafts.run_once()
    states = d.conn.execute("SELECT id, state FROM draft ORDER BY id").fetchall()
    assert [row["state"] for row in states] == ["failed", "done"]
    event = d.conn.execute("SELECT data FROM event_log WHERE kind='draft_failed'").fetchone()[0]
    assert json.loads(event)["error"] == "RuntimeError"  # its type only: never what it said
    assert (
        outbound(d, f"draft_failed:{draft_of(d, first)}:2")[2]["text"]
        == "I couldn't make the PDF. The photos are saved."
    )


async def test_a_page_changed_on_disk_is_never_built(d):
    """What the model ordered is what is built: a page's bytes are checked against the record before each drawing."""
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    (d.cfg.incoming_dir / "2026/10/ev11.jpg").write_bytes(b"another page")
    ordered(d, draft, [1, 2], revision=1)
    await d.drafts.run_once()
    assert state(d, draft) == "failed"
    assert d.renderer.calls == []
    assert d.conn.execute("SELECT COUNT(*) FROM document_version").fetchone()[0] == 0
    event = d.conn.execute("SELECT data FROM event_log WHERE kind='draft_failed'").fetchone()[0]
    assert json.loads(event)["error"] == "page 2 is not its stored original"


async def test_deciding_never_waits_for_drawing(d):
    """A long PDF being drawn holds no chat's batch: deciding and drawing are loops of their own."""
    import asyncio

    release = asyncio.Event()
    original = d.renderer.pages

    async def slow(action, inputs, job, *, max_bytes):
        await release.wait()
        return await original(action, inputs, job, max_bytes=max_bytes)

    d.renderer.pages = slow
    stop = asyncio.Event()
    loops = [asyncio.create_task(d.drafts.run(stop)), asyncio.create_task(d.drafts.draw(stop))]
    try:
        first = album(d, b"one", b"two")
        await wait_until(lambda: draft_of(d, first) is not None)
        ordered(d, draft_of(d, first), [1, 2], revision=1)
        await wait_until(lambda: state(d, draft_of(d, first)) == "building")
        later = album(d, b"three", b"four", chat_id=MEMBER, first=20, prefix="m")  # while the first PDF is drawn
        d.drafts.wake.set()
        await wait_until(lambda: draft_of(d, later) is not None)
    finally:
        release.set()
        stop.set()
        await asyncio.gather(*loops)


async def test_a_link_a_sandboxed_step_leaves_in_the_job_folder_is_never_written_through(d, tmp_path):
    """Review 1's C1: ffmpeg, broken by a crafted HEIC page, may leave a link where Core is to put the next page."""
    outside = tmp_path / "outside" / "planted"
    outside.parent.mkdir()
    image(d, "ev10", b"heic one", kind="file", name="IMG_1.heic")
    image(d, "ev11", b"jpeg two")
    message = d.queue.enqueue_attachment(OWNER, [(10, 10, "ev10"), (11, 11, "ev11")])
    original = d.renderer.to_png

    async def broken(source, target, job, *, max_side=None):
        planted = job / "page-2.jpg"
        planted.unlink(missing_ok=True)
        planted.symlink_to(outside)
        await original(source, target, job, max_side=max_side)

    d.renderer.to_png = broken
    d.drafts.tick()
    ordered(d, draft_of(d, message), [1, 2], revision=1)
    await d.drafts.run_once()
    assert not outside.exists()  # Core wrote nothing through it
    assert state(d, draft_of(d, message)) == "failed"


async def test_a_pdf_waits_until_its_album_is_quiet(d):
    """Review 1's I4: an album's late item may still make it personal, so a PDF of its photos waits for the quiet."""
    first = d.clock() - 5
    for offset, content in enumerate((b"one", b"two")):
        image(d, f"ev{10 + offset}", content, message_id=10 + offset, album="a", received=first)
    message = d.queue.enqueue_attachment(OWNER, [(10, 10, "ev10"), (11, 11, "ev11")])
    d.drafts.tick()
    draft = draft_of(d, message)
    ordered(d, draft, [1, 2], revision=1)
    await d.drafts.run_once()
    assert state(d, draft) == "building"  # 5 of 60 quiet seconds
    d.clock.advance(56)
    await d.drafts.run_once()
    assert state(d, draft) == "done"


async def test_a_stop_cancels_the_drawing_under_way_and_its_draft_is_drawn_again(d):
    """The final review's I1: a long drawing must not keep Core from stopping (launchd would kill it, and the owner
    would hear of a crash); the draft is left as it was, so the next start draws its PDF."""
    import asyncio

    started = asyncio.Event()

    async def held(action, inputs, job, *, max_bytes):
        started.set()
        await asyncio.sleep(3600)

    d.renderer.pages = held
    message = album(d, b"one", b"two")
    d.drafts.tick()
    ordered(d, draft_of(d, message), [1, 2], revision=1)
    stop = asyncio.Event()
    drawing = asyncio.create_task(d.drafts.draw(stop))
    await asyncio.wait_for(started.wait(), 5)
    stop.set()
    await asyncio.wait_for(drawing, 2)
    assert d.conn.execute("SELECT COUNT(*) FROM document_version").fetchone()[0] == 0
    assert state(d, draft_of(d, message)) == "building"  # built at the next start


async def test_a_page_no_longer_the_persons_to_use_is_never_built(d):
    """The final review's I2: a shared photo the owner added may become another member's own (a late private caption,
    or stage 3's make_private); from then on Core draws it for nobody else."""
    image(d, "ev10", b"mine")
    image(d, "theirs", b"their photo", chat_id=MEMBER, message_id=60)
    result = d.drafts.change("owner", OWNER, SPACES, draft_id=None, order=None, add=["ev10", "theirs"], reply_to=50)
    d.conn.execute("UPDATE evidence SET space_id='personal:member' WHERE id='theirs'")
    await d.drafts.run_once()
    assert d.conn.execute("SELECT state FROM draft").fetchone()[0] == "failed"
    assert d.renderer.calls == []
    event = d.conn.execute("SELECT data FROM event_log WHERE kind='draft_failed'").fetchone()[0]
    assert json.loads(event)["error"] == "page 2 is no longer the person's to use"
    assert d.conn.execute("SELECT COUNT(*) FROM document_version").fetchone()[0] == 0
    assert result["pages"] == 2


async def test_a_change_leaves_out_a_page_no_longer_the_persons_to_use(d):
    image(d, "ev10", b"mine")
    image(d, "theirs", b"their photo", chat_id=MEMBER, message_id=60)
    draft = d.drafts.change("owner", OWNER, SPACES, draft_id=None, order=None, add=["ev10", "theirs"], reply_to=50)
    d.conn.execute("UPDATE evidence SET space_id='personal:member' WHERE id='theirs'")
    result = ordered(d, draft["draft"], [2, 1], reply_to=51)
    assert result == {
        "draft": draft["draft"],
        "pages": 1,
        "records": ["ev10"],
        "left_out": "1 photo(s) no longer the person's to use",
        "pdf": NOTE,
    }


async def test_a_mark_served_after_the_model_ordered_the_draft_says_so(d):
    """A turn retried after a restart reads that the model ordered the draft already, not to order it again."""
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    ordered(d, draft, [2, 1], revision=1)
    [update] = served(d)
    assert update["message"]["text"].endswith(
        f"⟦document draft {draft} · 2 pages made from the images above, in the order they came: 1 record ev10, 2 "
        "record ev11; you have ordered it already, and Core sends the PDF after your answer; never name these numbers "
        "or records to the person⟧"
    )


# ---- the model tells a document from photos of the family ----------------------------------------------------------
async def test_photos_the_model_finds_no_document_get_no_pdf(d):
    """The owner's choice of 2026-10-26: holiday photos get no PDF; the model reads that they are no document."""
    d.drafts.hold = HOLD_SECONDS
    message = album(d, b"beach", b"mountains")
    d.drafts.tick()
    served(d)
    draft = draft_of(d, message)
    result = d.drafts.change(
        "owner", OWNER, SPACES, draft_id=draft, order=None, add=None, reply_to=None, revision=1, not_a_document=True
    )
    assert result == {
        "draft": draft,
        "not_a_document": True,
        "note": "make_pdf with the draft and an order opens it again",
    }
    d.queue.mark_answered(OWNER, prefer=message)
    d.clock.advance(HOLD_SECONDS + 1)
    await d.drafts.run_once()
    assert d.renderer.calls == []  # no PDF, ever
    assert state(d, draft) == "declined"
    assert d.conn.execute("SELECT COUNT(*) FROM outbound").fetchone()[0] == 0  # nothing said either
    event = d.conn.execute("SELECT data FROM event_log WHERE kind='draft_declined'").fetchone()[0]
    assert json.loads(event) == {"by": "model", "draft": draft, "revision": 1}
    lone = album(d, b"three", first=30)
    d.drafts.tick()
    assert draft_of(d, lone) is None  # a declined draft is offered no more photos


async def test_a_draft_with_a_page_not_read_is_never_closed_as_no_document(d):
    """The review's I2: no text because no reader read it (none installed, or it failed) is no sign of anything."""
    message = album(d, b"one", b"two")
    d.drafts.tick()
    d.conn.execute("UPDATE job SET state='failed' WHERE evidence_id='ev11'")
    with pytest.raises(DraftRefused, match=r"^a page was not read: make the PDF, and the person decides$"):
        d.drafts.change(
            "owner",
            OWNER,
            SPACES,
            draft_id=draft_of(d, message),
            order=None,
            add=None,
            reply_to=None,
            revision=1,
            not_a_document=True,
        )


async def test_a_draft_closed_as_no_document_opens_again_with_an_order(d):
    """The review's M1: the way back, when the person says it is a document after all."""
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    d.drafts.change(
        "owner", OWNER, SPACES, draft_id=draft, order=None, add=None, reply_to=None, revision=1, not_a_document=True
    )
    ordered(d, draft, [1, 2], revision=1)
    await d.drafts.run_once()
    assert state(d, draft) == "done"
    assert d.renderer.calls == [("pdf", [b"one", b"two"])]


async def test_a_mark_served_after_the_model_closed_the_draft_says_so(d):
    """The review's M4: a turn retried after a restart reads that the draft is closed, not that a PDF will come."""
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    d.drafts.change(
        "owner", OWNER, SPACES, draft_id=draft, order=None, add=None, reply_to=None, revision=1, not_a_document=True
    )
    [update] = served(d)
    assert update["message"]["text"].endswith(
        f"⟦document draft {draft} · 2 pages made from the images above, in the order they came: 1 record ev10, 2 "
        "record ev11; it is closed as no document, and no PDF is made; klepa__make_pdf with the draft and an order "
        "opens it⟧"
    )


@pytest.mark.parametrize(
    ("kwargs", "refusal"),
    [
        ({"order": [2, 1]}, "not_a_document goes alone: no order, no add"),
        ({"add": ["ev10"]}, "not_a_document goes alone: no order, no add"),
        ({"draft_id": None}, "not_a_document needs a draft"),
        ({"revision": 5}, "the draft has changed: it is revision 1 now, its pages 1 record ev10, 2 record ev11"),
        ({}, "not_a_document needs the draft's revision"),
    ],
)
async def test_not_a_document_is_refused_with_anything_else(d, kwargs, refusal):
    message = album(d, b"one", b"two")
    d.drafts.tick()
    change = {"draft_id": draft_of(d, message), "order": None, "add": None, "revision": None} | kwargs
    with pytest.raises(DraftRefused) as refused:
        d.drafts.change("owner", OWNER, SPACES, reply_to=None, not_a_document=True, **change)
    assert str(refused.value) == refusal


async def test_a_document_being_built_or_built_is_a_document(d):
    message = album(d, b"one", b"two")
    d.drafts.tick()
    draft = draft_of(d, message)
    for draft_state, refusal in (
        ("building", "its PDF is being made: try again in a moment"),
        ("done", "its PDF is made"),
    ):
        d.conn.execute("UPDATE draft SET state=?", (draft_state,))
        with pytest.raises(DraftRefused, match=f"^{refusal}"):
            d.drafts.change(
                "owner",
                OWNER,
                SPACES,
                draft_id=draft,
                order=None,
                add=None,
                reply_to=None,
                revision=1,
                not_a_document=True,
            )
