"""Acceptance scenarios for stage 1b (docs/architecture.md: Testing)."""

import asyncio
import functools
import json
import os
import threading
import time

import pytest

from helpers import (
    BASE_CONFIG,
    MEMBER,
    OWNER,
    SERVICE_TOKEN,
    STRANGER,
    copied_count,
    evidence_rows,
    pdf,
    query,
    receipts,
    run_until,
    sent_texts,
    with_service_bot,
)
from klepa_core import app, db
from klepa_core.app import init_layout, run_service
from klepa_core.documents import DocumentsFolder
from klepa_core.evidence import EvidenceStore
from klepa_core.gatekeeper.service import Gatekeeper
from klepa_core.keys import load_or_create_key
from klepa_core.servicebot import ServiceBot
from klepa_core.snapshot import Snapshotter, verify_snapshot


async def test_album_caption_arriving_after_the_first_receipt_still_keeps_the_album_private(fake_tg, make_config):
    hour = BASE_CONFIG.replace("album_quiet_seconds = 0.3", "album_quiet_seconds = 3600")
    cfg = make_config(api_root=fake_tg.url, text=hour)
    fake_tg.add_photo(OWNER, pdf(0) * 3, media_group_id="late")
    fake_tg.add_photo(OWNER, pdf(1) * 3, media_group_id="late")
    state: dict[str, float] = {}

    def progress():
        if "receipt" not in state and receipts(fake_tg) == ["📄 got 2 files"]:
            state["receipt"] = time.monotonic()
        if "receipt" in state and "late" not in state and time.monotonic() - state["receipt"] > 0.5:
            fake_tg.add_photo(OWNER, pdf(2) * 3, media_group_id="late", caption="just for me")
            state["late"] = time.monotonic()
        return len(receipts(fake_tg)) == 2  # the late photo gets a receipt of its own

    await run_until(cfg, progress)
    assert {row["space_id"] for row in evidence_rows(cfg)} == {"personal:owner"}
    assert copied_count(cfg) == 0
    # Once the album has been quiet long enough, all of it goes to the personal folder, none to Shared.
    await run_until(make_config(api_root=fake_tg.url), lambda: copied_count(cfg) == 3)
    assert not (cfg.documents_dir / "Shared").exists()


class Crash(Exception):
    """Stands in for kill -9."""


def service_cfg(make_config, install, fake_tg, service_tg, **schedule):
    text = with_service_bot(BASE_CONFIG, install["service_token_file"], service_tg.url, **schedule)
    cfg = make_config(api_root=fake_tg.url, text=text)
    init_layout(cfg)
    conn = db.connect(cfg.core_db_path)
    conn.execute("INSERT INTO service_binding(person_id, chat_id, bound_at) VALUES ('owner', ?, 't')", (OWNER,))
    conn.close()
    return cfg


def documents_alerts(service_tg):
    return [text for text in sent_texts(service_tg) if text.startswith("⚠️ The documents folder is not available")]


async def test_snapshot_and_daily_line_come_once_a_day_even_across_a_restart(fake_tg, service_tg, make_config, install):
    cfg = service_cfg(make_config, install, fake_tg, service_tg, snapshot_at="00:00", daily_line_at="00:00")
    await run_until(cfg, lambda: any(t.startswith("✅ All good") for t in sent_texts(service_tg)))
    line = next(t for t in sent_texts(service_tg) if t.startswith("✅ All good"))
    assert "snapshot: #1 " in line
    assert "documents folder: ok" in line
    key = load_or_create_key(cfg.signing_key_path)
    local = next((cfg.data_dir / "snapshots").iterdir())
    assert verify_snapshot(local, key)["generation"] == 1
    assert verify_snapshot(cfg.documents_dir / "_klepa" / "snapshots" / local.name, key)["generation"] == 1
    # Core starts again the same day. The scheduler looks at its jobs right at start, long before the owner's
    # /status is answered, so a second run of either job would already be logged by then.
    service_tg.add_text(OWNER, "/status")
    await run_until(cfg, lambda: sum(t.startswith("✅ All good") for t in sent_texts(service_tg)) == 2)
    rows = query(cfg, "SELECT data FROM event_log WHERE kind='job_started'")
    assert sorted(json.loads(row["data"])["job"] for row in rows) == ["daily_line", "snapshot"]
    assert len(list((cfg.data_dir / "snapshots").iterdir())) == 1


async def test_documents_folder_missing_at_start_alerts_once_and_copies_catch_up(
    fake_tg, service_tg, make_config, install
):
    cfg = service_cfg(make_config, install, fake_tg, service_tg)
    cfg.documents_dir.rmdir()  # its volume is not mounted yet
    fake_tg.add_document(OWNER, "a.pdf", pdf(1))

    def progress():
        if not cfg.documents_dir.exists() and receipts(fake_tg) and documents_alerts(service_tg):
            cfg.documents_dir.mkdir()  # the volume mounts while Core runs
        return copied_count(cfg) == 1

    await run_until(cfg, progress)
    assert len(documents_alerts(service_tg)) == 1


async def test_documents_folder_gone_mid_run_alerts_once_and_intake_goes_on(fake_tg, service_tg, make_config, install):
    cfg = service_cfg(make_config, install, fake_tg, service_tg)
    away = install["tmp"] / "unmounted"
    fake_tg.add_document(OWNER, "a.pdf", pdf(1))
    state: set[str] = set()

    def progress():
        if "gone" not in state and copied_count(cfg) == 1:
            os.rename(cfg.documents_dir, away)  # the volume goes away while Core runs
            state.add("gone")
            fake_tg.add_document(OWNER, "b.pdf", pdf(2))
            service_tg.add_text(OWNER, "/status")
        attention = any(t.startswith("⚠️ Needs attention") for t in sent_texts(service_tg))
        noticed = len(receipts(fake_tg)) == 2 and attention and documents_alerts(service_tg)
        if "gone" in state and "back" not in state and noticed:
            os.rename(away, cfg.documents_dir)  # and comes back
            state.add("back")
        return "back" in state and copied_count(cfg) == 2

    await run_until(cfg, progress)
    assert len(documents_alerts(service_tg)) == 1  # one alert, not one per failed copy
    status = next(t for t in sent_texts(service_tg) if t.startswith("⚠️ Needs attention"))
    assert "unavailable (FileNotFoundError)" in status


async def test_revoked_family_bot_token_alerts_the_owner(fake_tg, service_tg, make_config, install):
    cfg = service_cfg(make_config, install, fake_tg, service_tg)
    fake_tg.token_valid = False
    await run_until(cfg, lambda: any(t.startswith("⚠️ Telegram refuses") for t in sent_texts(service_tg)))
    assert "Unauthorized" in next(t for t in sent_texts(service_tg) if t.startswith("⚠️ Telegram refuses"))


async def test_restart_after_a_crash_is_reported(fake_tg, service_tg, make_config, install, monkeypatch):
    cfg = service_cfg(make_config, install, fake_tg, service_tg)

    async def crash(self, stop):
        raise Crash("kill -9")

    monkeypatch.setattr(Gatekeeper, "copy_forever", crash)
    with pytest.raises(ExceptionGroup):
        await run_service(cfg, asyncio.Event(), copy_interval=0.05)
    monkeypatch.undo()
    await run_until(cfg, lambda: any(t.startswith("⚠️ Core restarted") for t in sent_texts(service_tg)))


async def test_service_bot_answers_only_the_owner(fake_tg, service_tg, make_config, install):
    cfg = service_cfg(make_config, install, fake_tg, service_tg)
    service_tg.add_text(STRANGER, "/status")
    service_tg.add_text(MEMBER, "/status")
    service_tg.add_text(OWNER, "/status")
    await run_until(cfg, lambda: any(t.startswith("✅ All good") for t in sent_texts(service_tg)))
    assert all(item["params"]["chat_id"] == OWNER for item in service_tg.sent)
    assert len(query(cfg, "SELECT id FROM event_log WHERE kind='service_update_rejected'")) == 2


async def test_a_broken_service_bot_token_turns_the_bot_off_not_intake(fake_tg, service_tg, make_config, install):
    cfg = service_cfg(make_config, install, fake_tg, service_tg)
    install["service_token_file"].chmod(0o644)  # e.g. after the owner typed it in again
    fake_tg.add_document(OWNER, "a.pdf", pdf(1))
    await run_until(cfg, lambda: copied_count(cfg) == 1)
    assert query(cfg, "SELECT id FROM event_log WHERE kind='service_bot_off'")
    assert service_tg.calls == []


async def test_a_failing_service_loop_never_stops_intake(fake_tg, service_tg, make_config, install, monkeypatch):
    cfg = service_cfg(make_config, install, fake_tg, service_tg)

    async def broken(self, stop):
        raise RuntimeError("a bug in the service bot")

    monkeypatch.setattr(ServiceBot, "poll_forever", broken)
    fake_tg.add_document(OWNER, "a.pdf", pdf(1))
    failed = "SELECT id FROM event_log WHERE kind='loop_failed'"
    await run_until(cfg, lambda: copied_count(cfg) == 1 and bool(query(cfg, failed)))
    assert evidence_rows(cfg)[0]["copy_state"] == "copied"


async def test_service_token_never_lands_on_disk(fake_tg, service_tg, make_config, install):
    cfg = service_cfg(make_config, install, fake_tg, service_tg, snapshot_at="00:00", daily_line_at="00:00")
    fake_tg.add_document(OWNER, "a.pdf", pdf(1))
    await run_until(cfg, lambda: copied_count(cfg) == 1 and any(t.startswith("✅") for t in sent_texts(service_tg)))
    secret = SERVICE_TOKEN.encode()
    for path in list(cfg.data_dir.rglob("*")) + list(cfg.documents_dir.rglob("*")):
        if path.is_file() and path.name != "service-bot.token":
            assert secret not in path.read_bytes(), path


async def test_a_failing_snapshot_sweep_never_stops_intake(fake_tg, service_tg, make_config, install, monkeypatch):
    cfg = service_cfg(make_config, install, fake_tg, service_tg)

    def broken(self):
        raise PermissionError(13, "Permission denied")  # e.g. a snapshot folder Core may no longer remove

    monkeypatch.setattr(Snapshotter, "sweep", broken)
    fake_tg.add_document(OWNER, "a.pdf", pdf(1))
    await run_until(cfg, lambda: copied_count(cfg) == 1)
    assert query(cfg, "SELECT id FROM event_log WHERE kind='snapshot_sweep_failed'")


async def test_a_hanging_documents_folder_never_blocks_intake_or_the_service_bot(
    fake_tg, service_tg, make_config, install, monkeypatch
):
    cfg = service_cfg(make_config, install, fake_tg, service_tg)
    release = threading.Event()
    write_copy = EvidenceStore._write_copy

    def hanging(self, *args):
        release.wait(60)  # e.g. a macOS permission prompt nobody answers
        return write_copy(self, *args)

    monkeypatch.setattr(EvidenceStore, "_write_copy", hanging)
    monkeypatch.setattr(app, "DocumentsFolder", functools.partial(DocumentsFolder, timeout=0.5))
    fake_tg.add_document(OWNER, "a.pdf", pdf(1))
    asked: set[str] = set()

    def progress():
        # Ask only once the copy has timed out: from then on the folder is stuck, and the probe must say so.
        if not asked and documents_alerts(service_tg):
            service_tg.add_text(OWNER, "/status")
            asked.add("status")
        attention = any(t.startswith("⚠️ Needs attention") for t in sent_texts(service_tg))
        return bool(receipts(fake_tg)) and attention

    try:
        await run_until(cfg, progress)
    finally:
        release.set()
    status = next(t for t in sent_texts(service_tg) if t.startswith("⚠️ Needs attention"))
    assert "unavailable (DocumentsTimeout)" in status
