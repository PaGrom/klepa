"""Signed snapshots of core.db (docs/architecture.md: Storage, D24).

VACUUM INTO writes a consistent copy on the local disk. A manifest with the SHA-256 and a monotonic
generation number is signed with the snapshot key; only then is the snapshot copied into the documents
folder. On both sides a snapshot is built under a `.partial-` name and renamed only when it is complete, so an
incomplete snapshot never appears under a final name. Retention keeps the latest good snapshot of each of the
last 14 days and the first good one of each of the last 12 months; a snapshot that failed its integrity check
stays two weeks for diagnosis. Nothing outside the snapshot folders is ever touched.

Every method opens its own connection to core.db, so Core runs them on worker threads: `take` and `sweep`
with asyncio.to_thread, `copy_pending` and `prune` through DocumentsFolder, because they touch that folder.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .config import Config
from .db import connect, transaction
from .durable import fsync_dir, full_fsync, write_exclusive
from .events import EventLog, utc_now_iso
from .keys import ensure_private_dir
from .signing import canonical_json, sign, verify

MANIFEST_FORMAT = 1
DOCUMENTS_SUBDIR = Path("_klepa") / "snapshots"
FILES = ("core.db", "manifest.json", "manifest.sig")
KEEP_DAILY = 14
KEEP_MONTHLY = 12
PARTIAL_PREFIX = ".partial-"
_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-g\d{6}$")


class SnapshotError(Exception):
    """A snapshot could not be taken or does not verify."""


@dataclass(frozen=True)
class SnapshotInfo:
    generation: int
    day: str
    name: str
    sha256: str
    size: int
    integrity: str


def dir_name(day: str, generation: int) -> str:
    return f"{day}-g{generation:06d}"


def check_integrity(path: Path) -> str:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as copy:
        rows = [str(row[0]) for row in copy.execute("PRAGMA integrity_check").fetchall()]
    return "ok" if rows == ["ok"] else "; ".join(rows[:3])


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _drop_host_text(path: Path) -> None:
    """The host's messages that wait in outbound stay out of snapshots: snapshots go to the documents folder, and
    a held answer is part of a family conversation. In the copy they count as not sent, so a restore never hands
    the worker a message without text. secure_delete overwrites the bytes the updates free."""
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as copy:
        copy.execute("PRAGMA secure_delete=ON")
        copy.execute(
            "UPDATE outbound SET state='FAILED', last_error='snapshot' "
            "WHERE origin='host' AND state IN ('PENDING','SENDING','RETRY_WAIT')"
        )
        copy.execute("UPDATE outbound SET payload='{}' WHERE origin='host' AND payload != '{}'")


def _flush(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        full_fsync(fd)
    finally:
        os.close(fd)


def verify_snapshot(directory: Path, key: bytes) -> dict[str, Any]:
    """Check the signature, the checksum and SQLite's own integrity check; return the manifest."""
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        signature = (directory / "manifest.sig").read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"unreadable manifest: {type(exc).__name__}") from None
    if not isinstance(manifest, dict) or not verify(key, manifest, signature):
        raise SnapshotError("manifest signature does not verify")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise SnapshotError("unknown manifest format")
    if _sha256(directory / "core.db") != (manifest.get("sha256"), manifest.get("size")):
        raise SnapshotError("core.db does not match the manifest")
    if check_integrity(directory / "core.db") != "ok":
        raise SnapshotError("core.db fails the integrity check")
    return manifest


def keep_generations(rows: Iterable[tuple[int, str]]) -> set[int]:
    """The latest snapshot of each of the last KEEP_DAILY days and the first of each of the last KEEP_MONTHLY months."""
    latest_of_day: dict[str, int] = {}
    for generation, day in rows:
        latest_of_day[day] = max(generation, latest_of_day.get(day, 0))
    days = sorted(latest_of_day)
    keep = {latest_of_day[day] for day in days[-KEEP_DAILY:]}
    first_of_month: dict[str, int] = {}
    for day in days:
        first_of_month.setdefault(day[:7], latest_of_day[day])
    keep.update(first_of_month[month] for month in sorted(first_of_month)[-KEEP_MONTHLY:])
    return keep


def _remove(base: Path, name: str) -> None:
    """Remove one snapshot folder and its partial sibling, nothing else: no symlinks, nothing outside `base`."""
    if not _NAME.match(name):
        raise ValueError(f"not a snapshot folder name: {name!r}")
    for candidate in (name, PARTIAL_PREFIX + name):
        path = base / candidate
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != base.resolve():
            continue
        shutil.rmtree(path)


class Snapshotter:
    def __init__(self, cfg: Config, key: bytes, *, clock: Callable[[], float] = time.time) -> None:
        self.db_path = cfg.core_db_path
        self.key = key
        self.clock = clock
        self.tz = ZoneInfo(cfg.timezone)
        self.root = cfg.data_dir / "snapshots"
        self.documents_dir = cfg.documents_dir
        self.documents_root = cfg.documents_dir / DOCUMENTS_SUBDIR

    def take(self) -> SnapshotInfo:
        """Write, check and sign a snapshot of core.db on the local disk.

        Raises SnapshotError when the copy fails SQLite's integrity check; such a snapshot is kept for
        diagnosis but never copied into the documents folder.
        """
        day = datetime.fromtimestamp(self.clock(), self.tz).date().isoformat()
        ensure_private_dir(self.root)
        with contextlib.closing(connect(self.db_path)) as conn:
            with transaction(conn):
                cursor = conn.execute("INSERT INTO snapshot(day, created_at) VALUES (?, ?)", (day, utc_now_iso()))
            assert cursor.lastrowid is not None
            generation = cursor.lastrowid
            name = dir_name(day, generation)
            partial = self.root / (PARTIAL_PREFIX + name)
            try:
                partial.mkdir(mode=0o700)
                db_path = partial / "core.db"
                conn.execute("VACUUM INTO ?", (str(db_path),))
                _drop_host_text(db_path)
                _flush(db_path)
                integrity = check_integrity(db_path)
                digest, size = _sha256(db_path)
                manifest = {
                    "format": MANIFEST_FORMAT,
                    "generation": generation,
                    "day": day,
                    "created_at": utc_now_iso(),
                    "sha256": digest,
                    "size": size,
                    "integrity": integrity,
                    "schema_version": int(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]),
                    "engine_version": __version__,
                    "privacy_journal_head": None,  # the privacy journal arrives in stage 3
                }
                write_exclusive(partial, "manifest.json", canonical_json(manifest))
                write_exclusive(partial, "manifest.sig", sign(self.key, manifest).encode("ascii"))
                os.rename(partial, self.root / name)
                fsync_dir(self.root)
            except BaseException:
                shutil.rmtree(partial, ignore_errors=True)
                conn.execute("DELETE FROM snapshot WHERE generation=?", (generation,))
                raise
            conn.execute(
                "UPDATE snapshot SET sha256=?, size=?, integrity=? WHERE generation=?",
                (digest, size, integrity, generation),
            )
            EventLog(conn).log("snapshot_taken", {"generation": generation, "integrity": integrity})
        if integrity != "ok":
            raise SnapshotError(f"generation {generation} fails the integrity check")
        return SnapshotInfo(generation, day, name, digest, size, integrity)

    def sweep(self) -> int:
        """At start, after a crash: remove half-built snapshots, and keep one whose rename finished before its
        row was updated. Returns how many rows were dropped; their generation numbers are never reused."""
        if self.root.is_dir():
            for path in self.root.iterdir():
                if path.name.startswith(PARTIAL_PREFIX) and path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
        dropped = 0
        with contextlib.closing(connect(self.db_path)) as conn:
            for row in conn.execute("SELECT generation, day FROM snapshot WHERE sha256 IS NULL").fetchall():
                name = dir_name(row["day"], row["generation"])
                try:
                    manifest = verify_snapshot(self.root / name, self.key)
                except SnapshotError:
                    _remove(self.root, name)
                    conn.execute("DELETE FROM snapshot WHERE generation=?", (row["generation"],))
                    dropped += 1
                    continue
                conn.execute(
                    "UPDATE snapshot SET sha256=?, size=?, integrity=? WHERE generation=?",
                    (manifest["sha256"], manifest["size"], manifest["integrity"], row["generation"]),
                )
            if dropped:
                EventLog(conn).log("snapshots_swept", {"count": dropped})
        return dropped

    def copy_pending(self) -> int:
        """Copy good local snapshots that are not in the documents folder yet, oldest first; return how many.

        An OSError (the folder is unavailable) stops the pass. A SnapshotError (the folder holds something else
        under our name) holds back only that snapshot and is raised after the newer ones are copied.
        """
        copied = 0
        conflict: SnapshotError | None = None
        with contextlib.closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT generation, day FROM snapshot WHERE copied_at IS NULL AND integrity='ok' ORDER BY generation"
            ).fetchall()
            for row in rows:
                try:
                    self._copy(dir_name(row["day"], row["generation"]))
                except SnapshotError as exc:
                    conflict = exc
                    continue
                conn.execute("UPDATE snapshot SET copied_at=? WHERE generation=?", (utc_now_iso(), row["generation"]))
                copied += 1
        if conflict is not None:
            raise conflict
        return copied

    def _copy(self, name: str) -> None:
        if not (self.root / name).is_dir():
            raise SnapshotError(f"the local snapshot {name} is missing")
        if not self.documents_dir.is_dir():
            # Never recreate a missing root: it may live on a volume that is not mounted yet.
            raise FileNotFoundError(errno.ENOENT, "documents folder is missing")
        target_root = self.documents_dir
        for part in DOCUMENTS_SUBDIR.parts:
            target_root = target_root / part
            target_root.mkdir(exist_ok=True)
        final = target_root / name
        if final.is_dir():
            verify_snapshot(final, self.key)  # an earlier copy got this far before Core stopped
            return
        partial = target_root / (PARTIAL_PREFIX + name)
        shutil.rmtree(partial, ignore_errors=True)  # left by an interrupted earlier copy
        partial.mkdir()
        for file_name in FILES:
            write_exclusive(partial, file_name, (self.root / name / file_name).read_bytes())
        verify_snapshot(partial, self.key)
        os.rename(partial, final)
        fsync_dir(target_root)

    def prune(self) -> list[int]:
        """Remove snapshots that retention no longer keeps, locally and in the documents folder.

        Retention counts good snapshots only, so the newest good one always stays; a snapshot that failed its
        integrity check stays KEEP_DAILY days for diagnosis.
        """
        today = datetime.fromtimestamp(self.clock(), self.tz).date()
        cutoff = (today - timedelta(days=KEEP_DAILY)).isoformat()
        removed: list[int] = []
        with contextlib.closing(connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT generation, day, integrity, copied_at FROM snapshot WHERE sha256 IS NOT NULL"
            ).fetchall()
            good = [(int(row["generation"]), str(row["day"])) for row in rows if row["integrity"] == "ok"]
            keep = keep_generations(good)
            for row in rows:
                generation = int(row["generation"])
                if generation in keep or (row["integrity"] != "ok" and row["day"] >= cutoff):
                    continue
                if row["copied_at"] is not None and not self.documents_dir.is_dir():
                    continue  # its copy cannot be removed now; try again next time
                name = dir_name(row["day"], generation)
                _remove(self.root, name)
                _remove(self.documents_root, name)
                conn.execute("DELETE FROM snapshot WHERE generation=?", (generation,))
                removed.append(generation)
            if removed:
                EventLog(conn).log("snapshots_pruned", {"generations": removed})
        return removed
