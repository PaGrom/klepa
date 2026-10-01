import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from klepa_core import db, snapshot
from klepa_core.snapshot import PARTIAL_PREFIX, SnapshotError, Snapshotter, dir_name, keep_generations, verify_snapshot

KEY = b"s" * 32


def noon(day: int, month: int = 10) -> float:
    return datetime(2026, month, day, 12, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()


@pytest.fixture
def setup(make_config):
    cfg = make_config()
    conn = db.connect(cfg.core_db_path)
    db.migrate(conn)
    db.seed(conn, cfg)
    now = [noon(5)]
    return cfg, conn, Snapshotter(cfg, KEY, clock=lambda: now[0]), now


def test_take_writes_a_signed_verifiable_snapshot_with_monotonic_generations(setup):
    _, _, snapshotter, _ = setup
    first = snapshotter.take()
    second = snapshotter.take()
    assert (first.generation, second.generation) == (1, 2)
    manifest = verify_snapshot(snapshotter.root / second.name, KEY)
    assert (manifest["generation"], manifest["day"], manifest["integrity"]) == (2, "2026-10-05", "ok")
    assert manifest["privacy_journal_head"] is None
    assert sorted(path.name for path in snapshotter.root.iterdir()) == [first.name, second.name]  # no partials
    copy = sqlite3.connect(snapshotter.root / second.name / "core.db")
    assert copy.execute("SELECT MAX(generation) FROM snapshot").fetchone()[0] == 2  # it records itself
    copy.close()


def test_tampering_is_detected(setup):
    _, _, snapshotter, _ = setup
    info = snapshotter.take()
    directory = snapshotter.root / info.name
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["generation"] = 99
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SnapshotError, match="signature"):
        verify_snapshot(directory, KEY)
    with pytest.raises(SnapshotError, match="signature"):
        verify_snapshot(snapshotter.root / snapshotter.take().name, b"x" * 32)


def test_a_failed_take_leaves_nothing_and_its_number_is_not_reused(setup, monkeypatch):
    _, conn, snapshotter, _ = setup

    def broken(path):
        raise RuntimeError("power loss")

    monkeypatch.setattr(snapshot, "check_integrity", broken)
    with pytest.raises(RuntimeError):
        snapshotter.take()
    assert list(snapshotter.root.iterdir()) == []
    assert conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 0
    monkeypatch.undo()
    assert snapshotter.take().generation == 2


def test_a_take_killed_half_way_is_swept_at_start(setup):
    _, conn, snapshotter, _ = setup
    # kill -9 while VACUUM INTO was writing: a row without a checksum and a half-written folder.
    orphan = conn.execute("INSERT INTO snapshot(day, created_at) VALUES ('2026-10-05', 't')").lastrowid
    snapshotter.root.mkdir(mode=0o700)
    half = snapshotter.root / (PARTIAL_PREFIX + dir_name("2026-10-05", orphan))
    half.mkdir()
    (half / "core.db").write_bytes(b"SQLite format 3\x00 and then nothing")
    # kill -9 after the rename but before the row was updated: a complete, signed snapshot.
    done = snapshotter.take()
    conn.execute("UPDATE snapshot SET sha256=NULL, size=NULL, integrity=NULL WHERE generation=?", (done.generation,))
    assert snapshotter.sweep() == 1
    assert [path.name for path in snapshotter.root.iterdir()] == [done.name]
    rows = conn.execute("SELECT generation, sha256 FROM snapshot").fetchall()
    assert [(row[0], row[1]) for row in rows] == [(done.generation, done.sha256)]
    assert snapshotter.take().generation == done.generation + 1


def test_copy_waits_for_the_documents_folder_and_the_copy_verifies(setup):
    cfg, conn, snapshotter, _ = setup
    info = snapshotter.take()
    cfg.documents_dir.rmdir()  # e.g. its volume is not mounted
    with pytest.raises(FileNotFoundError):
        snapshotter.copy_pending()
    assert not cfg.documents_dir.exists()  # never recreated
    assert conn.execute("SELECT copied_at FROM snapshot").fetchone()[0] is None
    cfg.documents_dir.mkdir()
    assert snapshotter.copy_pending() == 1
    verify_snapshot(snapshotter.documents_root / info.name, KEY)
    assert snapshotter.copy_pending() == 0


def test_an_interrupted_copy_never_shows_an_incomplete_snapshot(setup, monkeypatch):
    _, _, snapshotter, _ = setup
    info = snapshotter.take()
    real = snapshot.write_exclusive

    def fail_on_signature(directory, name, data):
        if name == "manifest.sig":
            raise OSError(28, "No space left on device")
        return real(directory, name, data)

    monkeypatch.setattr(snapshot, "write_exclusive", fail_on_signature)
    with pytest.raises(OSError, match="No space left"):
        snapshotter.copy_pending()
    assert not (snapshotter.documents_root / info.name).exists()  # only a .partial- folder
    monkeypatch.undo()
    assert snapshotter.copy_pending() == 1
    assert sorted(path.name for path in snapshotter.documents_root.iterdir()) == [info.name]


def test_a_conflicting_copy_holds_back_only_its_own_snapshot(setup):
    _, conn, snapshotter, _ = setup
    first = snapshotter.take()
    second = snapshotter.take()
    (snapshotter.documents_root / first.name).mkdir(parents=True)  # something else under our name
    with pytest.raises(SnapshotError):
        snapshotter.copy_pending()
    copied = dict(conn.execute("SELECT generation, copied_at IS NOT NULL FROM snapshot").fetchall())
    assert copied == {first.generation: 0, second.generation: 1}


def test_keep_generations():
    rows = []
    generation = 0
    for month, days in ((8, 31), (9, 30), (10, 5)):
        for day in range(1, days + 1):
            generation += 1
            rows.append((generation, f"2026-{month:02d}-{day:02d}"))
    keep = keep_generations(rows)
    kept_days = sorted(day for generation, day in rows if generation in keep)
    assert kept_days[-14:] == [f"2026-09-{d:02d}" for d in range(22, 31)] + [f"2026-10-{d:02d}" for d in range(1, 6)]
    assert {"2026-08-01", "2026-09-01", "2026-10-01"} <= set(kept_days)
    assert len(keep) == 16


def test_prune_removes_only_old_snapshot_folders(setup):
    _, _, snapshotter, now = setup
    names = []
    for day in range(1, 21):
        now[0] = noon(day, month=9)
        names.append(snapshotter.take().name)
    snapshotter.copy_pending()
    (snapshotter.root / "notes").mkdir()  # not a snapshot folder: never touched
    removed = snapshotter.prune()
    assert len(removed) == 5  # 2026-09-02 .. 2026-09-06; 09-01 stays as the month's first
    for name in names:
        exists = (snapshotter.root / name).exists()
        assert exists == (name.startswith("2026-09-01") or name >= "2026-09-07")
        assert (snapshotter.documents_root / name).exists() == exists
    assert (snapshotter.root / "notes").exists()
    assert dir_name("2026-09-01", 1) == "2026-09-01-g000001"


def test_retention_counts_good_snapshots_only(setup):
    _, conn, snapshotter, now = setup
    days = [f"2026-09-{d:02d}" for d in range(1, 31)] + [f"2026-10-{d:02d}" for d in range(1, 7)]
    for generation, day in enumerate(days, start=1):
        integrity = "ok" if day <= "2026-09-20" else "row 3 missing from index"  # the live database broke
        conn.execute(
            "INSERT INTO snapshot(generation, day, created_at, sha256, size, integrity) VALUES (?, ?, 't', 'x', 1, ?)",
            (generation, day, integrity),
        )
        (snapshotter.root / dir_name(day, generation)).mkdir(parents=True)
    now[0] = noon(6)
    snapshotter.prune()
    kept = sorted(path.name[:10] for path in snapshotter.root.iterdir())
    # The broken days displace no good snapshot, and they stay two weeks for diagnosis: from 09-22 on.
    assert [day for day in kept if day <= "2026-09-20"] == ["2026-09-01"] + [f"2026-09-{d:02d}" for d in range(7, 21)]
    assert [day for day in kept if day > "2026-09-20"] == days[21:]


def test_a_held_answer_of_the_host_stays_out_of_the_snapshot(setup):
    _, conn, snapshotter, _ = setup
    conn.execute(
        "INSERT INTO outbound(idempotency_key, origin, method, chat_id, payload, state, created_at, updated_at) "
        "VALUES ('host:1', 'host', 'sendMessage', 1, '{\"text\": \"HELD-ANSWER\"}', 'PENDING', 't', 't')"
    )
    conn.execute(
        "INSERT INTO outbound(idempotency_key, origin, method, chat_id, payload, state, created_at, updated_at) "
        "VALUES ('receipt:1', 'core', 'sendMessage', 1, '{\"text\": \"CORE-RECEIPT\"}', 'PENDING', 't', 't')"
    )
    info = snapshotter.take()
    data = (snapshotter.root / info.name / "core.db").read_bytes()
    assert b"HELD-ANSWER" not in data
    assert b"CORE-RECEIPT" in data
    verify_snapshot(snapshotter.root / info.name, KEY)
    assert conn.execute("SELECT payload FROM outbound WHERE idempotency_key='host:1'").fetchone()[0] != "{}"
