import sqlite3

import pytest

from klepa_core import db


def test_connect_sets_durability_pragmas(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    assert db.migrate(conn) == 6
    assert db.migrate(conn) == 6
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"member", "space", "evidence", "outbound", "event_log", "schema_version", "service_binding"}
    assert expected | {"button_action", "alert_state", "snapshot", "job_run"} <= tables
    assert {"host_message", "host_update", "host_run", "host_notice", "host_setting"} <= tables
    assert {"host_tool_call", "job", "evidence_text", "evidence_fts", "host_message_item"} <= tables
    columns = {row[1] for row in conn.execute("PRAGMA table_info(host_message)")}
    assert {"cancelled_at", "interim_key", "interim_state"} <= columns


def test_seed_creates_members_and_spaces(tmp_path, make_config):
    cfg = make_config()
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    db.seed(conn, cfg)
    db.seed(conn, cfg)
    spaces = {row["space_id"]: row["folder"] for row in conn.execute("SELECT * FROM space")}
    assert spaces == {"shared": "Shared", "personal:owner": "Owner", "personal:member": "Member"}


def test_transaction_rolls_back_on_error(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)

    def insert_then_fail():
        with db.transaction(conn):
            conn.execute("INSERT INTO event_log(at, kind, data) VALUES ('t', 'k', '{}')")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        insert_then_fail()
    assert conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 0


def test_v1_database_is_upgraded_in_place(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    with db.transaction(conn):
        for statement in db.SCHEMA_V1:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version(version) VALUES (1)")
    conn.execute(
        "INSERT INTO outbound(idempotency_key, origin, method, chat_id, payload, state, created_at, updated_at) "
        "VALUES ('k', 'core', 'sendMessage', 1, '{}', 'PENDING', 't', 't')"
    )
    assert db.migrate(conn) == 6
    assert conn.execute("SELECT bot FROM outbound").fetchone()[0] == "family"


def test_snapshot_generations_are_never_reused(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    insert = "INSERT INTO snapshot(day, created_at) VALUES ('2026-10-05', 't')"
    first = conn.execute(insert).lastrowid
    conn.execute("DELETE FROM snapshot WHERE generation=?", (first,))
    assert conn.execute(insert).lastrowid == first + 1


def test_owner_service_chat(tmp_path, make_config):
    cfg = make_config()
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    db.seed(conn, cfg)
    assert db.owner_service_chat(conn) is None
    conn.execute("INSERT INTO service_binding(person_id, chat_id, bound_at) VALUES ('owner', 111111, 't')")
    assert db.owner_service_chat(conn) == 111111


def test_host_messages_take_kinds_that_later_stages_add(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    conn.execute(
        "INSERT INTO host_message(kind, chat_id, message_id, sender_id, date, created_at) "
        "VALUES ('attachment_text', 1, 1, 1, 1, 0)"
    )


def test_records_stored_before_stage_2b_are_found_by_name_and_caption(tmp_path):
    """The search index covers every record: those of earlier stages get theirs when the database is upgraded."""
    conn = db.connect(tmp_path / "core.db")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    with db.transaction(conn):
        for schema in (db.SCHEMA_V1, db.SCHEMA_V2, db.SCHEMA_V3, db.SCHEMA_V4):
            for statement in schema:
                conn.execute(statement)
        conn.execute("INSERT INTO schema_version(version) VALUES (4)")
    conn.execute("INSERT INTO member(person_id, telegram_id, name, role) VALUES ('owner', 1, 'Owner', 'owner')")
    conn.execute("INSERT INTO space(space_id, kind, owner_person_id, folder) VALUES ('shared', 'shared', NULL, 'S')")
    conn.execute(
        "INSERT INTO evidence(id, space_id, kind, original_name, disk_name, incoming_path, mime, size, sha256, "
        "received_at, channel, chat_id, message_id, update_id, authenticated_subject, ingest_key, state, caption) "
        "VALUES ('ev1', 'shared', 'file', 'Insurance_policy.pdf', 'd', 'p', 'application/pdf', 1, 'h', 't', "
        "'telegram', 1, 1, 1, 'owner', 'k1', 'stored', 'for the car of the \u0111ak')"
    )
    assert db.migrate(conn) == 6
    rows = conn.execute("SELECT evidence_id, page FROM evidence_fts WHERE evidence_fts MATCH 'insur* car'").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("ev1", 0)]
    # folded as the index folds what it is given later: d with stroke as "dj"
    rows = conn.execute("SELECT evidence_id FROM evidence_fts WHERE evidence_fts MATCH 'djak*'").fetchall()
    assert [row[0] for row in rows] == ["ev1"]


def test_a_job_takes_every_kind_a_reader_reads_and_keeps_a_note(tmp_path):
    """The note says what a limit left out of the text ("OCR read the first 50 of 70 scanned pages")."""
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    conn.execute("INSERT INTO member(person_id, telegram_id, name, role) VALUES ('owner', 1, 'Owner', 'owner')")
    conn.execute("INSERT INTO space(space_id, kind, owner_person_id, folder) VALUES ('shared', 'shared', NULL, 'S')")
    kinds = ("voice", "video", "pdf", "image", "document", "sheet", "text", "zip")
    for n in range(len(kinds)):
        conn.execute(
            "INSERT INTO evidence(id, space_id, kind, received_at, channel, chat_id, message_id, update_id, "
            "authenticated_subject, ingest_key, state) VALUES (?, 'shared', 'file', 't', 'telegram', 1, ?, 1, "
            "'owner', ?, 'stored')",
            (f"ev{n}", n, f"k{n}"),
        )
    for n, kind in enumerate(kinds[:-1]):
        conn.execute(
            "INSERT INTO job(evidence_id, kind, state, created_at, note) VALUES (?, ?, 'done', 0, 'cut')",
            (f"ev{n}", kind),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO job(evidence_id, kind, state, created_at) VALUES ('ev7', 'zip', 'queued', 0)")
