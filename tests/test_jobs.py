"""Processing jobs: one per readable original, read in order, their texts stored and indexed, and a job that a crash
interrupted read again, a few times at most."""

import asyncio
import json
import os
import stat
import sys
import threading

import pytest

from fakereaders import PlainRunner, processing_config
from klepa_core.events import EventLog
from klepa_core.processing import jobs as jobs_module
from klepa_core.processing.jobs import KINDS, MAX_ATTEMPTS, Jobs, Worker, job_kind
from klepa_core.processing.readers import Readers
from klepa_core.processing.sandbox import ProcessingStopped


@pytest.fixture
def setup(core_db, tmp_path):
    cfg, conn, _ = core_db
    processing = processing_config(tmp_path)
    events = EventLog(conn)
    jobs = Jobs(conn, events)
    worker = Worker(conn, jobs, Readers(processing, PlainRunner()), cfg.incoming_dir, tmp_path / "jobs", events)
    return cfg, conn, jobs, worker


def store(cfg, conn, record_id, kind, name, content, *, mime=None, state="stored", space="shared"):
    path = cfg.incoming_dir / "2026" / "10" / f"{record_id}-{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    conn.execute(
        "INSERT INTO evidence(id, space_id, kind, original_name, disk_name, incoming_path, mime, size, sha256, "
        "received_at, channel, chat_id, message_id, update_id, authenticated_subject, ingest_key, state, caption) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'h', '2026-10-07T10:00:00Z', 'telegram', 1, ?, 1, 'owner', ?, ?, NULL)",
        (record_id, space, kind, name, name, f"2026/10/{record_id}-{name}", mime, len(content),
         abs(hash(record_id)) % 100_000, f"k:{record_id}", state),
    )  # fmt: skip


def texts(conn, record_id):
    rows = conn.execute("SELECT page, text FROM evidence_text WHERE evidence_id=? ORDER BY page", (record_id,))
    return [(row["page"], row["text"]) for row in rows]


@pytest.mark.parametrize(
    ("kind", "mime", "name", "expected"),
    [
        ("voice", "audio/ogg", None, "voice"),
        ("audio", "audio/mpeg", "talk.mp3", "voice"),
        ("file", "audio/ogg", "recording.ogg", "voice"),  # a voice note sent as a file
        ("file", "application/pdf", "a.pdf", "pdf"),
        ("file", None, "scan.PDF", "pdf"),
        ("photo", "image/jpeg", None, "image"),
        ("file", "image/png", "receipt.png", "image"),
        ("file", "text/plain", "notes.txt", "text"),
        ("file", None, "list.md", "text"),
        ("file", "image/heic", "photo.heic", "image"),  # ffmpeg turns it into a PNG for tesseract
        ("file", None, "IMG_0042.HEIF", "image"),
        ("file", "application/zip", "a.zip", None),
        ("video", "video/mp4", "clip.mp4", "video"),  # a video, a video note or an animation: its sound
        ("file", "video/quicktime", "IMG_0042.MOV", "video"),
        ("file", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "lease.docx", "document"),
        ("file", "application/msword", "old.doc", "document"),
        ("file", None, "letter.rtf", "document"),
        ("file", "application/vnd.oasis.opendocument.text", "minutes.odt", "document"),
        ("file", "text/html", "page.html", "document"),
        ("file", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "bills.xlsx", "sheet"),
        ("file", None, "budget.ods", "sheet"),
        ("file", None, "macros.xlsm", "sheet"),
        ("file", "application/vnd.ms-excel", "old.xls", None),  # the old binary format: no reader
    ],
)
def test_each_kind_of_original_has_its_reader_or_none(kind, mime, name, expected):
    assert job_kind(kind, mime, name) == expected


def test_a_voice_message_is_transcribed_and_its_text_indexed(setup):
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-voice", "voice", None, b"renew the car insurance", mime="audio/ogg")
    assert jobs.add("ev-voice", "voice")
    assert not jobs.add("ev-voice", "voice")  # one job per original
    assert asyncio.run(worker.run_once()) is True
    assert texts(conn, "ev-voice") == [(1, "transcript: renew the car insurance")]
    job = conn.execute("SELECT state, pages FROM job WHERE evidence_id='ev-voice'").fetchone()
    assert (job["state"], job["pages"]) == ("done", 1)
    assert conn.execute("SELECT state FROM evidence WHERE id='ev-voice'").fetchone()[0] == "processed"
    found = conn.execute("SELECT evidence_id, page FROM evidence_fts WHERE evidence_fts MATCH 'insur*'").fetchall()
    assert [tuple(row) for row in found] == [("ev-voice", 1)]
    assert not list((worker.jobs_dir).iterdir())  # the job's folder is gone with its copy
    assert asyncio.run(worker.run_once()) is False  # nothing left


def test_a_document_keeps_its_pages(setup):
    cfg, conn, jobs, worker = setup
    first = "The first page of the policy names the house, the car and the garden, and the year the cover ends."
    second = "The second page of the policy lists the deductible, the claims and the thirty days to make one."
    store(cfg, conn, "ev-pdf", "file", "policy.pdf", f"{first} PAGEBREAK {second}".encode(), mime="application/pdf")
    jobs.add("ev-pdf", "pdf")
    asyncio.run(worker.run_once())
    assert texts(conn, "ev-pdf") == [(1, first), (2, second)]


def test_a_reader_that_fails_marks_the_record_and_says_only_why(setup):
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-bad", "voice", None, b"BROKEN private words", mime="audio/ogg")
    jobs.add("ev-bad", "voice")
    asyncio.run(worker.run_once())
    job = conn.execute("SELECT state, error FROM job WHERE evidence_id='ev-bad'").fetchone()
    assert (job["state"], job["error"]) == ("failed", "whisper-cli exited with 3")
    assert conn.execute("SELECT state FROM evidence WHERE id='ev-bad'").fetchone()[0] == "failed"
    assert texts(conn, "ev-bad") == []
    logged = [json.loads(row[0]) for row in conn.execute("SELECT data FROM event_log WHERE kind='job_failed'")]
    assert [(event["kind"], event["error"]) for event in logged] == [("voice", "whisper-cli exited with 3")]
    assert not any("private" in json.dumps(event) for event in logged)


def test_an_original_missing_from_its_folder_fails_its_job_alone(setup):
    """The error names no path: a file's name came from the sender's device and may say what is in it."""
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-gone", "file", "Diagnosis.pdf", b"text", mime="application/pdf")
    (cfg.incoming_dir / "2026" / "10" / "ev-gone-Diagnosis.pdf").unlink()
    store(cfg, conn, "ev-next", "file", "n.txt", b"the next note", mime="text/plain")
    jobs.add("ev-gone", "pdf")
    jobs.add("ev-next", "text")
    asyncio.run(worker.run_once())
    asyncio.run(worker.run_once())
    rows = conn.execute("SELECT evidence_id, state, error FROM job ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [("ev-gone", "failed", "the original is missing"), ("ev-next", "done", None)]
    assert "Diagnosis" not in json.dumps([tuple(row) for row in conn.execute("SELECT data FROM event_log")])


def test_a_job_is_logged_by_its_kind_pages_and_time_never_its_text(setup):
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-log", "file", "n.txt", b"the secret words", mime="text/plain")
    jobs.add("ev-log", "text")
    asyncio.run(worker.run_once())
    (event,) = [json.loads(row[0]) for row in conn.execute("SELECT data FROM event_log WHERE kind='job_done'")]
    assert set(event) == {"job", "evidence_id", "kind", "pages", "seconds", "extractor"}
    assert (event["kind"], event["pages"], event["extractor"]) == ("text", 1, "text")


def test_copies_a_crash_left_in_the_jobs_folder_are_removed_at_start(setup):
    worker = setup[3]
    leftover = worker.jobs_dir / "7"
    leftover.mkdir(parents=True)
    (leftover / "input.pdf").write_bytes(b"a copy of someone's original")
    worker.sweep()
    assert worker.jobs_dir.is_dir()
    assert not list(worker.jobs_dir.iterdir())


def test_what_a_reader_made_hard_to_remove_is_removed(tmp_path):
    """A reader may take the owner's rights from a folder in its job's folder, or mark a file unchangeable (finding
    I4): the removal gives them back first."""
    folder = tmp_path / "jobs" / "7"
    locked = folder / "locked"
    locked.mkdir(parents=True)
    (locked / "page.png").write_bytes(b"page")
    locked.chmod(0)
    readonly = folder / "readonly"
    readonly.mkdir()
    (readonly / "out.txt").write_text("text")
    readonly.chmod(stat.S_IRUSR | stat.S_IXUSR)
    if sys.platform == "darwin":
        frozen = folder / "frozen.txt"
        frozen.write_text("text")
        os.chflags(frozen, stat.UF_IMMUTABLE)
    folder.chmod(0)
    if sys.platform == "darwin":
        os.chflags(folder, stat.UF_IMMUTABLE)
    jobs_module.remove(folder)
    assert not folder.exists()
    assert (tmp_path / "jobs").is_dir()


def test_a_jobs_folder_that_cannot_be_removed_is_moved_aside_and_core_starts(setup, monkeypatch):
    _, conn, _, worker = setup
    (worker.jobs_dir / "7").mkdir(parents=True)

    def refuse(path):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(jobs_module, "remove", refuse)
    worker.sweep()
    assert worker.jobs_dir.is_dir()
    assert not list(worker.jobs_dir.iterdir())
    (aside,) = worker.jobs_dir.parent.glob("jobs.stale-*")
    assert (aside / "7").is_dir()
    (event,) = [json.loads(row[0]) for row in conn.execute("SELECT data FROM event_log WHERE kind='jobs_folder_left'")]
    assert event == {"folder": aside.name}


def test_a_job_folder_that_cannot_be_removed_does_not_stop_the_queue(setup, monkeypatch):
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-1", "file", "n1.txt", b"a note", mime="text/plain")
    jobs.add("ev-1", "text")
    threads = []

    def refuse(path):
        threads.append(threading.current_thread())
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(jobs_module, "remove", refuse)
    assert asyncio.run(worker.run_once()) is True
    assert conn.execute("SELECT state FROM job").fetchone()[0] == "done"
    assert threads
    assert threading.main_thread() not in threads  # gigabytes are not removed on the event loop
    (event,) = [json.loads(row[0]) for row in conn.execute("SELECT data FROM event_log WHERE kind='job_folder_left'")]
    assert event == {"job": 1, "error": "PermissionError"}


def test_jobs_are_read_in_the_order_they_came(setup):
    cfg, conn, jobs, worker = setup
    for n in (1, 2, 3):
        store(cfg, conn, f"ev-{n}", "file", f"n{n}.txt", f"note number {n}".encode(), mime="text/plain")
        jobs.add(f"ev-{n}", "text")
    asyncio.run(worker.run_once())
    done = [row[0] for row in conn.execute("SELECT evidence_id FROM job WHERE state='done'")]
    assert done == ["ev-1"]


def test_a_job_a_crash_interrupted_is_read_again_a_few_times_at_most(setup):
    cfg, conn, jobs, _ = setup
    store(cfg, conn, "ev-crash", "file", "n.txt", b"some note", mime="text/plain")
    jobs.add("ev-crash", "text")
    state = "SELECT state FROM evidence WHERE id='ev-crash'"
    for _ in range(MAX_ATTEMPTS):
        assert jobs.claim() is not None  # taken, then Core died before the end
        assert conn.execute(state).fetchone()[0] == "processing"
        jobs.recover()  # the next Core's start
    assert jobs.claim() is None
    job = conn.execute("SELECT state, error, attempts FROM job WHERE evidence_id='ev-crash'").fetchone()
    assert (job["state"], job["error"], job["attempts"]) == ("failed", "interrupted", MAX_ATTEMPTS)
    assert conn.execute(state).fetchone()[0] == "failed"


def test_a_job_that_was_running_is_queued_again_at_start(setup):
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-again", "file", "n.txt", b"some note", mime="text/plain")
    jobs.add("ev-again", "text")
    jobs.claim()
    assert jobs.recover() == 1
    assert conn.execute("SELECT state FROM evidence WHERE id='ev-again'").fetchone()[0] == "stored"
    asyncio.run(worker.run_once())
    job = conn.execute("SELECT state, attempts FROM job WHERE evidence_id='ev-again'").fetchone()
    assert (job["state"], job["attempts"]) == ("done", 2)


def test_originals_stored_before_processing_was_set_up_are_read_too(setup):
    cfg, conn, jobs, _ = setup
    store(cfg, conn, "ev-old", "file", "old.pdf", b"an old policy document with text", mime="application/pdf")
    store(cfg, conn, "ev-zip", "file", "a.zip", b"zip", mime="application/zip")
    store(cfg, conn, "ev-big", "file", "big.pdf", b"", mime="application/pdf", state="too_large")
    assert jobs.backfill() == 1
    assert [row[0] for row in conn.execute("SELECT evidence_id FROM job")] == ["ev-old"]
    events = [json.loads(row[0]) for row in conn.execute("SELECT data FROM event_log WHERE kind='jobs_backfilled'")]
    assert events == [{"jobs": 1}]


def test_a_record_no_longer_kept_is_not_read(setup):
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-del", "file", "n.txt", b"words of a deleted note", mime="text/plain", state="deleted")
    jobs.add("ev-del", "text")
    asyncio.run(worker.run_once())
    job = conn.execute("SELECT state, error FROM job WHERE evidence_id='ev-del'").fetchone()
    assert (job["state"], job["error"]) == ("failed", "the record is not kept")
    assert conn.execute("SELECT state FROM evidence WHERE id='ev-del'").fetchone()[0] == "deleted"
    assert texts(conn, "ev-del") == []


def test_an_unexpected_error_fails_the_job_by_its_type_and_the_queue_goes_on(setup, monkeypatch):
    cfg, conn, jobs, worker = setup
    for n in (1, 2):
        store(cfg, conn, f"ev-{n}", "file", f"n{n}.txt", b"a note", mime="text/plain")
        jobs.add(f"ev-{n}", "text")
    calls = []

    async def odd_read(kind, source, job):
        calls.append(source)
        if len(calls) == 1:
            raise ValueError("a message that may quote the file")
        return await original(kind, source, job)

    original = worker.readers.read
    monkeypatch.setattr(worker.readers, "read", odd_read)
    asyncio.run(worker.run_once())
    asyncio.run(worker.run_once())
    rows = conn.execute("SELECT evidence_id, state, error FROM job ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [("ev-1", "failed", "ValueError"), ("ev-2", "done", None)]


def test_a_kind_that_cannot_be_read_now_waits_and_the_others_go_on(setup, tmp_path):
    """A missing program or model must not use up the jobs that need it: they wait for Core's next start."""
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-voice", "voice", None, b"a voice note", mime="audio/ogg")
    store(cfg, conn, "ev-note", "file", "n.txt", b"a note", mime="text/plain")
    jobs.add("ev-voice", "voice")
    jobs.add("ev-note", "text")
    without_voice = Worker(
        conn, jobs, worker.readers, cfg.incoming_dir, worker.jobs_dir, worker.events, kinds=KINDS - {"voice"}
    )
    assert asyncio.run(without_voice.run_once()) is True
    assert asyncio.run(without_voice.run_once()) is False
    rows = conn.execute("SELECT evidence_id, state, attempts FROM job ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [("ev-voice", "queued", 0), ("ev-note", "done", 1)]


def test_a_note_on_what_was_left_out_is_kept_with_the_job(setup):
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-gif", "video", "funny.mp4", b"MUTE animation", mime="video/mp4")
    jobs.add("ev-gif", "video")
    asyncio.run(worker.run_once())
    job = conn.execute("SELECT state, pages, note FROM job WHERE evidence_id='ev-gif'").fetchone()
    assert tuple(job) == ("done", 0, "the video has no sound")
    assert conn.execute("SELECT state FROM evidence WHERE id='ev-gif'").fetchone()[0] == "processed"


def test_a_reader_the_watchdog_stopped_fails_its_job_with_the_limit(setup, monkeypatch):
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-flood", "file", "a.pdf", b"text", mime="application/pdf")
    jobs.add("ev-flood", "pdf")

    async def flood(kind, source, job):
        raise ProcessingStopped("ocrmypdf wrote more than 2048 MB or 10000 files")

    monkeypatch.setattr(worker.readers, "read", flood)
    asyncio.run(worker.run_once())
    job = conn.execute("SELECT state, error FROM job").fetchone()
    assert tuple(job) == ("failed", "ocrmypdf wrote more than 2048 MB or 10000 files")


def test_a_program_gone_since_start_puts_the_job_back_and_its_kind_waits(setup):
    """Homebrew upgrades a program under the same path: the original did nothing wrong (finding M8). Its kind waits
    for Core's next start, the owner is told, and the other kinds go on."""
    cfg, conn, jobs, worker = setup
    missing = []
    worker.on_missing = lambda kind, program: missing.append((kind, program))
    store(cfg, conn, "ev-voice", "voice", None, b"a voice note", mime="audio/ogg")
    store(cfg, conn, "ev-note", "file", "n.txt", b"a note", mime="text/plain")
    jobs.add("ev-voice", "voice")
    jobs.add("ev-note", "text")
    worker.readers.processing.whisper.unlink()
    assert asyncio.run(worker.run_once()) is True
    rows = conn.execute("SELECT evidence_id, state, attempts FROM job ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [("ev-voice", "queued", 0), ("ev-note", "queued", 0)]
    assert conn.execute("SELECT state FROM evidence WHERE id='ev-voice'").fetchone()[0] == "stored"
    assert "voice" not in worker.kinds
    assert missing == [("voice", "whisper-cli")]
    (event,) = [json.loads(row[0]) for row in conn.execute("SELECT data FROM event_log WHERE kind='reader_missing'")]
    assert event == {"job": 1, "kind": "voice", "program": "whisper-cli"}
    assert asyncio.run(worker.run_once()) is True  # the text note
    assert asyncio.run(worker.run_once()) is False
    assert conn.execute("SELECT state FROM job WHERE evidence_id='ev-note'").fetchone()[0] == "done"


def test_core_stopping_in_the_middle_of_a_job_puts_it_back_without_using_an_attempt(setup):
    cfg, conn, jobs, worker = setup
    store(cfg, conn, "ev-slow", "voice", None, b"SLOW words", mime="audio/ogg")
    jobs.add("ev-slow", "voice")

    async def stop_while_reading():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if conn.execute("SELECT state FROM job").fetchone()[0] == "running":
                break
        stop.set()
        await asyncio.wait_for(task, 10)

    asyncio.run(stop_while_reading())
    job = conn.execute("SELECT state, attempts FROM job WHERE evidence_id='ev-slow'").fetchone()
    assert (job["state"], job["attempts"]) == ("queued", 0)
    assert conn.execute("SELECT state FROM evidence WHERE id='ev-slow'").fetchone()[0] == "stored"
    assert not list(worker.jobs_dir.iterdir())
