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
    assert db.migrate(conn) == 3
    assert db.migrate(conn) == 3
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"member", "space", "evidence", "outbound", "event_log", "schema_version", "service_binding"}
    assert expected | {"button_action", "alert_state", "snapshot", "job_run"} <= tables
    assert {"host_message", "host_update", "host_run", "host_notice", "host_setting"} <= tables


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
    assert db.migrate(conn) == 3
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
