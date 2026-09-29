"""Evidence store (docs/architecture.md: Storage): originals are written once and never changed.

Order of writes: the file lands in incoming/ first (a temporary file with F_FULLFSYNC, then a rename that
never replaces), then its row in core.db, then a verified copy and a signed card in the documents folder.
A row never points to a missing file, and a retry after a crash reuses the file under its stable id.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import hmac
import json
import mimetypes
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import cards
from .db import transaction
from .durable import make_dirs_durably, write_new_atomically
from .events import EventLog, utc_now_iso
from .names import disk_name


@dataclass(frozen=True)
class IncomingFile:
    update_id: int
    chat_id: int
    message_id: int
    message_date: int
    person_id: str
    space_id: str
    kind: str
    original_name: str | None
    mime: str | None
    data: bytes
    file_id: str | None
    file_unique_id: str | None
    media_group_id: str | None
    caption: str | None
    tags: tuple[str, ...] = ()


class CopyConflict(Exception):
    """The documents folder already holds different content under our name."""


_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "application/pdf": ".pdf",
}


def fallback_name(kind: str, mime: str | None) -> str:
    """Telegram gives photos, voice and video notes no name: call them by kind, with an extension."""
    extension = _EXTENSIONS.get(mime or "") or (mimetypes.guess_extension(mime) if mime else None) or ""
    return kind + extension


def ingest_key(chat_id: int, message_id: int) -> str:
    return f"telegram:{chat_id}:{message_id}"


class EvidenceStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        incoming_dir: Path,
        documents_dir: Path,
        signing_key: bytes,
        timezone: str,
        events: EventLog,
    ) -> None:
        self.conn = conn
        self.incoming_dir = incoming_dir
        self.documents_dir = documents_dir
        self.key = signing_key
        self.tz = ZoneInfo(timezone)
        self.events = events
        self._deferred: dict[str, int | None] = {}

    def month(self, unix_seconds: int) -> str:
        return datetime.fromtimestamp(unix_seconds, self.tz).strftime("%Y/%m")

    def evidence_id_for(self, key: str) -> str:
        """Stable per message and installation, so a retry after a crash finds the file it already wrote."""
        mac = hmac.new(self.key, b"evidence-id\x00" + key.encode("utf-8"), hashlib.sha256)
        return "ev" + mac.hexdigest()[:20]

    def by_ingest_key(self, key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute("SELECT * FROM evidence WHERE ingest_key=?", (key,)).fetchone()
        return row

    async def ingest(self, f: IncomingFile) -> sqlite3.Row:
        """Store an original. Idempotent: one Telegram message is stored once."""
        key = ingest_key(f.chat_id, f.message_id)
        existing = self.by_ingest_key(key)
        if existing is not None:
            return existing
        evidence_id = self.evidence_id_for(key)
        name = disk_name(evidence_id, f.original_name or fallback_name(f.kind, f.mime))
        month = self.month(f.message_date)
        directory = make_dirs_durably(self.incoming_dir, month)
        digest = hashlib.sha256(f.data).hexdigest()
        await asyncio.to_thread(self._write_original, directory, name, f.data, digest)
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO evidence(id, space_id, kind, original_name, disk_name, incoming_path, mime, size,
                       sha256, received_at, message_date, channel, chat_id, message_id, update_id, file_id,
                       file_unique_id, media_group_id, authenticated_subject, ingest_key, state, copy_state,
                       caption, tags)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?, 'telegram', ?,?,?,?,?,?,?,?, 'stored', 'pending', ?, ?)""",
                (
                    evidence_id,
                    f.space_id,
                    f.kind,
                    f.original_name,
                    name,
                    f"{month}/{name}",
                    f.mime,
                    len(f.data),
                    digest,
                    utc_now_iso(),
                    f.message_date,
                    f.chat_id,
                    f.message_id,
                    f.update_id,
                    f.file_id,
                    f.file_unique_id,
                    f.media_group_id,
                    f.person_id,
                    key,
                    f.caption,
                    json.dumps(list(f.tags)),
                ),
            )
        self.events.log("evidence_stored", {"evidence_id": evidence_id, "kind": f.kind, "space_id": f.space_id})
        row = self.by_ingest_key(key)
        assert row is not None
        return row

    def record_too_large(self, f: IncomingFile, size: int | None) -> sqlite3.Row:
        """Record a file Telegram will not hand to bots (> 20 MB). No bytes are stored."""
        key = ingest_key(f.chat_id, f.message_id)
        existing = self.by_ingest_key(key)
        if existing is not None:
            return existing
        evidence_id = self.evidence_id_for(key)
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO evidence(id, space_id, kind, original_name, mime, size, received_at, message_date,
                       channel, chat_id, message_id, update_id, file_id, file_unique_id, media_group_id,
                       authenticated_subject, ingest_key, state, copy_state, caption)
                   VALUES (?,?,?,?,?,?,?,?, 'telegram', ?,?,?,?,?,?,?,?, 'too_large', 'none', ?)""",
                (
                    evidence_id,
                    f.space_id,
                    f.kind,
                    f.original_name,
                    f.mime,
                    size,
                    utc_now_iso(),
                    f.message_date,
                    f.chat_id,
                    f.message_id,
                    f.update_id,
                    f.file_id,
                    f.file_unique_id,
                    f.media_group_id,
                    f.person_id,
                    key,
                    f.caption,
                ),
            )
        self.events.log("evidence_too_large", {"evidence_id": evidence_id, "size": size})
        row = self.by_ingest_key(key)
        assert row is not None
        return row

    def album_spaces(self, chat_id: int, media_group_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT space_id FROM evidence WHERE chat_id=? AND media_group_id=?", (chat_id, media_group_id)
        )
        return {row["space_id"] for row in rows}

    def move_album(self, chat_id: int, media_group_id: str, space_id: str) -> int:
        """Put the album's items that are not copied yet into one space."""
        return self.conn.execute(
            "UPDATE evidence SET space_id=? WHERE chat_id=? AND media_group_id=? AND space_id<>? "
            "AND copy_state<>'copied'",
            (space_id, chat_id, media_group_id, space_id),
        ).rowcount

    async def copy_pending(self, ready: Callable[[sqlite3.Row], bool] | None = None) -> int:
        rows = self.conn.execute("SELECT * FROM evidence WHERE copy_state='pending' ORDER BY received_at").fetchall()
        copied = 0
        for row in rows:
            if ready is not None and not ready(row):
                continue
            if await self.copy_one(row):
                copied += 1
        return copied

    async def copy_one(self, row: sqlite3.Row) -> bool:
        """Copy one original and its signed card into the documents folder, verified by re-reading."""
        folder = self.conn.execute("SELECT folder FROM space WHERE space_id=?", (row["space_id"],)).fetchone()["folder"]
        month = row["incoming_path"].rsplit("/", 1)[0]
        relative = Path(folder) / month / row["disk_name"]
        card = cards.build_card(row)
        try:
            data = await asyncio.to_thread((self.incoming_dir / row["incoming_path"]).read_bytes)
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise CopyConflict("incoming checksum mismatch")
            await asyncio.to_thread(self._write_copy, relative.parent, row["disk_name"], data, card)
        except CopyConflict as exc:
            self._set_copy_state(row["id"], "failed")
            self.events.log("documents_copy_conflict", {"evidence_id": row["id"], "reason": str(exc)})
            return False
        except OSError as exc:
            if self._deferred.get(row["id"], -1) != exc.errno:
                self._deferred[row["id"]] = exc.errno
                self.events.log("documents_copy_deferred", {"evidence_id": row["id"], "errno": exc.errno})
            return False
        self._deferred.pop(row["id"], None)
        self._set_copy_state(row["id"], "copied", str(relative))
        return True

    @staticmethod
    def _write_original(directory: Path, name: str, data: bytes, digest: str) -> None:
        """Write the original once. A complete file left by an interrupted attempt is adopted; other bytes
        under our name are refused (FileExistsError) and never overwritten."""
        try:
            write_new_atomically(directory, name, data)
        except FileExistsError:
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
                raise

    def _write_copy(self, relative_dir: Path, name: str, data: bytes, card: dict[str, Any]) -> None:
        if not self.documents_dir.is_dir():
            # Never recreate a missing root: it may live on a volume that is not mounted yet.
            raise FileNotFoundError(errno.ENOENT, "documents folder is missing")
        directory = self.documents_dir
        for part in relative_dir.parts:
            directory = directory / part
            directory.mkdir(exist_ok=True)
        with contextlib.suppress(FileExistsError):  # an earlier attempt got this far; the check below decides
            write_new_atomically(directory, name, data)
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != card["sha256"]:
            raise CopyConflict("documents copy differs from the original")
        try:
            cards.write_card(directory, card, self.key)
        except FileExistsError:
            if cards.read_card(directory / cards.card_file_name(card["evidence_id"]), self.key) != card:
                raise CopyConflict("existing card differs") from None

    def _set_copy_state(self, evidence_id: str, state: str, documents_path: str | None = None) -> None:
        self.conn.execute(
            "UPDATE evidence SET copy_state=?, documents_path=COALESCE(?, documents_path) WHERE id=?",
            (state, documents_path, evidence_id),
        )
