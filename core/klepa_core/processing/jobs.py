"""Processing jobs (spec 5.4, 6.1 item 4): one per original a reader can read, read one at a time in the order they
came, their texts stored page by page and indexed for search.

The record follows its job (spec 5.4): `stored` → `processing` → `processed` or `failed`. A job a crash interrupted is
queued again at the next start, a few times at most: an original that crashes its reader every time must not stop
the queue. A job whose program is gone since the start waits, and its kind with it, for a start that has the program.
The person may cancel a job (spec 6.1 item 6): a queued one never runs, a running one's reader is killed, and a result
that comes after the cancel is dropped, so the record stays as it was stored. The event log says which job, its kind,
pages and time, or the error's name; never a text.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from ..aio import until_stopped
from ..db import transaction
from ..events import EventLog
from ..search import index_pages
from .readers import Pages, ReaderFailed, ReaderMissing, Readers
from .sandbox import ProcessingStopped

MAX_ATTEMPTS = 3  # starts of one job; a crash in the middle of each is the original's fault
KINDS = frozenset({"voice", "video", "pdf", "image", "document", "sheet", "text"})
RECORDING_KINDS = frozenset({"voice", "video"})  # read by a worker of their own: a voice note never waits for a scan
IDLE_SECONDS = 60.0  # the worker looks at the queue at least this often, even without a wake-up
_TEXT_SUFFIXES = frozenset({".txt", ".md", ".csv"})
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv"})
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic", ".heif"})
# What macOS's textutil reads: Word's old and new formats, RTF, OpenDocument's text, HTML.
_DOCUMENT_SUFFIXES = frozenset({".doc", ".docx", ".rtf", ".odt", ".html", ".htm"})
_DOCUMENT_TYPES = frozenset(
    {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/rtf",
        "text/rtf",
        "application/vnd.oasis.opendocument.text",
        "text/html",
    }
)
# What Core's own reader of spreadsheets reads: Excel's xlsx and xlsm, OpenDocument's ods; not the old binary xls.
_SHEET_SUFFIXES = frozenset({".xlsx", ".xlsm", ".ods"})
_SHEET_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/vnd.oasis.opendocument.spreadsheet",
    }
)


def job_kind(kind: str, mime: str | None, name: str | None) -> str | None:
    """Which reader reads an original, by what Telegram said it is; None: none of them."""
    mime = (mime or "").lower()
    suffix = Path(name).suffix.lower() if name else ""
    if kind in ("voice", "audio") or mime.startswith("audio/"):
        return "voice"
    if kind == "video" or mime.startswith("video/"):  # a video, a video note, an animation: their sound
        return "video"
    if mime == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if kind == "photo" or mime.startswith("image/") or suffix in _IMAGE_SUFFIXES:
        return "image"
    if mime in _DOCUMENT_TYPES or suffix in _DOCUMENT_SUFFIXES:
        return "document"
    if mime in _SHEET_TYPES or suffix in _SHEET_SUFFIXES:
        return "sheet"
    if kind == "text" or mime in _TEXT_TYPES or suffix in _TEXT_SUFFIXES:
        return "text"
    return None


def remove(folder: Path) -> None:
    """Remove a job's folder with all its reader left there, even what the reader made hard to remove (finding I4): a
    folder it took the owner's rights from, a file or folder it marked unchangeable. Those are given back and the
    removal tried once more; OSError when that is not enough."""
    tried: set[str] = set()

    def give_back(function: Callable[..., object], path: str, error: BaseException) -> None:
        if not isinstance(error, PermissionError) or path in tried:
            raise error
        tried.add(path)
        for target in (os.path.dirname(path), path):
            _unlock(target)
        if function in (os.lstat, os.open, os.scandir):  # a folder it could not enter: removed on its own
            shutil.rmtree(path, onexc=give_back)
        else:
            function(path)

    if os.path.lexists(folder):
        shutil.rmtree(folder, onexc=give_back)


def _unlock(path: str) -> None:
    """No unchangeable flag, and the owner's rights on a folder."""
    if hasattr(os, "lchflags"):  # macOS's flags: a reader may set them on what it wrote
        with contextlib.suppress(OSError):
            os.lchflags(path, 0)
    with contextlib.suppress(OSError):
        if stat.S_ISDIR(os.lstat(path).st_mode):
            os.chmod(path, stat.S_IRWXU)


@dataclass(frozen=True)
class Job:
    id: int
    evidence_id: str
    kind: str
    attempts: int


class Jobs:
    """The queue, in core.db: what is waiting, running and finished survives a restart."""

    def __init__(self, conn: sqlite3.Connection, events: EventLog, *, clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.events = events
        self.clock = clock
        self.wake = asyncio.Event()  # set by add: the worker stops waiting

    def add(self, evidence_id: str, kind: str) -> bool:
        """Queue an original's job; False when it has one already."""
        added = self.conn.execute(
            "INSERT INTO job(evidence_id, kind, state, created_at) VALUES (?, ?, 'queued', ?) "
            "ON CONFLICT(evidence_id) DO NOTHING",
            (evidence_id, kind, self.clock()),
        )
        if added.rowcount != 1:
            return False
        self.wake.set()
        return True

    def claim(self, kinds: frozenset[str] = KINDS) -> Job | None:
        """The oldest queued job of these kinds, now running; None also when no kind is given."""
        if not kinds:
            return None
        marks = ",".join("?" * len(kinds))
        with transaction(self.conn):
            row = self.conn.execute(
                f"SELECT id, evidence_id, kind, attempts FROM job WHERE state='queued' AND kind IN ({marks}) "
                "ORDER BY id LIMIT 1",
                sorted(kinds),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE job SET state='running', attempts=attempts+1, started_at=? WHERE id=?",
                (self.clock(), row["id"]),
            )
            self.conn.execute(
                "UPDATE evidence SET state='processing' WHERE id=? AND state='stored'", (row["evidence_id"],)
            )
        return Job(row["id"], row["evidence_id"], row["kind"], row["attempts"] + 1)

    def release(self, job: Job) -> None:
        """Back to the queue, its attempt not counted: Core stopped, the original did nothing wrong."""
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE job SET state='queued', attempts=attempts-1, started_at=NULL WHERE id=? AND state='running'",
                (job.id,),
            )
            self.conn.execute(
                "UPDATE evidence SET state='stored' WHERE id=? AND state='processing'", (job.evidence_id,)
            )

    def done(self, job: Job, pages: Pages) -> bool:
        """The texts, their index rows, the job and its record: together or not at all. False when the job was
        cancelled meanwhile: its late result is dropped (spec 6.1 item 6)."""
        now = self.clock()
        with transaction(self.conn):
            ended = self.conn.execute(
                "UPDATE job SET state='done', pages=?, note=?, error=NULL, ended_at=? WHERE id=? AND state='running'",
                (len(pages.texts), pages.note, now, job.id),
            )
            if ended.rowcount != 1:
                return False
            self.conn.executemany(
                "INSERT INTO evidence_text(evidence_id, page, text, extractor, created_at) VALUES (?, ?, ?, ?, ?)",
                [(job.evidence_id, n, text, pages.extractor, now) for n, text in enumerate(pages.texts, 1) if text],
            )
            index_pages(self.conn, job.evidence_id, pages.texts)
            self._mark(job.evidence_id, "processed")
        return True

    def failed(self, job: Job, error: str) -> bool:
        """False when the job was cancelled meanwhile."""
        with transaction(self.conn):
            ended = self.conn.execute(
                "UPDATE job SET state='failed', error=?, ended_at=? WHERE id=? AND state='running'",
                (error, self.clock(), job.id),
            )
            if ended.rowcount != 1:
                return False
            self._mark(job.evidence_id, "failed")
        return True

    def cancel(self, evidence_ids: Collection[str]) -> list[int]:
        """The person cancelled the reading of these originals: their queued and running jobs end 'cancelled' now,
        and the records stay as they were stored. Returns the jobs that were running: the worker stops their readers."""
        marks = ",".join("?" * len(evidence_ids))
        with transaction(self.conn):
            rows = self.conn.execute(
                f"SELECT id, evidence_id, state FROM job WHERE evidence_id IN ({marks}) "
                "AND state IN ('queued', 'running')",
                list(evidence_ids),
            ).fetchall()
            for row in rows:
                self.conn.execute("UPDATE job SET state='cancelled', ended_at=? WHERE id=?", (self.clock(), row["id"]))
                self.conn.execute(
                    "UPDATE evidence SET state='stored' WHERE id=? AND state='processing'", (row["evidence_id"],)
                )
        if rows:
            self.events.log("jobs_cancelled", {"jobs": [int(row["id"]) for row in rows]})
        return [int(row["id"]) for row in rows if row["state"] == "running"]

    def _mark(self, evidence_id: str, state: str) -> None:
        # Only a record that is still being read: one deleted meanwhile stays deleted.
        self.conn.execute(
            "UPDATE evidence SET state=? WHERE id=? AND state IN ('stored', 'processing')", (state, evidence_id)
        )

    def recover(self) -> int:
        """At start: jobs a crash interrupted run again, unless they used their attempts. Returns how many."""
        now = self.clock()
        with transaction(self.conn):
            running = self.conn.execute("SELECT id, evidence_id, attempts FROM job WHERE state='running'").fetchall()
            spent = [row for row in running if row["attempts"] >= MAX_ATTEMPTS]
            again = [row for row in running if row["attempts"] < MAX_ATTEMPTS]
            for row in spent:
                self.conn.execute(
                    "UPDATE job SET state='failed', error='interrupted', ended_at=? WHERE id=?", (now, row["id"])
                )
                self._mark(row["evidence_id"], "failed")
            for row in again:
                self.conn.execute("UPDATE job SET state='queued', started_at=NULL WHERE id=?", (row["id"],))
                self.conn.execute(
                    "UPDATE evidence SET state='stored' WHERE id=? AND state='processing'", (row["evidence_id"],)
                )
            if running:
                self.events.log("jobs_recovered", {"queued": len(again), "failed": len(spent)})
        return len(again)

    def backfill(self) -> int:
        """Jobs for the stored originals that have none: those that came before processing was set up, and one whose
        job a crash kept from being queued. Returns how many."""
        rows = self.conn.execute(
            "SELECT e.id, e.kind, e.mime, e.original_name FROM evidence e LEFT JOIN job j ON j.evidence_id = e.id "
            "WHERE j.id IS NULL AND e.state = 'stored' ORDER BY e.received_at, e.rowid"
        ).fetchall()
        added = 0
        with transaction(self.conn):
            for row in rows:
                kind = job_kind(row["kind"], row["mime"], row["original_name"])
                if kind is not None and self.add(row["id"], kind):
                    added += 1
            if added:
                self.events.log("jobs_backfilled", {"jobs": added})
        return added


class Worker:
    """Reads the queued originals one at a time. A reader gets a copy of its original in the job's own folder, which
    is removed with everything the reader wrote once the job ends, off the event loop: it may hold gigabytes."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        jobs: Jobs,
        readers: Readers,
        incoming_dir: Path,
        jobs_dir: Path,
        events: EventLog,
        *,
        kinds: frozenset[str] = KINDS,
        on_missing: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.conn = conn
        self.jobs = jobs
        self.kinds = kinds  # what can be read now: the others wait for a start that has what they lack
        self.on_missing = on_missing  # told the kind and the program when a program is gone since the start
        self.readers = readers
        self.incoming_dir = incoming_dir
        self.jobs_dir = jobs_dir
        self.events = events
        self.clock = clock
        self.on_end: Callable[[], None] | None = None  # told when a job ends: its message may be ready for the host
        self._current: Job | None = None
        self._reading: asyncio.Future[Pages] | None = None

    def stop_reading(self, job_ids: Collection[int]) -> None:
        """Jobs the person cancelled (spec 6.1 item 6): the running one's reader is killed with its process group."""
        if self._current is not None and self._current.id in job_ids and self._reading is not None:
            self._reading.cancel()

    def sweep(self) -> None:
        """At start: what a crash left in the jobs folder is copies of originals and what readers wrote of them. A
        folder that cannot be removed is moved aside, so that Core still starts (finding I4); one moved aside at an
        earlier start is tried again. OSError when even the move fails."""
        for stale in self.jobs_dir.parent.glob(self.jobs_dir.name + ".stale-*"):
            with contextlib.suppress(OSError):
                remove(stale)
        try:
            remove(self.jobs_dir)
        except OSError:
            aside = self.jobs_dir.with_name(f"{self.jobs_dir.name}.stale-{time.time_ns()}")
            self.jobs_dir.rename(aside)
            self.events.log("jobs_folder_left", {"folder": aside.name})
        self.jobs_dir.mkdir(mode=0o700, parents=True)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.jobs.wake.clear()  # before the claim: a job added while one is read is not missed
            if not await until_stopped(stop, self.run_once()):
                await until_stopped(stop, self._idle())

    async def _idle(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.jobs.wake.wait(), IDLE_SECONDS)

    async def run_once(self) -> bool:
        """Read the oldest queued original. False when there was none."""
        job = self.jobs.claim(self.kinds)
        if job is None:
            return False
        started = self.clock()
        folder = self.jobs_dir / str(job.id)
        self._current = job
        self._reading = asyncio.ensure_future(self._read(job, folder))
        try:
            pages = await self._reading
            if not self.jobs.done(job, pages):
                self._cancelled(job, started)
                return True
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                self.jobs.release(job)  # Core is stopping: the job is read at the next start, as if never begun
                raise
            self._cancelled(job, started)  # only its reading was stopped: the person cancelled it
            return True
        except ReaderMissing as e:
            self._missing(job, str(e))
        except (ReaderFailed, ProcessingStopped) as e:
            self._failed(job, str(e), started)
        except Exception as e:  # one original must not stop the queue; the error's type names it, never its text
            self._failed(job, type(e).__name__, started)
        else:
            self.events.log(
                "job_done",
                {
                    "job": job.id,
                    "evidence_id": job.evidence_id,
                    "kind": job.kind,
                    "pages": len(pages.texts),
                    "seconds": round(self.clock() - started, 1),
                    "extractor": pages.extractor,
                },
            )
        finally:
            self._current, self._reading = None, None
            if self.on_end is not None:  # the result is written: its message need not wait for the folder to go
                self.on_end()
            await self._clear(job, folder)
        return True

    async def _clear(self, job: Job, folder: Path) -> None:
        try:
            await asyncio.to_thread(remove, folder)
        except Exception as e:  # left for the next start's sweep; the queue goes on
            self.events.log("job_folder_left", {"job": job.id, "error": type(e).__name__})

    async def _read(self, job: Job, folder: Path) -> Pages:
        row = self.conn.execute("SELECT state, incoming_path FROM evidence WHERE id=?", (job.evidence_id,)).fetchone()
        if row is None or row["state"] != "processing":
            raise ReaderFailed("the record is not kept")  # deleted or expired while it waited
        source = self.incoming_dir / row["incoming_path"] if row["incoming_path"] else None
        if source is None or not source.is_file():
            raise ReaderFailed("the original is missing")
        if os.path.lexists(folder):
            await asyncio.to_thread(remove, folder)
        folder.mkdir(mode=0o700, parents=True)
        return await self.readers.read(job.kind, source, folder)

    def _missing(self, job: Job, program: str) -> None:
        """A program a reader needs is gone since the start (Homebrew replaced it): the job waits, its attempt not
        counted, and so does its kind, for a start that has the program (finding M8)."""
        self.jobs.release(job)
        self.kinds = self.kinds - {job.kind}
        self.events.log("reader_missing", {"job": job.id, "kind": job.kind, "program": program})
        if self.on_missing is not None:
            self.on_missing(job.kind, program)

    def _cancelled(self, job: Job, started: float) -> None:
        self.events.log(
            "job_cancelled",
            {
                "job": job.id,
                "evidence_id": job.evidence_id,
                "kind": job.kind,
                "seconds": round(self.clock() - started, 1),
            },
        )

    def _failed(self, job: Job, error: str, started: float) -> None:
        if not self.jobs.failed(job, error):
            self._cancelled(job, started)
            return
        self.events.log(
            "job_failed",
            {
                "job": job.id,
                "evidence_id": job.evidence_id,
                "kind": job.kind,
                "error": error,
                "seconds": round(self.clock() - started, 1),
            },
        )
