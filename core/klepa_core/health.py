"""What the owner sees: the documents folder probe and the status line (docs/architecture.md: Service bot).

The line carries counts, times and hashes only: no file names, captions or other family data.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .documents import DocumentsFolder, DocumentsTimeout
from .durable import write_exclusive
from .journal import InboundJournal

PROBE_PREFIX = ".klepa-probe-"
STALE_PROBE_SECONDS = 600.0
SNAPSHOT_MAX_AGE_SECONDS = 48 * 3600
UNKNOWN_WINDOW_SECONDS = 48 * 3600  # an unconfirmed send is reported in the daily lines of two days


@dataclass(frozen=True)
class Probe:
    ok: bool
    error: str | None = None
    code: int | None = None  # errno

    @property
    def denied(self) -> bool:
        return self.code in (errno.EPERM, errno.EACCES)


def probe_documents(documents_dir: Path) -> Probe:
    """Write, read back, list and remove a probe file with a name of its own; never create the documents root.

    Listing matters: macOS lets a background process without permission write a known path in a protected
    folder but refuses to list it. Blocking: Core runs it on the documents folder's worker.
    """
    name = PROBE_PREFIX + secrets.token_hex(8)
    data = secrets.token_bytes(16)
    try:
        if not documents_dir.is_dir():
            raise FileNotFoundError(errno.ENOENT, "documents folder is missing")
        write_exclusive(documents_dir, name, data)
        try:
            if (documents_dir / name).read_bytes() != data:
                return Probe(False, "ProbeMismatch")
            names = os.listdir(documents_dir)
            if name not in names:
                return Probe(False, "ProbeNotListed")
            _remove_stale_probes(documents_dir, names)
        finally:
            with contextlib.suppress(OSError):
                (documents_dir / name).unlink()
    except OSError as exc:
        return Probe(False, type(exc).__name__, exc.errno)
    return Probe(True)


def _remove_stale_probes(documents_dir: Path, names: list[str]) -> None:
    """Remove probe files left by a Core that was stopped while its probe hung; never anything else."""
    now = time.time()
    for name in names:
        if not name.startswith(PROBE_PREFIX):
            continue
        path = documents_dir / name
        with contextlib.suppress(OSError):
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and now - info.st_mtime > STALE_PROBE_SECONDS:
                path.unlink()


async def check_documents(documents: DocumentsFolder) -> Probe:
    """The probe on the documents folder's worker; a folder that does not answer in time is unavailable."""
    try:
        return await documents.call(probe_documents, documents.root)
    except DocumentsTimeout as exc:
        return Probe(False, type(exc).__name__, exc.errno)


class Health:
    def __init__(
        self, cfg: Config, conn: sqlite3.Connection, journal: InboundJournal, clock: Callable[[], float] = time.time
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.journal = journal
        self.clock = clock
        self.tz = ZoneInfo(cfg.timezone)

    def _local(self, iso: str) -> str:
        return datetime.fromisoformat(iso).astimezone(self.tz).strftime("%Y-%m-%d %H:%M")

    def _count(self, sql: str, *args: object) -> int:
        return int(self.conn.execute(sql, args).fetchone()[0])

    def _snapshot(self, now: float) -> tuple[str, bool]:
        """The newest snapshot as the line shows it, and whether snapshots are healthy."""
        loc = self.cfg.locale
        row = self.conn.execute(
            "SELECT generation, sha256, integrity FROM snapshot WHERE sha256 IS NOT NULL "
            "ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if row is None:
            text = loc.service_text("no_snapshot")
        else:
            integrity = loc.service_text("ok") if row["integrity"] == "ok" else str(row["integrity"])
            text = loc.service_text(
                "snapshot", generation=row["generation"], hash=str(row["sha256"])[:8], integrity=integrity
            )
            if row["integrity"] != "ok":
                return text, False
        if self.cfg.snapshot_at is None:
            return text, True  # snapshots are off
        newest_good = self.conn.execute("SELECT MAX(created_at) FROM snapshot WHERE integrity='ok'").fetchone()[0]
        first_start = self.conn.execute("SELECT MIN(at) FROM event_log WHERE kind='core_started'").fetchone()[0]
        since = newest_good or first_start
        return text, since is None or now - datetime.fromisoformat(since).timestamp() <= SNAPSHOT_MAX_AGE_SECONDS

    def line(self, probe: Probe) -> tuple[str, bool]:
        """The status line, and whether everything checked is fine."""
        loc = self.cfg.locale
        now = self.clock()
        last = self.journal.last_received_at()
        pending = self._count("SELECT COUNT(*) FROM evidence WHERE copy_state='pending'")
        failed = self._count("SELECT COUNT(*) FROM evidence WHERE copy_state='failed'")
        window = datetime.fromtimestamp(now - UNKNOWN_WINDOW_SECONDS, UTC).isoformat(timespec="milliseconds")
        unknown = self._count("SELECT COUNT(*) FROM outbound WHERE state='UNKNOWN' AND updated_at >= ?", window)
        snapshot, snapshot_ok = self._snapshot(now)
        fine = probe.ok and failed == 0 and unknown == 0 and snapshot_ok
        documents = loc.service_text("ok") if probe.ok else loc.service_text("unavailable", error=probe.error)
        text = loc.service_text(
            "line",
            headline=loc.service_text("all_good" if fine else "attention"),
            last_intake=self._local(last) if last else loc.service_text("never"),
            snapshot=snapshot,
            documents=documents,
            pending_copies=pending,
            failed_copies=failed,
            unknown_sends=unknown,
        )
        return text, fine
