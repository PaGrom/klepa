"""Documents from photos (spec 6.1 item 4, 10 make_pdf; scenarios 37, 45, 46, 48).

Two or more images in one batch are a draft of a document: an album's arrival does not prove that every page came
(forum #65177), so the person checks the pages before there is a PDF. When the batch's reading ends, the batch's message
tells the model of the draft and numbers its pages; the model puts them in the order their content shows, through
make_pdf, before it answers, or says they are no document, and then no sheet goes out (the owner's choices of
2026-10-26); only then Core sends one sheet of numbered
thumbnails, as a reply to the first image, with the buttons Done, Change the order and Not a document. Later the person
says in words what to change, and the model passes it to make_pdf. Each change is a revision with a sheet of its own,
and an older sheet loses its buttons. Done builds the PDF of the current revision from the originals: each time a new
version of the document, a record Core derived, whose text is its pages' texts and whose space is the most private of
theirs. The originals stay as they are, and search finds only the latest version. Sheets and PDFs are drawn in the
sandbox by Core's own renderer, as readers read files: Core never decodes a person's image itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..aio import until_stopped
from ..db import transaction
from ..durable import make_dirs_durably, write_new_atomically
from ..events import EventLog, utc_now_iso
from ..evidence import EvidenceStore
from ..host.queue import HostQueue
from ..locale import Locale
from ..processing.jobs import job_kind, remove
from ..processing.readers import OutputTooLarge, ReaderFailed, ReaderMissing, Readers
from ..processing.sandbox import ProcessingStopped
from .outbox import Outbox

MIN_PAGES = 2  # images in one batch that make a draft
# A sheet waits for the model's answer to its batch, which may put the pages in the order their content shows: while
# the model's run on the batch is alive, and at most this long after the host took it (the owner's word, 2026-10-26:
# many pages of small text take a model long; ten minutes is also when Core alerts the owner to a silent model).
HOLD_SECONDS = 600.0
JOIN_SECONDS = 600.0  # a photo sent alone this soon after a draft's last change is one more of its pages
MAX_PAGES = 100  # pages of one draft, as Core's renderer draws them (processing.pages.MAX_PAGES)
SHEET_SIDE = 720  # pixels: a HEIC page for the sheet, twice its thumbnail
PDF_SIDE = 3508  # pixels: a HEIC page for the PDF, A4 at 300 dpi (processing.pages.MAX_SIDE)
MAX_SHEET_BYTES = 10 << 20  # what Telegram takes as a photo
MAX_PDF_BYTES = 50 << 20  # what Telegram takes from a bot as a document
BUTTON_TTL_SECONDS = 24 * 3600.0  # as Core's other buttons; make_pdf draws a sheet with fresh ones
TICK_SECONDS = 0.5
USABLE = ("stored", "processing", "processed", "failed")  # the records whose original Core keeps
DONE, ORDER, NO = "draft_done", "draft_order", "draft_no"  # a button's action: "<what>:<draft>:<revision>"
_ACTION = re.compile(r"(draft_done|draft_order|draft_no):(\d{1,18}):(\d{1,9})")
_HEIC = frozenset({".heic", ".heif"})
_SUFFIX = re.compile(r"\.[a-z0-9]{1,8}")
_RENDER_ERRORS = (ReaderFailed, ReaderMissing, ProcessingStopped, OSError)


class DraftRefused(Exception):
    """A change make_pdf does not make. Its text goes back to the model and never carries a record's content."""


@dataclass(frozen=True)
class Answer:
    """What a press is answered with: a toast (None: none), or a dialog the person closes."""

    text: str | None
    alert: bool = False


def is_draft_action(action: str) -> bool:
    return _ACTION.fullmatch(action) is not None


def _sheet_key(draft_id: int, revision: int) -> str:
    return f"draft_sheet:{draft_id}:{revision}"


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
        drafts_dir: Path,
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
        self.drafts_dir = drafts_dir
        self.album_quiet = album_quiet_seconds  # a late item may still make an album personal (#15)
        self.hold = hold_seconds
        self.clock = clock
        # Each loop's own wake-up (2b-2's M1): deciding never waits for drawing, which may take a minute.
        self.wake = asyncio.Event()  # deciding: batches to look at, sheets to take buttons off
        self.drawing = asyncio.Event()  # drawing: sheets to draw, PDFs to build

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
        """A photo sent alone joins the chat's open draft when it changed less than ten minutes ago: a page forgotten
        comes after the album, and one sent twice is a page once. Two or more images make a new draft: an album sent
        soon after is another document (the final review's M2)."""
        pages = self._images(int(row["id"]))
        records = [str(page["id"]) for page in pages]
        chat_id, message_id = int(row["chat_id"]), int(row["message_id"])
        draft_id: int | None = None
        revision: int | None = None
        with transaction(self.conn):
            joined = self._joinable(chat_id) if 0 < len(pages) < MIN_PAGES else None
            current: list[str] = []
            merged: list[str] = []
            if joined is not None:
                spaces = _spaces(str(joined["person_id"]))
                current = [
                    str(page["id"])
                    for page in self._pages(int(joined["id"]), int(joined["revision"]))
                    if page["space_id"] in spaces  # a page no longer the person's to use is left out
                ]
                merged = [str(page["id"]) for page in _distinct(self._rows([*current, *records]))]
                if len(merged) > MAX_PAGES:  # a draft that would pass its pages takes none of them
                    joined = None
            if joined is not None:
                draft_id = int(joined["id"])
                if len(merged) > len(current):  # one only of pages it has already changes nothing
                    revision = int(joined["revision"]) + 1
                    self._revise(draft_id, revision, merged, message_id)
            elif len(pages) >= MIN_PAGES:
                draft_id = self._create(chat_id, str(pages[0]["authenticated_subject"]), message_id, records)
                revision = 1
            self._looked_at(int(row["id"]), draft_id, revision)
        if draft_id is not None:
            self.events.log("draft_batch", {"draft": draft_id, "revision": revision, "images": len(pages)})
            self.drawing.set()
        self.queue.wake()

    def _joinable(self, chat_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM draft WHERE chat_id = ? AND state = 'open' AND updated_at >= ? ORDER BY id DESC LIMIT 1",
            (chat_id, self.clock() - JOIN_SECONDS),
        ).fetchone()
        return row

    def _rows(self, records: Sequence[str]) -> list[sqlite3.Row]:
        rows = {
            str(row["id"]): row
            for row in self.conn.execute(
                f"SELECT * FROM evidence WHERE id IN ({','.join('?' * len(records))})", list(records)
            )
        }
        return [rows[record] for record in records]

    def _looked_at(self, host_message_id: int, draft_id: int | None, revision: int | None) -> None:
        """The batch was looked at for a draft: the one it made or joined, and the revision it made (None: its
        images were the draft's pages already)."""
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

    def _create(self, chat_id: int, person_id: str, reply_to: int | None, records: Sequence[str]) -> int:
        now = self.clock()
        cursor = self.conn.execute(
            "INSERT INTO draft(chat_id, person_id, state, revision, reply_to, created_at, updated_at) "
            "VALUES (?, ?, 'open', 1, ?, ?, ?)",
            (chat_id, person_id, reply_to, now, now),
        )
        assert cursor.lastrowid is not None
        self._set_pages(cursor.lastrowid, 1, records)
        return cursor.lastrowid

    def _set_pages(self, draft_id: int, revision: int, records: Sequence[str]) -> None:
        self.conn.executemany(
            "INSERT INTO draft_page(draft_id, revision, position, evidence_id) VALUES (?, ?, ?, ?)",
            [(draft_id, revision, position, record) for position, record in enumerate(records, 1)],
        )

    def _revise(self, draft_id: int, revision: int, records: Sequence[str], reply_to: int | None) -> None:
        """A new revision: its pages, and its sheet to come as a reply to `reply_to` (None: where the last went)."""
        self._set_pages(draft_id, revision, records)
        self.conn.execute(
            "UPDATE draft SET revision = ?, state = 'open', reply_to = COALESCE(?, reply_to), updated_at = ? "
            "WHERE id = ?",
            (revision, reply_to, self.clock(), draft_id),
        )

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
        """A new revision of the person's own draft, or a new draft from images they may see. `order`: the pages to
        keep, by their numbers as the draft is now (before its first sheet, every one), in their new order; `add`:
        records of images to put after them; `revision`: the revision the numbers belong to. Core then sends a new
        sheet, and the PDF still waits for the person's Done. `not_a_document`: the model read that the images are no
        document (the owner's choice of 2026-10-26): the draft is closed, and a sheet not sent yet never goes out."""
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
            if draft_id is None:
                if order is not None:
                    raise DraftRefused("order needs a draft; for a new one, give its photos in add")
                pages = _distinct(added)
                if not pages:
                    raise DraftRefused("give the photos of the new document in add")
                draft_id = self._create(chat_id, person_id, reply_to, [str(page["id"]) for page in pages])
                revision, lost, repeated = 1, 0, len(added) - len(pages)
            else:
                draft = self.conn.execute(
                    "SELECT * FROM draft WHERE id = ? AND person_id = ? AND chat_id = ?", (draft_id, person_id, chat_id)
                ).fetchone()
                if draft is None:
                    raise DraftRefused("no such draft")
                if draft["state"] == "building":
                    raise DraftRefused("its PDF is being made: try again in a moment")
                current = self._pages(draft_id, int(draft["revision"]))
                if revision is not None and revision != draft["revision"]:  # the review's I1: numbers of another one
                    pages_now = ", ".join(f"{n} record {page['id']}" for n, page in enumerate(current, 1))
                    raise DraftRefused(
                        f"the draft has changed: it is revision {draft['revision']} now, its pages {pages_now}"
                    )
                if not_a_document:
                    if draft["state"] == "done":
                        raise DraftRefused("its PDF is made already")
                    if self._sheet_sent(draft_id):  # the review's I1: a stray photo joined; the person decides
                        raise DraftRefused(
                            "a sheet went out already: the person decides with its buttons, and a page that is no "
                            "part of the document is left out with order"
                        )
                    if not self._all_read(draft_id, int(draft["revision"])):  # the review's I2: no text, no sign
                        raise DraftRefused("a page was not read: keep the draft, and the person decides")
                    self.conn.execute(
                        "UPDATE draft SET state = 'declined', updated_at = ? WHERE id = ?", (self.clock(), draft_id)
                    )
                    declined = {"draft": draft_id, "revision": int(draft["revision"]), "by": "model"}
                    self.events.log("draft_declined", declined)
                    self.wake.set()  # a sheet sent already loses its buttons
                    return {
                        "draft": draft_id,
                        "not_a_document": True,
                        "note": "make_pdf with the draft and an order opens it again",
                    }
                kept = current
                if order is not None:
                    if not all(1 <= number <= len(current) for number in order):
                        raise DraftRefused(f"the pages of the latest sheet are numbered 1 to {len(current)}")
                    if len(set(order)) != len(order):
                        raise DraftRefused("a page appears twice in order")
                    whole = sorted(order) == list(range(1, len(current) + 1))
                    if order and not whole and not self._sheet_sent(draft_id):
                        raise DraftRefused(f"before the first sheet, order lists every page once: 1 to {len(current)}")
                    kept = [current[number - 1] for number in order]
                usable = [page for page in kept if page["space_id"] in spaces]  # the final review's I2
                pages = _distinct([*usable, *added])
                lost, repeated = len(kept) - len(usable), len(usable) + len(added) - len(pages)
                if not pages:
                    raise DraftRefused("a document needs one page at least")
                if len(pages) > MAX_PAGES:
                    raise DraftRefused(f"a document has at most {MAX_PAGES} pages")
                revision = int(draft["revision"]) + 1
                self._revise(draft_id, revision, [str(page["id"]) for page in pages], reply_to)
        self.events.log("draft_changed", {"draft": draft_id, "revision": revision, "pages": len(pages)})
        self.wake.set()  # the older sheet's buttons come off
        self.drawing.set()  # the new one is drawn
        result: dict[str, Any] = {"draft": draft_id, "pages": len(pages), "records": [str(p["id"]) for p in pages]}
        notes = [f"{repeated} photo(s) already in the draft"] if repeated else []
        notes += [f"{lost} photo(s) no longer the person's to use"] if lost else []
        if notes:
            result["left_out"] = "; ".join(notes)
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

    def _sheet_sent(self, draft_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM draft_sheet WHERE draft_id = ? AND outbound_key IS NOT NULL LIMIT 1", (draft_id,)
        ).fetchone()
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

    # ---- the buttons ------------------------------------------------------------------------------------------------
    def press(self, action: str, chat_id: int) -> Answer:
        """A press on a draft's sheet, its button checked by Attachments.press (one use, its own chat and message, not
        expired): Done has the PDF built, Not a document closes the draft, Change the order says how to. A sheet that
        no longer stands for the draft says its button no longer works."""
        expired = Answer(self.locale.text("button_expired"))
        match = _ACTION.fullmatch(action)
        if match is None:
            return expired
        what, draft_id, revision = match.group(1), int(match.group(2)), int(match.group(3))
        with transaction(self.conn):
            draft = self.conn.execute("SELECT * FROM draft WHERE id = ?", (draft_id,)).fetchone()
            if (
                draft is None
                or draft["state"] != "open"
                or draft["revision"] != revision
                or draft["chat_id"] != chat_id
            ):
                return expired
            if what == ORDER:
                return Answer(self.locale.text("draft_order_hint"), alert=True)
            claimed = self.conn.execute(
                "UPDATE button_action SET used_at = ? WHERE outbound_key = ? AND used_at IS NULL",
                (utc_now_iso(), _sheet_key(draft_id, revision)),
            ).rowcount
            if not claimed:
                return expired
            state = "building" if what == DONE else "declined"
            self.conn.execute(
                "UPDATE draft SET state = ?, updated_at = ? WHERE id = ?", (state, self.clock(), draft_id)
            )
        self.events.log("draft_done" if what == DONE else "draft_declined", {"draft": draft_id, "revision": revision})
        self.wake.set()  # the sheet's buttons come off
        self.drawing.set()  # the PDF is built
        return Answer(self.locale.text("draft_building" if what == DONE else "draft_declined"))

    def _unbutton(self) -> None:
        """A sheet that no longer stands for its draft loses its buttons: an older revision's, and the sheet of a draft
        done, declined, failed or being built. One that has not left yet never will; one on its way waits for
        Telegram's id for it."""
        rows = self.conn.execute(
            "SELECT s.draft_id, s.revision, s.outbound_key, d.chat_id FROM draft_sheet s JOIN draft d ON d.id = "
            "s.draft_id WHERE s.unbuttoned = 0 AND (s.revision < d.revision OR d.state != 'open')"
        ).fetchall()
        for row in rows:
            key = row["outbound_key"]
            if key is not None and self.outbox.state(str(key)) == "SENDING":
                continue
            with transaction(self.conn):
                if key is not None:
                    self.conn.execute(
                        "UPDATE button_action SET used_at = ? WHERE outbound_key = ? AND used_at IS NULL",
                        (utc_now_iso(), key),
                    )
                    message_id = self.outbox.message_id(str(key))
                    if message_id is not None:
                        unbutton = f"draft_unbutton:{row['draft_id']}:{row['revision']}"
                        self.outbox.enqueue_unbutton(unbutton, int(row["chat_id"]), message_id)
                    else:
                        self.outbox.withdraw(str(key))
                self.conn.execute(
                    "UPDATE draft_sheet SET unbuttoned = 1 WHERE draft_id = ? AND revision = ?",
                    (row["draft_id"], row["revision"]),
                )
            self._discard(int(row["draft_id"]), int(row["revision"]))

    # ---- the sheet --------------------------------------------------------------------------------------------------
    async def _sheets(self) -> None:
        """Each open draft's current revision gets its sheet, once, when no batch of it waits for the model's answer."""
        due = self.conn.execute(
            "SELECT d.* FROM draft d LEFT JOIN draft_sheet s ON s.draft_id = d.id AND s.revision = d.revision "
            "WHERE d.state = 'open' AND s.draft_id IS NULL ORDER BY d.id"
        ).fetchall()
        for draft in due:
            draft_id, revision = int(draft["id"]), int(draft["revision"])
            if self._held(draft):
                continue
            try:
                await self._sheet(draft_id, revision)
            except Exception as exc:  # one draft must not stop the others; its error's type names it
                self._failed(draft_id, revision, "sheet", exc)

    async def _sheet(self, draft_id: int, revision: int) -> None:
        pages = self._pages(draft_id, revision)
        folder = self.jobs_dir / f"draft-{draft_id}-{revision}"
        try:
            person = self.conn.execute("SELECT person_id FROM draft WHERE id = ?", (draft_id,)).fetchone()[0]
            data = await self._render("sheet", folder, pages, MAX_SHEET_BYTES, str(person))
        except _RENDER_ERRORS as exc:
            self._failed(draft_id, revision, "sheet", exc)
            return
        finally:
            await self._clear(folder)
        target = await asyncio.to_thread(self._keep, draft_id, f"sheet-{revision}.jpg", data)
        key = _sheet_key(draft_id, revision)
        buttons = [
            (secrets.token_hex(16), f"{what}:{draft_id}:{revision}", self.locale.text(label))  # 128 bits each
            for what, label in ((DONE, "draft_done_button"), (ORDER, "draft_order_button"), (NO, "draft_no_button"))
        ]
        markup = {"inline_keyboard": [[{"text": label, "callback_data": action_id}] for action_id, _, label in buttons]}
        photo = {"path": str(target), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        caption = self.locale.text("draft_sheet").format(n=len(pages))
        with transaction(self.conn):
            draft = self.conn.execute("SELECT * FROM draft WHERE id = ?", (draft_id,)).fetchone()
            if draft["state"] != "open" or draft["revision"] != revision:  # changed while it was drawn
                self.conn.execute(
                    "INSERT INTO draft_sheet(draft_id, revision, outbound_key, unbuttoned) VALUES (?, ?, NULL, 1)",
                    (draft_id, revision),
                )
                return
            chat_id = int(draft["chat_id"])
            self.outbox.enqueue_photo(key, chat_id, photo, caption, reply_to=draft["reply_to"], reply_markup=markup)
            self.conn.executemany(
                "INSERT INTO button_action(id, action, chat_id, outbound_key, expires_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (action_id, action, chat_id, key, self.clock() + BUTTON_TTL_SECONDS)
                    for action_id, action, _ in buttons
                ],
            )
            self.conn.execute(
                "INSERT INTO draft_sheet(draft_id, revision, outbound_key) VALUES (?, ?, ?)", (draft_id, revision, key)
            )
        self.events.log("draft_sheet", {"draft": draft_id, "revision": revision, "pages": len(pages)})

    def _held(self, draft: sqlite3.Row) -> bool:
        """A batch that made or joined the draft is with the model, which puts the pages in the order their content
        shows before it answers (the owner's choice of 2026-10-26): the person gets one sheet, in that order. The wait
        ends with the answer, or when the model's run on the batch is over and HOLD_SECONDS have passed since the
        host took it (or since the draft's last change while the host has not): no person is left without a sheet."""
        now = self.clock()
        row = self.conn.execute(
            "SELECT 1 FROM host_message m LEFT JOIN host_update u ON u.host_message_id = m.id "
            "WHERE m.draft_id = ? AND m.answered_at IS NULL AND m.cancelled_at IS NULL "
            "AND (COALESCE(u.issued_at, ?) > ? OR EXISTS (SELECT 1 FROM host_run r WHERE r.host_message_id = m.id "
            "AND r.expires_at > ?)) LIMIT 1",
            (draft["id"], draft["updated_at"], now - self.hold, now),
        ).fetchone()
        return row is not None

    def _keep(self, draft_id: int, name: str, data: bytes) -> Path:
        """A sheet as the outbox sends it, durably: a crash before it leaves must not lose it."""
        self.drafts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = make_dirs_durably(self.drafts_dir, str(draft_id))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(directory / name)  # drawn before a crash and never sent: drawn again
        return write_new_atomically(directory, name, data)

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
            sheet = self.outbox.message_id(_sheet_key(draft_id, revision))
            record = await self.store.ingest_derived(
                key,
                space_id=_space(pages),
                person_id=str(draft["person_id"]),
                chat_id=int(draft["chat_id"]),
                message_id=sheet or 0,
                name=self._name(draft, version),
                mime="application/pdf",
                data=data,
                sources=[str(page["id"]) for page in pages],
                texts=[self._text(str(page["id"])) for page in pages],
            )
        reply_to = self.outbox.message_id(_sheet_key(draft_id, revision))
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
            built = self.conn.execute(
                "UPDATE draft SET state = 'done', updated_at = ? WHERE id = ? AND revision = ? AND state = 'building'",
                (self.clock(), draft_id, revision),
            ).rowcount
            if built:
                self.outbox.enqueue_document(
                    f"draft_pdf:{draft_id}:{version}", int(draft["chat_id"]), document, reply_to=reply_to
                )
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

    def _discard(self, draft_id: int, revision: int) -> None:
        """A sheet whose send is over leaves the disk once it no longer stands for its draft (review 1's M1)."""
        key = _sheet_key(draft_id, revision)
        if self.outbox.state(key) in ("PENDING", "RETRY_WAIT", "SENDING"):
            return
        folder = self.drafts_dir / str(draft_id)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(folder / f"sheet-{revision}.jpg")
        with contextlib.suppress(OSError):
            folder.rmdir()  # only once it is empty

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
        """The sheet or the PDF of these pages, drawn in the sandbox from copies of their originals in a folder of
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
                side = SHEET_SIDE if action == "sheet" else PDF_SIDE
                await self.readers.to_png(copy, converted, folder, max_side=side)
                copy = converted
            inputs.append(copy)
        return await self.readers.pages(action, inputs, folder, max_bytes=max_bytes)

    async def _clear(self, folder: Path) -> None:
        try:
            await asyncio.to_thread(remove, folder)
        except Exception as exc:  # left for the next start's sweep of the jobs folder
            self.events.log("draft_folder_left", {"error": type(exc).__name__})

    def _failed(self, draft_id: int, revision: int, stage: str, exc: BaseException) -> None:
        """A sheet or a PDF that could not be drawn: the person hears so, and the photos stay as they are."""
        with transaction(self.conn):
            draft = self.conn.execute("SELECT * FROM draft WHERE id = ?", (draft_id,)).fetchone()
            if stage == "sheet":
                self.conn.execute(
                    "INSERT OR IGNORE INTO draft_sheet(draft_id, revision, outbound_key, unbuttoned) "
                    "VALUES (?, ?, NULL, 1)",
                    (draft_id, revision),
                )
            failed = self.conn.execute(
                "UPDATE draft SET state = 'failed', updated_at = ? WHERE id = ? AND revision = ? "
                "AND state IN ('open', 'building')",
                (self.clock(), draft_id, revision),
            ).rowcount
            if failed:
                too_large = isinstance(exc, OutputTooLarge) and stage == "pdf"  # a sheet's size is Core's to keep
                text = self.locale.text("draft_too_large" if too_large else "draft_failed")
                reply_to = self.outbox.message_id(_sheet_key(draft_id, revision)) or draft["reply_to"]
                self.outbox.enqueue_text(f"draft_failed:{draft_id}:{revision}", int(draft["chat_id"]), text, reply_to)
        self.events.log("draft_failed", {"draft": draft_id, "revision": revision, "stage": stage, "error": _error(exc)})
        self.wake.set()  # its sheet's buttons come off

    # ---- the loop ---------------------------------------------------------------------------------------------------
    async def run_once(self) -> None:
        self.tick()
        self._unbutton()
        await self._sheets()
        await self._builds()
        self._unbutton()

    async def run(self, stop: asyncio.Event) -> None:
        """Deciding: quick, and never behind a drawing, for every chat's batch waits for it."""
        while not stop.is_set():
            self.wake.clear()
            self.tick()
            self._unbutton()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), TICK_SECONDS)

    async def draw(self, stop: asyncio.Event) -> None:
        """Drawing: sheets and PDFs, one at a time. A stop cancels the drawing under way, its renderer killed with its
        process group; its draft is left as it was, so the next start draws it again (the final review's I1)."""
        while not stop.is_set():
            self.drawing.clear()
            await until_stopped(stop, self._draw_once())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.drawing.wait(), TICK_SECONDS)

    async def _draw_once(self) -> None:
        await self._sheets()
        await self._builds()
