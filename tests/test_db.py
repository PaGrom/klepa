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
    assert db.migrate(conn) == 1
    assert db.migrate(conn) == 1
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"member", "space", "evidence", "outbound", "event_log", "schema_version"} <= tables


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
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("INSERT INTO event_log(at, kind, data) VALUES ('t', 'k', '{}')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 0
