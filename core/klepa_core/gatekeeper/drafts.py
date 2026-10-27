"""Documents from photos (spec 6.1 item 4, 10 make_pdf; scenarios 37, 46, 48).

Two or more images in one batch are a draft of a document. When the batch's reading ends, the batch's message tells
the model of the draft and numbers its pages; the model puts them in the order their content shows through make_pdf,
each page once, leaving out a photo that repeats another, and says in its answer which page seems missing; or it says
they are no document. Once the model has answered, Core sends the person the PDF of the pages in that order: no sheet
to check, no buttons (the owner's choice of 2026-10-27: "as simple as can be"). The person says in words what to
change, or sends the missing photo, which the model adds; each change is a revision, and its PDF a new version of the
document, a record Core derived, whose text is its pages' texts and whose space is the most private of theirs. The
originals stay as they are, and search finds only the latest version. PDFs are drawn in the sandbox by Core's own
renderer, as readers read files: Core never decodes a person's image itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ..aio import until_stopped
from ..db import transaction
from ..events import EventLog
from ..evidence import EvidenceStore
from ..host.queue import HostQueue
from ..locale import Locale
from ..processing.jobs import job_kind, remove
from ..processing.readers import OutputTooLarge, ReaderFailed, ReaderMissing, Readers
from ..processing.sandbox import ProcessingStopped
from .outbox import Outbox

MIN_PAGES = 2  # images in one batch that make a draft
# A PDF waits for the model's answer to its batch, or to the person's words it changed the draft for: while the
# model's run is alive, and at most this long after the host took the batch (the owner's word, 2026-10-26: many pages
# of small text take a model long; ten minutes is also when Core alerts the owner to a silent model).
HOLD_SECONDS = 600.0
MAX_PAGES = 100  # pages of one draft, as Core's renderer draws them (processing.pages.MAX_PAGES)
PDF_SIDE = 3508  # pixels: a HEIC page for the PDF, A4 at 300 dpi (processing.pages.MAX_SIDE)
MAX_PDF_BYTES = 50 << 20  # what Telegram takes from a bot as a document
TICK_SECONDS = 0.5
USABLE = ("stored", "processing", "processed", "failed")  # the records whose original Core keeps
_HEIC = frozenset({".heic", ".heif"})
_SUFFIX = re.compile(r"\.[a-z0-9]{1,8}")
_RENDER_ERRORS = (ReaderFailed, ReaderMissing, ProcessingStopped, OSError)
NOTE = (
    "Core sends the person the PDF of these pages after your answer: say it is coming, never that it was sent; say "
    "which photos you left out as repeats, if any, and which page seems missing, if any"
)


class DraftRefused(Exception):
    """A change make_pdf does not make. Its text goes back to the model and never carries a record's content."""


def _image(row: sqlite3.Row) -> bool:
    """An image whose original Core keeps: a photo, or a file of an image's type, HEIC too."""
    return (
        job_kind(row["kind"], row["mime"], row["original_name"]) == "image"
        and row["state"] in USABLE
        and bool(row["incoming_path"])
    )


def _distinct(rows: Sequence[sqlite3.Row]) -> list[sqlite3.Row]:
    """Each original once, by its bytes: a photo sent twice is one page, where it first came."""
    seen: set[str] = set()
    kept = []
    for row in rows:
        if str(row["sha256"]) not in seen:
            seen.add(str(row["sha256"]))
            kept.append(row)
    return kept


def _spaces(person_id: str) -> tuple[str, str]:
    """The spaces whose records a person may use: the shared one and their own."""
    return ("shared", f"personal:{person_id}")


def _space(pages: Sequence[sqlite3.Row]) -> str:
    """The most private of the pages' spaces: a document with one personal page is personal."""
    return next((str(page["space_id"]) for page in pages if page["space_id"] != "shared"), "shared")


def _copy_checked(original: Path, copy: Path, sha256: str, number: int) -> None:
    """A page's copy for drawing, if its bytes are the record's: what the person confirmed is what is built. The copy
    is a new file: never a link, never one that was there."""
    data = original.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ReaderFailed(f"page {number} is not its stored original")
    fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data)


def _error(exc: BaseException) -> str:
    """The error for the event log: a reader's own words name only a program and a code, others only their type."""
    return str(exc) if isinstance(exc, ReaderFailed | ReaderMissing | ProcessingStopped) else type(exc).__name__


class Drafts:
    def __init__(
        self,
        conn: sqlite3.Connection,
        queue: HostQueue,
        outbox: Outbox,
        store: EvidenceStore,
        readers: Readers,
        locale: Locale,
        events: EventLog,
        *,
        jobs_dir: Path,
        album_quiet_seconds: float = 60.0,
        hold_seconds: float = HOLD_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.conn = conn
        self.queue = queue
        self.outbox = outbox
        self.store = store
        self.readers = readers
        self.locale = locale
        self.events = events
        self.jobs_dir = jobs_dir
        self.album_quiet = album_quiet_seconds  # a late item may still make an album personal (#15)
        self.hold = hold_seconds
        self.clock = clock
        # Each loop's own wake-up (2b-2's M1): deciding never waits for drawing, which may take a minute.
        self.wake = asyncio.Event()  # deciding: batches to look at
        self.drawing = asyncio.Event()  # drawing: PDFs to build

    # ---- the batches that make drafts -------------------------------------------------------------------------------
    def tick(self) -> None:
        """Look at each batch whose reading ended, once: two or more images make a draft. A batch the person cancelled
        makes none (scenario 37), nor one the host was given before Core made drafts."""
        rows = self.conn.execute(
            "SELECT m.*, u.update_id AS issued FROM host_message m LEFT JOIN host_update u ON u.host_message_id = m.id "
            "WHERE m.kind = 'attachment' AND m.draft_checked = 0 ORDER BY m.id"
        ).fetchall()
        for row in rows:
            if row["answered_at"] is not None or row["issued"] is not None:
                self._looked_at(int(row["id"]), None, None)
            elif self.queue.read_ended(row):
                self._decide(row)

    def _decide(self, row: sqlite3.Row) -> None:
        """Two or more images make a new draft: an album sent soon after is another document (the final review's M2).
        A photo sent alone is offered to the chat's draft that changed less than an hour ago: the model adds it as a
        page when the person's words or what it shows say so (a missing page sent later); it joins nothing itself."""
        pages = self._images(int(row["id"]))
        records = [str(page["id"]) for page in pages]
        chat_id, message_id = int(row["chat_id"]), int(row["message_id"])
        draft_id: int | None = None
        revision: int | None = None
        with transaction(self.conn):
            if len(pages) >= MIN_PAGES:
                draft_id = self._create(chat_id, str(pages[0]["authenticated_subject"]), message_id, records)
                revision = 1
            elif pages:
                offered = self._offered(chat_id, str(pages[0]["authenticated_subject"]))
                draft_id = None if offered is None else int(offered["id"])
            self._looked_at(int(row["id"]), draft_id, revision)
        if revision is not None:
            self.events.log("draft_batch", {"draft": draft_id, "revision": revision, "images": len(pages)})
        self.queue.wake()

    def _offered(self, chat_id: int, person_id: str) -> sqlite3.Row | None:
        row = self.queue.latest_document(chat_id)
        return row if row is not None and row["person_id"] == person_id else None

    def _looked_at(self, host_message_id: int, draft_id: int | None, revision: int | None) -> None:
        """The batch was looked at for a draft: the one it made, with its revision, or the one its photo is offered
        to (no revision)."""
        self.conn.execute(
            "UPDATE host_message SET draft_checked = 1, draft_id = ?, draft_revision = ? WHERE id = ?",
            (draft_id, revision, host_message_id),
        )

    def _images(self, host_message_id: int) -> list[sqlite3.Row]:
        """The batch's images in the order they came, each original once."""
        rows = self.conn.execute(
            "SELECT e.* FROM host_message_item i JOIN evidence e ON e.id = i.evidence_id "
            "WHERE i.host_message_id = ? ORDER BY i.position",
            (host_message_id,),
        ).fetchall()
        return _distinct([row for row in rows if _image(row)])[:MAX_PAGES]

    def _create(
        self,
        chat_id: int,
        person_id: str,
        reply_to: int | None,
        records: Sequence[str],
        *,
        ordered: bool = False,
        by: int | None = None,
    ) -> int:
        now = self.clock()
        cursor = self.conn.execute(
            "INSERT INTO draft(chat_id, person_id, state, revision, reply_to, created_at, updated_at, ordered, "
            "ordered_by) VALUES (?, ?, 'open', 1, ?, ?, ?, ?, ?)",
            (chat_id, person_id, reply_to, now, now, 1 if ordered else None, by),
        )
        assert cursor.lastrowid is not None
        self._set_pages(cursor.lastrowid, 1, records)
        return cursor.lastrowid

    def _set_pages(self, draft_id: int, revision: int, records: Sequence[str]) -> None:
        self.conn.executemany(
            "INSERT INTO draft_page(draft_id, revision, position, evidence_id) VALUES (?, ?, ?, ?)",
            [(draft_id, revision, position, record) for position, record in enumerate(records, 1)],
        )

    def _revise(
        self, draft_id: int, revision: int, records: Sequence[str], reply_to: int | None, by: int | None
    ) -> None:
        """A new revision the model ordered: its pages, and its PDF to come as a reply to `reply_to` (None: where the
        last went) once the turn's message `by` is answered."""
        self._set_pages(draft_id, revision, records)
        self.conn.execute(
            "UPDATE draft SET revision = ?, ordered = ?, ordered_by = ?, state = 'open', "
            "reply_to = COALESCE(?, reply_to), updated_at = ? WHERE id = ?",
            (revision, revision, by, reply_to, self.clock(), draft_id),
        )

    def _caller(self, chat_id: int, message_id: int | None) -> sqlite3.Row | None:
        """The issued message the turn calling make_pdf answers."""
        if message_id is None:
            return None
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM host_message WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
        ).fetchone()
        return row

    def _pages(self, draft_id: int, revision: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT e.* FROM draft_page p JOIN evidence e ON e.id = p.evidence_id "
            "WHERE p.draft_id = ? AND p.revision = ? ORDER BY p.position",
            (draft_id, revision),
        ).fetchall()

    # ---- make_pdf ---------------------------------------------------------------------------------------------------
    def change(
        self,
        person_id: str,
        chat_id: int,
        spaces: Sequence[str],
        *,
        draft_id: int | None,
        order: Sequence[int] | None,
        add: Sequence[str] | None,
        reply_to: int | None,
        revision: int | None = None,
        not_a_document: bool = False,
    ) -> dict[str, Any]:
        """A new revision of the person's own draft, or a new draft from images they may see, and its PDF after the
        model's answer. `add`: records of images to add; `order`: the pages to keep by number, the draft's pages as
        they are and then the added ones after them, in their new order (none: all, the added ones last); `revision`:
        the revision the numbers belong to. `not_a_document`: the model read that the images are no document (the
        owner's choice of 2026-10-26): the draft is closed, and no PDF is made."""
        if not_a_document and (order is not None or add):
            raise DraftRefused("not_a_document goes alone: no order, no add")
        if not_a_document and draft_id is None:
            raise DraftRefused("not_a_document needs a draft")
        if not_a_document and revision is None:  # the review's M2: never a page the model did not see
            raise DraftRefused("not_a_document needs the draft's revision")
        if len(add or ()) > MAX_PAGES:
            raise DraftRefused(f"a document has at most {MAX_PAGES} pages")
        added = [self._addable(record, spaces) for record in add or ()]
        with transaction(self.conn):
            caller = self._caller(chat_id, reply_to)
            by = None if caller is None else int(caller["id"])
            if draft_id is None:
                if order is not None:
                    raise DraftRefused("order needs a draft; for a new one, give its photos in add")
                pages = _distinct(added)
                if not pages:
                    raise DraftRefused("give the photos of the new document in add")
                records = [str(page["id"]) for page in pages]
                draft_id = self._create(chat_id, person_id, reply_to, records, ordered=True, by=by)
                revision, lost, repeated = 1, 0, len(added) - len(pages)
            else:
                draft = self.conn.execute(
                    "SELECT * FROM draft WHERE id = ? AND person_id = ? AND chat_id = ?", (draft_id, person_id, chat_id)
                ).fetchone()
                if draft is None:
                    raise DraftRefused("no such draft")
                current = self._pages(draft_id, int(draft["revision"]))
                if revision is not None and revision != draft["revision"]:  # the review's I1: numbers of another one
                    pages_now = ", ".join(f"{n} record {page['id']}" for n, page in enumerate(current, 1))
                    raise DraftRefused(
                        f"the draft has changed: it is revision {draft['revision']} now, its pages {pages_now}"
                    )
                if not_a_document:
                    if draft["state"] in ("done", "building") or self._built(draft_id):
                        raise DraftRefused("its PDF is made already")
                    if draft["ordered"] is not None:  # the review's I2: the model said it is a document
                        raise DraftRefused("it was put in order already: it is a document")
                    if caller is None or caller["draft_id"] != draft_id or caller["draft_revision"] is None:
                        raise DraftRefused("only the turn on the photos that made the draft closes it")
                    if not self._all_read(draft_id, int(draft["revision"])):  # the review's I2: no text, no sign
                        raise DraftRefused("a page was not read: make the PDF, and the person decides")
                    self.conn.execute(
                        "UPDATE draft SET state = 'declined', updated_at = ? WHERE id = ?", (self.clock(), draft_id)
                    )
                    declined = {"draft": draft_id, "revision": int(draft["revision"]), "by": "model"}
                    self.events.log("draft_declined", declined)
                    return {
                        "draft": draft_id,
                        "not_a_document": True,
                        "note": "make_pdf with the draft and an order opens it again",
                    }
                candidates = [*current, *added]
                kept = candidates
                if order is not None:
                    if not all(1 <= number <= len(candidates) for number in order):
                        raise DraftRefused(f"the pages are numbered 1 to {len(candidates)}")
                    if len(set(order)) != len(order):
                        raise DraftRefused("a page appears twice in order")
                    kept = [candidates[number - 1] for number in order]
                usable = [page for page in kept if page["space_id"] in spaces]  # the final review's I2
                pages = _distinct(usable)
                lost, repeated = len(kept) - len(usable), len(usable) - len(pages)
                if not pages:
                    raise DraftRefused("a document needs one page at least")
                if len(pages) > MAX_PAGES:
                    raise DraftRefused(f"a document has at most {MAX_PAGES} pages")
                revision = int(draft["revision"]) + 1
                self._revise(draft_id, revision, [str(page["id"]) for page in pages], reply_to, by)
        self.events.log("draft_changed", {"draft": draft_id, "revision": revision, "pages": len(pages)})
        self.drawing.set()  # its PDF is built once the model has answered
        result: dict[str, Any] = {
            "draft": draft_id,
            "revision": revision,
            "pages": len(pages),
            "records": [str(p["id"]) for p in pages],
        }
        notes = [f"{repeated} photo(s) of the same bytes as another page"] if repeated else []
        notes += [f"{lost} photo(s) no longer the person's to use"] if lost else []
        if notes:
            result["left_out"] = "; ".join(notes)
        result["pdf"] = NOTE
        return result

    def _all_read(self, draft_id: int, revision: int) -> bool:
        """Every page's reading ended with a text, empty or not: a reader not installed, or one that failed, is no
        sign that a page holds no text."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM draft_page p LEFT JOIN job j ON j.evidence_id = p.evidence_id "
            "WHERE p.draft_id = ? AND p.revision = ? AND (j.state IS NULL OR j.state != 'done')",
            (draft_id, revision),
        ).fetchone()
        return int(row[0]) == 0

    def _built(self, draft_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM document_version WHERE draft_id = ? LIMIT 1", (draft_id,)).fetchone()
        return row is not None

    def _addable(self, record: str, spaces: Sequence[str]) -> sqlite3.Row:
        marks = ",".join("?" * len(spaces))
        row: sqlite3.Row | None = self.conn.execute(
            f"SELECT * FROM evidence WHERE id = ? AND space_id IN ({marks})", (record, *spaces)
        ).fetchone()
        if row is None:
            raise DraftRefused("no such record")  # whether it exists in a space the person may not see is not told
        if not _image(row):
            raise DraftRefused("only photos and images can be pages")
        return row

    # ---- the PDF's time ---------------------------------------------------------------------------------------------
    async def _due(self) -> None:
        """A draft the model ordered gets its PDF once the model has answered: no batch of it waits for the answer,
        and no run of the model in its chat is alive (it may have changed the draft for the person's words)."""
        due = self.conn.execute(
            "SELECT * FROM draft WHERE state = 'open' AND ordered = revision ORDER BY id"
        ).fetchall()
        for draft in due:
            if self._held(draft):
                continue
            self.conn.execute(
                "UPDATE draft SET state = 'building', updated_at = ? WHERE id = ? AND state = 'open' AND revision = ?",
                (self.clock(), draft["id"], draft["revision"]),
            )
            self.events.log("draft_due", {"draft": int(draft["id"]), "revision": int(draft["revision"])})

    def _held(self, draft: sqlite3.Row) -> bool:
        """A batch of the draft, or the message whose turn ordered it, waits for the model's answer, or a run of the
        model in the draft's chat is alive: the person
        gets the PDF after its answer (the owner's choice of 2026-10-26). The wait ends with the answer, or when the
        runs are over and HOLD_SECONDS have passed since the host took the batch (or since the draft's last change
        while the host has not): no person is left without a PDF. The gateway may join a long text with the next
        message, and that run's answer answers both."""
        now = self.clock()
        run = self.conn.execute(
            "SELECT 1 FROM host_run r JOIN host_message rm ON rm.id = r.host_message_id "
            "WHERE rm.chat_id = ? AND r.expires_at > ? AND r.ended_at IS NULL LIMIT 1",
            (draft["chat_id"], now),
        ).fetchone()
        if run is not None:
            return True
        row = self.conn.execute(
            "SELECT 1 FROM host_message m LEFT JOIN host_update u ON u.host_message_id = m.id "
            "WHERE (m.draft_id = ? OR m.id = ?) AND m.answered_at IS NULL AND m.cancelled_at IS NULL "
            "AND (COALESCE(u.issued_at, ?) > ? OR EXISTS (SELECT 1 FROM host_run r JOIN host_message rm "
            "ON rm.id = r.host_message_id WHERE rm.chat_id = m.chat_id AND rm.id >= m.id AND r.expires_at > ?)) "
            "LIMIT 1",
            (draft["id"], draft["ordered_by"], draft["updated_at"], now - self.hold, now),
        ).fetchone()
        return row is not None

    # ---- the PDF ----------------------------------------------------------------------------------------------------
    async def _builds(self) -> None:
        for draft in self.conn.execute("SELECT * FROM draft WHERE state = 'building' ORDER BY id").fetchall():
            if not self._quiet(int(draft["id"]), int(draft["revision"])):
                continue  # looked at again on the next pass
            try:
                await self._build(draft)
            except Exception as exc:  # one draft must not stop the others; its error's type names it
                self._failed(int(draft["id"]), int(draft["revision"]), "pdf", exc)

    async def _build(self, draft: sqlite3.Row) -> None:
        """The PDF of the draft's current revision, from the originals in that order: a new version of the document.
        Built once: after a crash, the record made of it is found by its key."""
        draft_id, revision = int(draft["id"]), int(draft["revision"])
        key = f"draft:{draft_id}:{revision}"
        pages = self._pages(draft_id, revision)
        version = self._version(draft_id, revision)
        record = self.store.by_ingest_key(key)
        if record is None:
            folder = self.jobs_dir / f"draft-{draft_id}-{revision}"
            try:
                data = await self._render("pdf", folder, pages, MAX_PDF_BYTES, str(draft["person_id"]))
            except _RENDER_ERRORS as exc:
                self._failed(draft_id, revision, "pdf", exc)
                return
            finally:
                await self._clear(folder)
            record = await self.store.ingest_derived(
                key,
                space_id=_space(pages),
                person_id=str(draft["person_id"]),
                chat_id=int(draft["chat_id"]),
                message_id=int(draft["reply_to"] or 0),
                name=self._name(draft, version),
                mime="application/pdf",
                data=data,
                sources=[str(page["id"]) for page in pages],
                texts=[self._text(str(page["id"])) for page in pages],
            )
        reply_to = draft["reply_to"]
        document = {
            "evidence_id": record["id"],
            "path": str(self.store.incoming_dir / record["incoming_path"]),
            "sha256": record["sha256"],
            "size": record["size"],
            "mime": "application/pdf",
            "name": record["original_name"],
            "caption": self.locale.text("draft_built").format(n=len(pages)),
        }
        with transaction(self.conn):
            built = self.conn.execute(
                "UPDATE draft SET state = 'done', updated_at = ? WHERE id = ? AND revision = ? AND state = 'building'",
                (self.clock(), draft_id, revision),
            ).rowcount
            if not built:  # the review's I3: the draft changed while it was built; its new revision is built next
                self.conn.execute("DELETE FROM evidence_fts WHERE evidence_id = ?", (record["id"],))
            else:
                self.conn.execute(
                    "INSERT OR IGNORE INTO document_version(draft_id, version, revision, evidence_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (draft_id, version, revision, record["id"], self.clock()),
                )
                # Only the latest version is found by search; get still reads the older ones.
                self.conn.execute(
                    "DELETE FROM evidence_fts WHERE evidence_id IN "
                    "(SELECT evidence_id FROM document_version WHERE draft_id = ? AND version < ?)",
                    (draft_id, version),
                )
                self.outbox.enqueue_document(
                    f"draft_pdf:{draft_id}:{version}", int(draft["chat_id"]), document, reply_to=reply_to
                )
        if not built:
            self.events.log("draft_superseded", {"draft": draft_id, "revision": revision})
            return
        self.events.log(
            "draft_built",
            {
                "draft": draft_id,
                "revision": revision,
                "version": version,
                "pages": len(pages),
                "evidence_id": record["id"],
            },
        )

    def _quiet(self, draft_id: int, revision: int) -> bool:
        """No page's album got an item for the quiet window: a late private caption can no longer move it (review 1's
        I4), so the document's space is known."""
        for page in self._pages(draft_id, revision):
            if page["media_group_id"] is None:
                continue
            last = self.store.album_last_received(int(page["chat_id"]), str(page["media_group_id"]))
            if last is not None and self.clock() - last < self.album_quiet:
                return False
        return True

    def _version(self, draft_id: int, revision: int) -> int:
        """The version a revision's PDF is: the one it was given, or the next."""
        row = self.conn.execute(
            "SELECT version FROM document_version WHERE draft_id = ? AND revision = ?", (draft_id, revision)
        ).fetchone()
        if row is not None:
            return int(row[0])
        latest = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM document_version WHERE draft_id = ?", (draft_id,)
        ).fetchone()[0]
        return int(latest) + 1

    def _name(self, draft: sqlite3.Row, version: int) -> str:
        date = datetime.fromtimestamp(float(draft["created_at"]), self.store.tz).date().isoformat()
        if version == 1:
            return self.locale.text("draft_name").format(date=date) + ".pdf"
        return self.locale.text("draft_name_version").format(date=date, version=version) + ".pdf"

    def _text(self, evidence_id: str) -> str:
        """A page's text: all that was read from its photo."""
        rows = self.conn.execute(
            "SELECT text FROM evidence_text WHERE evidence_id = ? ORDER BY page", (evidence_id,)
        ).fetchall()
        return "\n\n".join(str(row[0]) for row in rows)

    # ---- drawing ----------------------------------------------------------------------------------------------------
    async def _render(
        self, action: str, folder: Path, pages: Sequence[sqlite3.Row], max_bytes: int, person_id: str
    ) -> bytes:
        """The PDF of these pages, drawn in the sandbox from copies of their originals in a folder of
        its own: the renderer never sees the incoming folder. Every page must still be the person's to use: a shared
        photo may have become another member's own since it was added (the final review's I2)."""
        for number, page in enumerate(pages, 1):
            if page["space_id"] not in _spaces(person_id):
                raise ReaderFailed(f"page {number} is no longer the person's to use")
        if os.path.lexists(folder):
            await asyncio.to_thread(remove, folder)
        folder.mkdir(mode=0o700, parents=True)
        copies = []
        # Every page is copied before any program runs in the folder: a program broken by a crafted page could
        # leave a link where Core would write the next one (review 1's C1). Each copy is created anew, never opened.
        for number, page in enumerate(pages, 1):
            suffix = Path(str(page["incoming_path"])).suffix.lower()
            copy = folder / f"page-{number}{suffix if _SUFFIX.fullmatch(suffix) else ''}"
            original = self.store.incoming_dir / str(page["incoming_path"])
            await asyncio.to_thread(_copy_checked, original, copy, str(page["sha256"]), number)
            copies.append(copy)
        inputs = []
        for number, copy in enumerate(copies, 1):
            if copy.suffix in _HEIC:
                converted = folder / f"page-{number}.png"
                await self.readers.to_png(copy, converted, folder, max_side=PDF_SIDE)
                copy = converted
            inputs.append(copy)
        return await self.readers.pages(action, inputs, folder, max_bytes=max_bytes)

    async def _clear(self, folder: Path) -> None:
        try:
            await asyncio.to_thread(remove, folder)
        except Exception as exc:  # left for the next start's sweep of the jobs folder
            self.events.log("draft_folder_left", {"error": type(exc).__name__})

    def _failed(self, draft_id: int, revision: int, stage: str, exc: BaseException) -> None:
        """A PDF that could not be drawn: the person hears so, and the photos stay as they are."""
        with transaction(self.conn):
            draft = self.conn.execute("SELECT * FROM draft WHERE id = ?", (draft_id,)).fetchone()
            failed = self.conn.execute(
                "UPDATE draft SET state = 'failed', updated_at = ? WHERE id = ? AND revision = ? "
                "AND state IN ('open', 'building')",
                (self.clock(), draft_id, revision),
            ).rowcount
            if failed:
                text = self.locale.text("draft_too_large" if isinstance(exc, OutputTooLarge) else "draft_failed")
                reply_to = draft["reply_to"]
                self.outbox.enqueue_text(f"draft_failed:{draft_id}:{revision}", int(draft["chat_id"]), text, reply_to)
        self.events.log("draft_failed", {"draft": draft_id, "revision": revision, "stage": stage, "error": _error(exc)})

    # ---- the loop ---------------------------------------------------------------------------------------------------
    async def run_once(self) -> None:
        self.tick()
        await self._due()
        await self._builds()

    async def run(self, stop: asyncio.Event) -> None:
        """Deciding: quick, and never behind a drawing, for every chat's batch waits for it."""
        while not stop.is_set():
            self.wake.clear()
            self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), TICK_SECONDS)

    async def draw(self, stop: asyncio.Event) -> None:
        """Drawing: PDFs, one at a time. A stop cancels the drawing under way, its renderer killed with its process
        group; its draft is left as it was, so the next start draws it again (the final review's I1)."""
        while not stop.is_set():
            self.drawing.clear()
            await until_stopped(stop, self._draw_once())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.drawing.wait(), TICK_SECONDS)

    async def _draw_once(self) -> None:
        await self._due()
        await self._builds()
