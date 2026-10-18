"""Acceptance scenarios for stage 2b-1: attachments read in the sandbox, their texts kept page by page and indexed.

The readers are the stand-ins of fakereaders.py; they run in the real sandbox, which exists on macOS only."""

import dataclasses
import json
from contextlib import closing

import pytest

from fakereaders import processing_config, processing_section
from helpers import BASE_CONFIG, MEMBER, OWNER, evidence_rows, pdf, query, receipts, run_until
from klepa_core import app as app_module
from klepa_core import db
from klepa_core.processing.sandbox import sandbox_available

macos = pytest.mark.skipif(not sandbox_available(), reason="the readers run in sandbox-exec, which macOS has")


def done_jobs(cfg) -> int:
    return len(query(cfg, "SELECT 1 FROM job WHERE state='done'"))


def page_texts(cfg) -> list[tuple]:
    rows = query(
        cfg,
        "SELECT e.kind, t.page, t.text FROM evidence_text t JOIN evidence e ON e.id = t.evidence_id "
        "ORDER BY e.message_id, t.page",
    )
    return [tuple(row) for row in rows]


def alerts_logged(cfg) -> list[str]:
    rows = query(cfg, "SELECT data FROM event_log WHERE kind IN ('alert_unsent', 'alert_queued')")
    return [json.loads(row["data"])["alert"] for row in rows]


@macos
async def test_attachments_are_read_in_the_sandbox_and_their_texts_kept(fake_tg, make_config, tmp_path):
    section = processing_section(processing_config(tmp_path))
    cfg = make_config(api_root=fake_tg.url, text=BASE_CONFIG + section)
    fake_tg.add_voice(OWNER, b"renew the car insurance")
    fake_tg.add_document(OWNER, "policy.pdf", b"%PDF policy number 4512 PAGEBREAK valid until November")
    fake_tg.add_photo(MEMBER, b"receipt for the tyres")
    await run_until(cfg, lambda: done_jobs(cfg) == 3)
    assert page_texts(cfg) == [
        ("voice", 1, "transcript: renew the car insurance"),
        ("file", 1, "%PDF policy number 4512"),
        ("file", 2, "valid until November"),
        ("photo", 1, "photo text (eng): receipt for the tyres"),
    ]
    assert {row["state"] for row in evidence_rows(cfg)} == {"processed"}
    assert not list(cfg.jobs_dir.iterdir())  # no copy of an original is left behind


@macos
async def test_files_kept_before_processing_was_set_up_are_read_once_it_is(fake_tg, make_config, tmp_path):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_document(OWNER, "old.pdf", pdf(1))
    await run_until(cfg, lambda: len(receipts(fake_tg)) == 1)
    assert query(cfg, "SELECT 1 FROM job") == []
    cfg = make_config(api_root=fake_tg.url, text=BASE_CONFIG + processing_section(processing_config(tmp_path)))
    await run_until(cfg, lambda: done_jobs(cfg) == 1)
    assert page_texts(cfg)[0][:2] == ("file", 1)
    assert "test document 1" in page_texts(cfg)[0][2]


@macos
async def test_a_missing_program_keeps_its_kind_waiting_and_the_owner_is_told(fake_tg, make_config, tmp_path):
    processing = dataclasses.replace(processing_config(tmp_path), whisper=tmp_path / "missing" / "whisper-cli")
    cfg = make_config(api_root=fake_tg.url, text=BASE_CONFIG + processing_section(processing))
    fake_tg.add_voice(OWNER, b"a voice note")
    fake_tg.add_document(OWNER, "policy.pdf", pdf(2))
    await run_until(cfg, lambda: done_jobs(cfg) == 1)
    assert [tuple(row) for row in query(cfg, "SELECT kind, state, attempts FROM job ORDER BY id")] == [
        ("voice", "queued", 0),
        ("pdf", "done", 1),
    ]
    assert "processing_unavailable" in alerts_logged(cfg)


async def test_without_the_sandbox_files_are_kept_unread_and_the_owner_is_told(
    fake_tg, make_config, tmp_path, monkeypatch
):
    monkeypatch.setattr(app_module, "sandbox_available", lambda: False)
    cfg = make_config(api_root=fake_tg.url, text=BASE_CONFIG + processing_section(processing_config(tmp_path)))
    fake_tg.add_document(OWNER, "policy.pdf", pdf(3))
    await run_until(cfg, lambda: len(receipts(fake_tg)) == 1 and query(cfg, "SELECT 1 FROM job"))
    assert [row["state"] for row in query(cfg, "SELECT state FROM job")] == ["queued"]
    assert "processing_unavailable" in alerts_logged(cfg)


@macos
async def test_a_job_a_crash_left_running_is_read_at_the_next_start(fake_tg, make_config, tmp_path):
    """Core died while a reader ran: its job is queued again at start and read (finding M6)."""
    cfg = make_config(api_root=fake_tg.url, text=BASE_CONFIG + processing_section(processing_config(tmp_path)))
    fake_tg.add_document(OWNER, "policy.pdf", pdf(4))
    await run_until(cfg, lambda: done_jobs(cfg) == 1)
    with closing(db.connect(cfg.core_db_path)) as conn:  # as Core leaves it when it dies in the middle of the job
        conn.execute("DELETE FROM evidence_text")
        conn.execute("DELETE FROM evidence_fts WHERE page > 0")
        conn.execute("UPDATE job SET state='running', pages=NULL, ended_at=NULL")
        conn.execute("UPDATE evidence SET state='processing'")
    await run_until(cfg, lambda: done_jobs(cfg) == 1)
    assert [tuple(row) for row in query(cfg, "SELECT state, attempts FROM job")] == [("done", 2)]
    assert "test document 4" in page_texts(cfg)[0][2]
