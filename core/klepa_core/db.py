"""core.db: one SQLite database for Core state (docs/architecture.md: D16)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .names import sanitize_original_name
from .search import fold_sql

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

SCHEMA_V2 = (
    "ALTER TABLE outbound ADD COLUMN bot TEXT NOT NULL DEFAULT 'family' CHECK (bot IN ('family','service'))",
    """CREATE TABLE service_binding (
        person_id TEXT PRIMARY KEY REFERENCES member(person_id),
        chat_id INTEGER NOT NULL UNIQUE,
        bound_at TEXT NOT NULL)""",
    """CREATE TABLE button_action (
        id TEXT PRIMARY KEY,
        action TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        outbound_key TEXT NOT NULL REFERENCES outbound(idempotency_key),
        expires_at REAL NOT NULL,
        used_at TEXT)""",
    """CREATE TABLE alert_state (
        class TEXT PRIMARY KEY,
        last_sent_at REAL NOT NULL)""",
    # AUTOINCREMENT: a generation number is never handed out twice, even after its row is removed (D24).
    """CREATE TABLE snapshot (
        generation INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT NOT NULL,
        created_at TEXT NOT NULL,
        sha256 TEXT,
        size INTEGER,
        integrity TEXT,
        copied_at TEXT)""",
    """CREATE TABLE job_run (
        name TEXT PRIMARY KEY,
        last_day TEXT NOT NULL)""",
    "CREATE INDEX outbound_bot_state ON outbound(bot, state, next_attempt_at)",
)

# Stage 1c: the host behind the gatekeeper (docs/architecture.md: Host interface). No message text is stored here:
# core.db goes into snapshots, so a served message is built from the inbound journal when the host polls.
SCHEMA_V3 = (
    """CREATE TABLE host_message (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_update_id INTEGER UNIQUE,
        kind TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        date INTEGER NOT NULL,
        forwarded INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        answered_at REAL,
        UNIQUE (chat_id, message_id))""",
    # update_id is never handed out twice, not even after a restore: max(previous + 1, unix time).
    """CREATE TABLE host_update (
        update_id INTEGER PRIMARY KEY,
        host_message_id INTEGER NOT NULL UNIQUE REFERENCES host_message(id),
        issued_at REAL NOT NULL,
        acked_at REAL)""",
    """CREATE TABLE host_run (
        run_id TEXT PRIMARY KEY,
        boot_id TEXT NOT NULL,
        chat_id INTEGER,
        sender_id INTEGER,
        prompt_built_at REAL,
        host_message_id INTEGER REFERENCES host_message(id),
        registered_at REAL,
        expires_at REAL)""",
    """CREATE TABLE host_notice (
        chat_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        sent_at REAL NOT NULL,
        PRIMARY KEY (chat_id, kind))""",
    """CREATE TABLE host_setting (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL)""",
    "CREATE INDEX host_message_chat ON host_message(chat_id, id)",
    "CREATE INDEX outbound_origin_state ON outbound(origin, state)",
)

# Stage 2a: the model's calls of Core's tools. A call id is used once (spec 4.5); no parameters are kept here.
SCHEMA_V4 = (
    """CREATE TABLE host_tool_call (
        run_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        used_at REAL NOT NULL,
        PRIMARY KEY (run_id, tool_call_id))""",
    "ALTER TABLE host_run ADD COLUMN ended_at REAL",
    "ALTER TABLE host_run ADD COLUMN succeeded INTEGER",
)

# Stage 2b: attachments read in a sandbox (spec 5.4, 5.6, 6.1 item 4). A job per original, with a note when a limit
# left part of the original unread; the texts it yields, a page each; and the search index over the names, captions
# and texts of every record (page 0: name and caption). The texts are records of their originals, kept here like them
# (spec 5.3).
SCHEMA_V5 = (
    """CREATE TABLE job (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evidence_id TEXT NOT NULL UNIQUE REFERENCES evidence(id),
        kind TEXT NOT NULL CHECK (kind IN ('voice', 'video', 'pdf', 'image', 'document', 'sheet', 'text')),
        state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'failed', 'cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        pages INTEGER,
        note TEXT,
        created_at REAL NOT NULL,
        started_at REAL,
        ended_at REAL)""",
    "CREATE INDEX job_state ON job(state, id)",
    """CREATE TABLE evidence_text (
        evidence_id TEXT NOT NULL REFERENCES evidence(id),
        page INTEGER NOT NULL CHECK (page >= 1),
        text TEXT NOT NULL,
        extractor TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (evidence_id, page))""",
    """CREATE VIRTUAL TABLE evidence_fts USING fts5(
        body, name, caption, evidence_id UNINDEXED, page UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 2')""",
    "INSERT INTO evidence_fts(body, name, caption, evidence_id, page) "
    f"SELECT '', {fold_sql("COALESCE(original_name, '')")}, {fold_sql("COALESCE(caption, '')")}, id, 0 FROM evidence",
)


# Stage 2b-2: an attachment given to the host as text (spec 6.1 items 4-6). A batch of attachments is one host message
# whose items are its originals; it is issued once their reading ends, the person may cancel it, and Core's interim
# message about it ("listening…") is known by its outbound key and what it says now. A cancel word Core took is a host
# message of its own kind that the host is never given.
SCHEMA_V6 = (
    """CREATE TABLE host_message_item (
        host_message_id INTEGER NOT NULL REFERENCES host_message(id),
        position INTEGER NOT NULL,
        evidence_id TEXT NOT NULL UNIQUE REFERENCES evidence(id),
        PRIMARY KEY (host_message_id, position))""",
    "ALTER TABLE host_message ADD COLUMN cancelled_at REAL",
    "ALTER TABLE host_message ADD COLUMN interim_key TEXT",
    "ALTER TABLE host_message ADD COLUMN interim_state TEXT",
    "CREATE INDEX host_message_reading ON host_message(id) WHERE kind = 'attachment' AND answered_at IS NULL",
    "CREATE INDEX host_message_interim ON host_message(id) WHERE interim_state = 'shown'",
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


# Stage 2c-1: a document made from photos (spec 6.1 item 4, make_pdf). A draft holds a person's pages in an order they
# check on a sheet of numbered thumbnails; each change is a revision with its own pages and sheet, and Done builds the
# PDF of the current revision as a version of the document: a record Core derived, which names its sources. A batch is
# looked at once for a draft; the batches closed before this stage are not.
SCHEMA_V7 = (
    """CREATE TABLE draft (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        person_id TEXT NOT NULL REFERENCES member(person_id),
        state TEXT NOT NULL CHECK (state IN ('open', 'building', 'done', 'declined', 'failed')),
        revision INTEGER NOT NULL DEFAULT 1,
        reply_to INTEGER,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL)""",
    """CREATE TABLE draft_page (
        draft_id INTEGER NOT NULL REFERENCES draft(id),
        revision INTEGER NOT NULL,
        position INTEGER NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence(id),
        PRIMARY KEY (draft_id, revision, position))""",
    """CREATE TABLE draft_sheet (
        draft_id INTEGER NOT NULL REFERENCES draft(id),
        revision INTEGER NOT NULL,
        outbound_key TEXT,
        unbuttoned INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (draft_id, revision))""",
    "CREATE INDEX draft_sheet_buttoned ON draft_sheet(draft_id) WHERE unbuttoned = 0",
    """CREATE TABLE document_version (
        draft_id INTEGER NOT NULL REFERENCES draft(id),
        version INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        evidence_id TEXT NOT NULL UNIQUE REFERENCES evidence(id),
        created_at REAL NOT NULL,
        PRIMARY KEY (draft_id, version),
        UNIQUE (draft_id, revision))""",
    "CREATE INDEX draft_open ON draft(state) WHERE state IN ('open', 'building')",
    "ALTER TABLE evidence ADD COLUMN derived_from TEXT",
    "ALTER TABLE host_message ADD COLUMN draft_id INTEGER REFERENCES draft(id)",
    "ALTER TABLE host_message ADD COLUMN draft_revision INTEGER",
    "ALTER TABLE host_message ADD COLUMN draft_checked INTEGER NOT NULL DEFAULT 0",
    "UPDATE host_message SET draft_checked = 1",
    "CREATE INDEX host_message_unchecked ON host_message(id) WHERE kind = 'attachment' AND draft_checked = 0",
)

# Stage 2c-1 reworked (the owner, 2026-10-27): no sheet to check; the PDF of the revision the model ordered goes out
# after its answer.
SCHEMA_V8 = ("ALTER TABLE draft ADD COLUMN ordered INTEGER",)


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    current = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0])
    if current < 1:
        with transaction(conn):
            for statement in SCHEMA_V1:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version(version) VALUES (1)")
        current = 1
    if current < 2:
        with transaction(conn):
            for statement in SCHEMA_V2:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version(version) VALUES (2)")
        current = 2
    if current < 3:
        with transaction(conn):
            for statement in SCHEMA_V3:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version(version) VALUES (3)")
        current = 3
    if current < 4:
        with transaction(conn):
            for statement in SCHEMA_V4:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version(version) VALUES (4)")
        current = 4
    if current < 5:
        with transaction(conn):
            for statement in SCHEMA_V5:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version(version) VALUES (5)")
        current = 5
    if current < 6:
        with transaction(conn):
            for statement in SCHEMA_V6:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version(version) VALUES (6)")
        current = 6
    if current < 7:
        with transaction(conn):
            for statement in SCHEMA_V7:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version(version) VALUES (7)")
        current = 7
    if current < 8:
        with transaction(conn):
            for statement in SCHEMA_V8:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version(version) VALUES (8)")
        current = 8
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


def owner_service_chat(conn: sqlite3.Connection) -> int | None:
    """The owner's private chat with the service bot, once bound."""
    row = conn.execute(
        "SELECT b.chat_id FROM service_binding b JOIN member m ON m.person_id = b.person_id WHERE m.role = 'owner'"
    ).fetchone()
    return None if row is None else int(row[0])
