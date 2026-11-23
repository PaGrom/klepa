"""Pages shown to the model as pictures (stage 2c-2, spec 4.6 rev): drawn in the readers' sandbox, two at a time,
each within a deadline the MCP shim's 30 seconds allow, and a turn's pictures within a count and bytes that keep any
request under the API's 32 MB and 100 images however many turns the history window holds (`DM_HISTORY_TURNS`).

What is shown is recorded: which run saw which records and pages, so `not_a_document` can require that the model saw
every page, and an album item not yet quiet — a late private caption may still move it — is shown to its sender
alone (review 2's N7), as is a document Core made from such an album."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ..db import transaction
from ..processing.readers import ReaderFailed

SIZES = {"small": (800, 120_000), "large": (1568, 400_000)}  # side in pixels, bytes
SHEET_BYTES = 400_000
OVERVIEW_PAGES = 16
STRIP_PAGES = 6  # processing.pages.STRIP_PAGES: a 9-point line stays legible
OVERVIEW_SIDE = 400  # what each page is drawn at for an overview's cell
STRIP_SIDE = 1568  # what each page is drawn at for strips: its full width
TURN_PICTURES = 10
TURN_PICTURE_BYTES = 3_000_000
DM_HISTORY_TURNS = 3  # channels.telegram.dmHistoryLimit: OpenClaw keeps up to ceil(1.5 * 3) earlier turns + the current
VIEW_SECONDS = 20.0  # from the call's arrival, waiting for a slot included: under the shim's 30 s
VIEWERS = 2


class PictureRefused(Exception):
    """Why a picture is not shown; the tool turns it into its refusal."""


class Drawer(Protocol):
    async def picture(
        self, kind: str, original: Path, page: int, folder: Path, *, side: int, max_bytes: int, deadline: float | None
    ) -> bytes: ...

    async def sheet(
        self, layout: str, pages: Sequence[Path], first: int, folder: Path, *, max_bytes: int, deadline: float | None
    ) -> bytes: ...


class Pictures:
    def __init__(
        self,
        conn: sqlite3.Connection,
        readers: Drawer,
        incoming_dir: Path,
        jobs_dir: Path,
        *,
        album_quiet: float = 60.0,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        pictures_dir: Path | None = None,
    ) -> None:
        self.conn = conn
        self.readers = readers
        self.incoming_dir = incoming_dir
        self.jobs_dir = jobs_dir
        self.album_quiet = album_quiet
        self.clock = clock
        self.mono = mono
        self._slots: asyncio.Semaphore | None = None
        self.pictures_dir = pictures_dir  # photos drawn once when they came (processing.jobs): views compose those

    # ---- who may see it ----------------------------------------------------------------------------------------------
    def _album_open(self, chat_id: int, media_group_id: str | None) -> bool:
        if not media_group_id:
            return False
        row = self.conn.execute(
            "SELECT MAX(received_at) FROM evidence WHERE chat_id = ? AND media_group_id = ?", (chat_id, media_group_id)
        ).fetchone()
        if row[0] is None:
            return False
        return self.clock() - datetime.fromisoformat(row[0]).timestamp() < self.album_quiet

    def withheld(self, record: sqlite3.Row, person_id: str) -> bool:
        """An album item not yet quiet, or a record Core made from one, is its sender's alone for now."""
        if record["authenticated_subject"] == person_id:
            return False
        if self._album_open(int(record["chat_id"]), record["media_group_id"]):
            return True
        sources = json.loads(record["derived_from"] or "[]") if record["channel"] == "core" else []
        for source in sources:
            row = self.conn.execute("SELECT chat_id, media_group_id FROM evidence WHERE id = ?", (source,)).fetchone()
            if row is not None and self._album_open(int(row["chat_id"]), row["media_group_id"]):
                return True
        return False

    # ---- budgets -----------------------------------------------------------------------------------------------------
    def _used(self, run_id: str) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM host_picture WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row[0]), int(row[1])

    def _over(self, run_id: str, more: int) -> str | None:
        count, size = self._used(run_id)
        if count + 1 > TURN_PICTURES:
            return f"this turn has been shown {TURN_PICTURES} pictures, as many as one turn may"
        if size + more > TURN_PICTURE_BYTES:
            return f"this turn has been shown {TURN_PICTURE_BYTES // 1_000_000} MB of pictures, as much as one turn may"
        return None

    def check(self, run_id: str) -> None:
        over = self._over(run_id, 0)
        if over:
            raise PictureRefused(over)

    def record(
        self, run_id: str, chat_id: int, person_id: str, size: str, data: bytes, pages: Sequence[tuple[str, int]]
    ) -> None:
        """The picture shown, and the records and pages it showed, after the budget is checked again in the same
        transaction: two drawn at once must not both pass a budget only one fits (review 3's minor)."""
        with transaction(self.conn):
            over = self._over(run_id, len(data))
            if over:
                raise PictureRefused(over)
            picture = self.conn.execute(
                "INSERT INTO host_picture(run_id, chat_id, person_id, size, bytes, shown_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, chat_id, person_id, size, len(data), self.clock()),
            ).lastrowid
            self.conn.executemany(
                "INSERT OR IGNORE INTO host_picture_record(picture_id, evidence_id, page) VALUES (?, ?, ?)",
                [(picture, evidence_id, page) for evidence_id, page in pages],
            )

    # ---- drawing -----------------------------------------------------------------------------------------------------
    def _slots_now(self) -> asyncio.Semaphore:
        if self._slots is None:
            self._slots = asyncio.Semaphore(VIEWERS)
        return self._slots

    async def _drawing(self, deadline: float, draw: Callable[[Path], Any]) -> bytes:
        slots = self._slots_now()
        try:
            await asyncio.wait_for(slots.acquire(), timeout=max(0.0, deadline - self.mono()))
        except TimeoutError:
            raise PictureRefused("busy: look at fewer pages at once") from None
        folder = self.jobs_dir / f"view-{uuid.uuid4().hex}"
        try:
            folder.mkdir(mode=0o700, parents=True)
            data: bytes = await draw(folder)
            return data
        except ReaderFailed:
            raise PictureRefused("the page could not be drawn") from None
        finally:
            slots.release()
            shutil.rmtree(folder, ignore_errors=True)

    def _source(self, kind: str, record: sqlite3.Row) -> Path:
        """A photo's picture drawn when it came, if Core has it: no HEIC decoded again; else the original."""
        if kind == "image" and self.pictures_dir is not None:
            row = self.conn.execute(
                "SELECT path FROM thumb WHERE sha256 = ? AND page = 1", (record["sha256"],)
            ).fetchone()
            kept: Path | None = None if row is None else self.pictures_dir / str(row["path"])
            if kept is not None and kept.is_file():
                return kept
        return self.incoming_dir / str(record["incoming_path"])

    async def page(self, kind: str, record: sqlite3.Row, page: int, size: str) -> bytes:
        side, cap = SIZES[size]
        deadline = self.mono() + VIEW_SECONDS
        original = self._source(kind, record)

        async def draw(folder: Path) -> bytes:
            return await self.readers.picture(kind, original, page, folder, side=side, max_bytes=cap, deadline=deadline)

        return await self._drawing(deadline, draw)

    async def sheet(self, layout: str, pages: Sequence[tuple[str, sqlite3.Row]], first: int) -> bytes:
        """`pages`: (job kind, record) in the draft's order; each is drawn, then composed."""
        deadline = self.mono() + VIEW_SECONDS
        side = OVERVIEW_SIDE if layout == "overview" else STRIP_SIDE

        async def draw(folder: Path) -> bytes:
            drawn = []
            for index, (kind, record) in enumerate(pages):
                part = folder / f"part-{index}"
                part.mkdir(mode=0o700)
                original = self._source(kind, record)
                data = await self.readers.picture(
                    kind, original, 1, part, side=side, max_bytes=SIZES["large"][1] * 4, deadline=deadline
                )
                path = folder / f"page-{index}.jpg"
                path.write_bytes(data)
                drawn.append(path)
            return await self.readers.sheet(layout, drawn, first, folder, max_bytes=SHEET_BYTES, deadline=deadline)

        return await self._drawing(deadline, draw)
