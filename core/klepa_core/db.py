"""core.db: one SQLite database for Core state (docs/architecture.md: D16)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .names import sanitize_original_name

SCHEMA_V1 = (
    """CREATE TABLE member (
        person_id TEXT PRIMARY KEY,
        telegram_id INTEGER NOT NULL UNIQUE,
        name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('owner','member')))""",
    """CREATE TABLE space (
        space_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('shared','personal')),
        owner_person_id TEXT REFERENCES member(person_id),
        folder TEXT NOT NULL UNIQUE)""",
    """CREATE TABLE evidence (
        id TEXT PRIMARY KEY,
        space_id TEXT NOT NULL REFERENCES space(space_id),
        kind TEXT NOT NULL CHECK (kind IN ('file','photo','voice','audio','video','text')),
        original_name TEXT,
        disk_name TEXT,
        incoming_path TEXT,
        documents_path TEXT,
        mime TEXT,
        size INTEGER,
        sha256 TEXT,
        received_at TEXT NOT NULL,
        message_date INTEGER,
        channel TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        update_id INTEGER NOT NULL,
        file_id TEXT,
        file_unique_id TEXT,
        media_group_id TEXT,
        authenticated_subject TEXT NOT NULL REFERENCES member(person_id),
        claimed_subject TEXT,
        ingest_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN
            ('stored','processing','processed','failed','quarantined','too_large','expired','deleted')),
        copy_state TEXT NOT NULL DEFAULT 'pending' CHECK (copy_state IN ('pending','copied','failed','none')),
        caption TEXT,
        tags TEXT NOT NULL DEFAULT '[]')""",
    """CREATE TABLE outbound (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT NOT NULL UNIQUE,
        origin TEXT NOT NULL CHECK (origin IN ('core','host')),
        method TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        payload TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('PENDING','SENDING','CONFIRMED','RETRY_WAIT','UNKNOWN','FAILED')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL,
        telegram_message_id INTEGER,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        data TEXT NOT NULL)""",
    "CREATE INDEX evidence_copy_state ON evidence(copy_state)",
    "CREATE INDEX outbound_state ON outbound(state, next_attempt_at)",
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA fullfsync=ON")
    conn.execute("PRAGMA checkpoint_fullfsync=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    current = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0])
    if current < 1:
        with transaction(conn):
            for statement in SCHEMA_V1:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version(version) VALUES (1)")
        current = 1
    return current


def seed(conn: sqlite3.Connection, cfg: Config) -> None:
    """Insert members and their spaces from the config. Idempotent; a folder clash fails loudly."""
    with transaction(conn):
        for m in cfg.members:
            conn.execute(
                "INSERT INTO member(person_id, telegram_id, name, role) VALUES (?,?,?,?) "
                "ON CONFLICT(person_id) DO UPDATE SET telegram_id=excluded.telegram_id, "
                "name=excluded.name, role=excluded.role",
                (m.person_id, m.telegram_id, m.name, m.role),
            )
        conn.execute(
            "INSERT INTO space(space_id, kind, owner_person_id, folder) VALUES ('shared', 'shared', NULL, ?) "
            "ON CONFLICT(space_id) DO NOTHING",
            (sanitize_original_name(cfg.shared_folder),),
        )
        for m in cfg.members:
            conn.execute(
                "INSERT INTO space(space_id, kind, owner_person_id, folder) VALUES (?, 'personal', ?, ?) "
                "ON CONFLICT(space_id) DO NOTHING",
                (f"personal:{m.person_id}", m.person_id, sanitize_original_name(m.name)),
            )
