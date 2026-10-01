import errno
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from helpers import BASE_CONFIG
from klepa_core import db, health
from klepa_core.documents import DocumentsFolder
from klepa_core.health import Health, Probe, check_documents, probe_documents
from klepa_core.journal import InboundJournal


def test_probe_writes_reads_lists_and_cleans_up(tmp_path):
    assert probe_documents(tmp_path) == Probe(True)
    assert list(tmp_path.iterdir()) == []


def test_probe_never_creates_a_missing_folder(tmp_path):
    missing = tmp_path / "gone"
    probe = probe_documents(missing)
    assert (probe.ok, probe.code) == (False, errno.ENOENT)
    assert not missing.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="file modes do not stop root")
def test_probe_reports_a_denied_folder(tmp_path):
    folder = tmp_path / "locked"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        probe = probe_documents(folder)
    finally:
        folder.chmod(0o700)
    assert (probe.ok, probe.error, probe.denied) == (False, "PermissionError", True)


def test_overlapping_probes_do_not_collide(tmp_path, monkeypatch):
    barrier = threading.Barrier(2, timeout=5)
    write_exclusive = health.write_exclusive

    def write_then_wait(directory, name, data):
        path = write_exclusive(directory, name, data)
        barrier.wait()  # both probes have written their files before either reads, lists or removes
        return path

    monkeypatch.setattr(health, "write_exclusive", write_then_wait)
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(probe_documents, [tmp_path, tmp_path])) == [Probe(True), Probe(True)]
    assert list(tmp_path.iterdir()) == []


async def test_a_folder_that_does_not_answer_is_unavailable(tmp_path, monkeypatch):
    release = threading.Event()

    def hanging(folder):
        release.wait()
        return Probe(True)

    monkeypatch.setattr(health, "probe_documents", hanging)
    try:
        probe = await check_documents(DocumentsFolder(tmp_path, timeout=0.2))
    finally:
        release.set()
    assert (probe.ok, probe.error, probe.code) == (False, "DocumentsTimeout", errno.ETIMEDOUT)


def utc(day: int, hour: int) -> str:
    return f"2026-10-{day:02d}T{hour:02d}:00:00.000+00:00"


@pytest.fixture
def health_check(make_config, tmp_path):
    cfg = make_config(text=BASE_CONFIG.replace('snapshot_at = "off"', 'snapshot_at = "03:30"'))
    conn = db.connect(cfg.core_db_path)
    db.migrate(conn)
    db.seed(conn, cfg)
    journal = InboundJournal(tmp_path / "inbound.db")
    now = [datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()]  # 07:00 UTC
    return Health(cfg, conn, journal, clock=lambda: now[0]), conn, journal, now


def add_snapshot(conn, generation, created_at, integrity="ok"):
    conn.execute(
        "INSERT INTO snapshot(generation, day, created_at, sha256, size, integrity) VALUES (?, ?, ?, ?, 10, ?)",
        (generation, created_at[:10], created_at, "a1b2c3d4" + "0" * 56, integrity),
    )


def test_fresh_install_is_all_good(health_check):
    checker, _, _, _ = health_check
    text, ok = checker.line(Probe(True))
    assert ok
    assert text.startswith("✅ All good")
    assert "last intake: none yet" in text
    assert "snapshot: none yet" in text


def test_line_reports_intake_snapshot_and_problems(health_check):
    checker, conn, journal, now = health_check
    journal.append_batch([{"update_id": 1}], "2026-10-05T06:30:00.000+00:00")
    add_snapshot(conn, 3, utc(5, 1))
    text, ok = checker.line(Probe(True))
    assert ok
    assert "last intake: 2026-10-05 08:30" in text
    assert "#3 a1b2c3d4 (ok)" in text
    text, ok = checker.line(Probe(False, "PermissionError", errno.EACCES))
    assert not ok
    assert text.startswith("⚠️ Needs attention")
    assert "unavailable (PermissionError)" in text
    now[0] += 3 * 24 * 3600  # the snapshot is three days old
    assert checker.line(Probe(True))[1] is False


def test_no_snapshot_two_days_after_the_first_start_needs_attention(health_check):
    checker, conn, _, _ = health_check
    conn.execute("INSERT INTO event_log(at, kind, data) VALUES (?, 'core_started', '{}')", (utc(4, 7),))
    assert checker.line(Probe(True))[1] is True  # one day after the first start: not yet
    conn.execute("DELETE FROM event_log")
    conn.execute("INSERT INTO event_log(at, kind, data) VALUES (?, 'core_started', '{}')", (utc(2, 7),))
    text, ok = checker.line(Probe(True))
    assert not ok
    assert "snapshot: none yet" in text


def test_a_broken_newest_snapshot_needs_attention(health_check):
    checker, conn, _, _ = health_check
    add_snapshot(conn, 1, utc(4, 1))
    add_snapshot(conn, 2, utc(5, 1), integrity="row 3 missing from index")
    text, ok = checker.line(Probe(True))
    assert not ok
    assert "#2 a1b2c3d4 (row 3 missing from index)" in text


def test_only_recent_unconfirmed_sends_and_copy_conflicts_count(health_check):
    checker, conn, _, _ = health_check
    add_snapshot(conn, 1, utc(5, 1))

    def unknown(key, updated_at):
        conn.execute(
            "INSERT INTO outbound(idempotency_key, origin, method, chat_id, payload, state, created_at, updated_at) "
            "VALUES (?, 'core', 'sendMessage', 1, '{}', 'UNKNOWN', ?, ?)",
            (key, updated_at, updated_at),
        )

    unknown("old", utc(2, 7))  # three days ago: already reported in earlier lines
    assert checker.line(Probe(True))[1] is True
    unknown("new", utc(5, 6))
    text, ok = checker.line(Probe(True))
    assert not ok
    assert "unconfirmed sends in 48 h: 1" in text
    conn.execute("DELETE FROM outbound")
    conn.execute(
        """INSERT INTO evidence(id, space_id, kind, received_at, channel, chat_id, message_id, update_id,
               authenticated_subject, ingest_key, state, copy_state)
           VALUES ('ev1', 'shared', 'file', 't', 'telegram', 1, 1, 1, 'owner', 'k', 'stored', 'failed')"""
    )
    text, ok = checker.line(Probe(True))
    assert not ok
    assert "copy conflicts: 1" in text


def test_line_carries_no_family_data(health_check):
    checker, conn, _, _ = health_check
    conn.execute(
        """INSERT INTO evidence(id, space_id, kind, original_name, received_at, channel, chat_id, message_id,
               update_id, authenticated_subject, ingest_key, state, caption)
           VALUES ('ev1', 'shared', 'file', 'Secret-diagnosis.pdf', 't', 'telegram', 1, 1, 1, 'owner', 'k',
                   'stored', 'just for me')"""
    )
    text, _ = checker.line(Probe(True))
    assert "Secret" not in text
    assert "just for me" not in text
    assert "waiting to copy: 1" in text


def test_a_probe_file_left_by_a_stopped_core_is_cleaned_up(tmp_path):
    stale = tmp_path / ".klepa-probe-0123456789abcdef"  # Core was stopped while its probe hung
    stale.write_bytes(b"left behind")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    keep = tmp_path / "Shared"
    keep.mkdir()
    assert probe_documents(tmp_path) == Probe(True)
    assert list(tmp_path.iterdir()) == [keep]


def test_the_line_names_the_hosts_state_and_only_running_is_good(health_check):
    checker, _, _, _ = health_check
    assert "host: not connected" in checker.line(Probe(True))[0]
    checker.host_state = lambda: "host_running"
    text, ok = checker.line(Probe(True))
    assert ok
    assert "✅ All good · host: running · " in text
    for state, words in (("host_stopped", "stopped"), ("host_paused", "paused"), ("host_starting", "starting")):
        checker.host_state = lambda state=state: state
        text, ok = checker.line(Probe(True))
        assert not ok
        assert f"host: {words}" in text
