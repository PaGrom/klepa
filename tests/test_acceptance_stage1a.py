"""Acceptance scenarios for stage 1a (spec §13.2: 2, 3, 5, 7, 12, 14, 28, 36; too large; Review Focus 1–3)."""
import asyncio
import errno
import random
import unicodedata

import pytest

from helpers import MEMBER, OWNER, STRANGER, copied_count, evidence_rows, query, run_until, sent_texts
from klepa_core.app import run_service
from klepa_core.cards import card_file_name, read_card
from klepa_core import evidence as evidence_module
from klepa_core.evidence import EvidenceStore
from klepa_core.journal import InboundJournal
from klepa_core.keys import load_or_create_key
from klepa_core.locale import load_locale

EN = load_locale("en")


class Crash(Exception):
    """Stands in for power loss or kill -9 at a chosen point."""


def pdf(i: int) -> bytes:
    return b"%PDF-1.4\n" + f"test document {i}\n".encode() * 50


def receipts(fake) -> list[str]:
    return [text for text in sent_texts(fake) if text.startswith(("📄", "🎧"))]


def stored_files(cfg) -> list:
    return sorted(p for p in cfg.incoming_dir.rglob("*") if p.is_file() and not p.name.startswith("."))


async def crash_run(cfg):
    with pytest.raises(ExceptionGroup) as info:
        await run_service(cfg, asyncio.Event(), copy_interval=0.05, retry_seconds=0.2)
    assert info.group_contains(Crash)


async def test_s2_thirteen_pdfs_in_two_sends(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    for i in range(10):
        fake_tg.add_document(OWNER, f"page{i}.pdf", pdf(i), media_group_id="album-1")
    second_send = {"done": False}

    def progress():
        if not second_send["done"] and receipts(fake_tg) == ["📄 got 10 files"]:
            for i in range(10, 13):
                fake_tg.add_document(OWNER, f"page{i}.pdf", pdf(i))
            second_send["done"] = True
        return len(receipts(fake_tg)) == 2 and copied_count(cfg) == 13

    await run_until(cfg, progress)
    assert receipts(fake_tg) == ["📄 got 10 files", "📄 got 3 files"]
    rows = evidence_rows(cfg)
    assert len(rows) == 13 and len({row["sha256"] for row in rows}) == 13
    assert len(stored_files(cfg)) == 13
    key = load_or_create_key(cfg.signing_key_path)
    for row in rows:
        target = cfg.documents_dir / row["documents_path"]
        assert read_card(target.parent / card_file_name(row["id"]), key)["sha256"] == row["sha256"]


async def test_s3_core_killed_mid_save_keeps_one_record_each(fake_tg, make_config, monkeypatch):
    cfg = make_config(api_root=fake_tg.url)
    for i in range(3):
        fake_tg.add_document(OWNER, f"doc{i}.pdf", pdf(i), media_group_id="g1")
    real_ingest = EvidenceStore.ingest
    calls = {"n": 0}

    async def crashing_ingest(self, f):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Crash("power loss while saving the second file")
        return await real_ingest(self, f)

    monkeypatch.setattr(EvidenceStore, "ingest", crashing_ingest)
    await crash_run(cfg)
    monkeypatch.setattr(EvidenceStore, "ingest", real_ingest)
    await run_until(cfg, lambda: len(receipts(fake_tg)) == 1 and copied_count(cfg) == 3)
    assert receipts(fake_tg) == ["📄 got 3 files"]
    assert len(evidence_rows(cfg)) == 3 and len(stored_files(cfg)) == 3


async def test_s3_crash_between_the_file_and_its_row_keeps_one_file(fake_tg, make_config, monkeypatch):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_document(OWNER, "a.pdf", pdf(1))
    real_transaction = evidence_module.transaction

    def power_loss(conn):
        raise Crash("power loss after the file became durable, before its row")

    monkeypatch.setattr(evidence_module, "transaction", power_loss)
    await crash_run(cfg)
    assert len(stored_files(cfg)) == 1 and evidence_rows(cfg) == []
    monkeypatch.setattr(evidence_module, "transaction", real_transaction)
    await run_until(cfg, lambda: receipts(fake_tg) == ["📄 got it"] and copied_count(cfg) == 1)
    assert len(stored_files(cfg)) == 1 and len(evidence_rows(cfg)) == 1


async def test_full_disk_while_saving_is_retried_not_fatal(fake_tg, make_config, monkeypatch):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_document(OWNER, "a.pdf", pdf(1))
    real_ingest = EvidenceStore.ingest
    calls = {"n": 0}

    async def full_disk_once(self, f):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ENOSPC, "No space left on device", "/private/incoming/a.pdf")
        return await real_ingest(self, f)

    monkeypatch.setattr(EvidenceStore, "ingest", full_disk_once)
    await run_until(cfg, lambda: receipts(fake_tg) == ["📄 got it"] and copied_count(cfg) == 1)
    retries = query(cfg, "SELECT data FROM event_log WHERE kind='attachment_retry'")
    assert retries and "/private/incoming" not in retries[0]["data"]

async def test_s3_crash_before_journaling_redelivers(fake_tg, make_config, monkeypatch):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_document(OWNER, "a.pdf", pdf(1))

    def failing_append(self, updates, received_at):
        raise Crash("died before the journal write")

    real_append = InboundJournal.append_batch
    monkeypatch.setattr(InboundJournal, "append_batch", failing_append)
    await crash_run(cfg)
    monkeypatch.setattr(InboundJournal, "append_batch", real_append)
    await run_until(cfg, lambda: receipts(fake_tg) == ["📄 got it"] and copied_count(cfg) == 1)
    assert len(evidence_rows(cfg)) == 1


async def test_s5_duplicate_delivery_gives_one_record(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    update = fake_tg.add_document(OWNER, "a.pdf", pdf(1))
    await run_until(cfg, lambda: receipts(fake_tg) == ["📄 got it"])
    fake_tg.redeliver(update)  # the same update_id again
    fake_tg.redeliver(update, new_update_id=True)  # the same message under a new update_id
    fake_tg.add_text(OWNER, "still there?")
    await run_until(cfg, lambda: len(fake_tg.sent) == 2)
    assert receipts(fake_tg) == ["📄 got it"]
    assert len(evidence_rows(cfg)) == 1 and len(stored_files(cfg)) == 1


async def test_s7_sender_comes_from_telegram_not_from_text(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_document(MEMBER, "b.pdf", pdf(2), caption="this is Owner writing")
    await run_until(cfg, lambda: copied_count(cfg) == 1)
    row = evidence_rows(cfg)[0]
    assert (row["authenticated_subject"], row["claimed_subject"]) == ("member", None)
    key = load_or_create_key(cfg.signing_key_path)
    card = read_card((cfg.documents_dir / row["documents_path"]).parent / card_file_name(row["id"]), key)
    assert card["authenticated_subject"] == "member"


async def test_s12_messages_sent_while_core_was_down_are_all_processed(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    for i in range(3):
        fake_tg.add_document(OWNER, f"d{i}.pdf", pdf(i))
    fake_tg.add_text(OWNER, "are you there?")
    await run_until(cfg, lambda: copied_count(cfg) == 3 and len(fake_tg.sent) == 2)
    assert sorted(sent_texts(fake_tg)) == sorted(["📄 got 3 files", EN.text("stage1")])


async def test_s14_strangers_groups_and_other_updates_are_not_stored(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_document(STRANGER, "spam.pdf", pdf(9))
    fake_tg.add_document(OWNER, "group.pdf", pdf(8), chat_id=-100123, chat_type="group")
    fake_tg.push({"channel_post": {"message_id": 1, "chat": {"id": -1001, "type": "channel"}}})
    fake_tg.push({"business_message": {"message_id": 1}})
    fake_tg.add_text(OWNER, "control message")
    await run_until(cfg, lambda: len(fake_tg.sent) == 1)
    assert sent_texts(fake_tg) == [EN.text("stage1")]
    assert "getFile" not in fake_tg.calls
    assert evidence_rows(cfg) == []
    assert len(query(cfg, "SELECT id FROM event_log WHERE kind IN ('update_rejected','update_ignored')")) == 4


NAMES = ["../x.pdf", "/etc/passwd", "\u202efdp.exe", "é" * 300 + ".pdf", "Scan.pdf", "scan.pdf",
         unicodedata.normalize("NFC", "Café.pdf"), unicodedata.normalize("NFD", "Café.pdf"), "Contract.pdf.json"]


async def test_s28_hostile_file_names_stay_inside_the_space_folder(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    for i, name in enumerate(NAMES):
        fake_tg.add_document(OWNER, name, pdf(i))
    await run_until(cfg, lambda: copied_count(cfg) == len(NAMES))
    key = load_or_create_key(cfg.signing_key_path)
    shared = (cfg.documents_dir / "Shared").resolve()
    rows = evidence_rows(cfg)
    assert len({row["documents_path"] for row in rows}) == len(NAMES)
    for row in rows:
        target = (cfg.documents_dir / row["documents_path"]).resolve()
        assert target.is_relative_to(shared)
        assert len(target.name.encode("utf-8")) <= 255
        assert target.read_bytes() == pdf(NAMES.index(row["original_name"]))
        card = read_card(target.parent / card_file_name(row["id"]), key)
        assert card["original_name"] == row["original_name"]
    assert not (cfg.documents_dir.parent / "x.pdf").exists()


async def test_s36_shuffled_and_repeated_album_gives_one_receipt(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    photos = [fake_tg.add_photo(OWNER, pdf(i) * 3, media_group_id="alb") for i in range(5)]
    random.Random(7).shuffle(fake_tg.updates)
    fake_tg.redeliver(photos[1])
    fake_tg.redeliver(photos[3], new_update_id=True)
    await run_until(cfg, lambda: copied_count(cfg) == 5 and len(receipts(fake_tg)) == 1)
    assert receipts(fake_tg) == ["📄 got 5 files"]
    assert len(stored_files(cfg)) == 5


async def test_s36_late_repeat_of_an_album_photo_gets_no_second_receipt(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    photos = [fake_tg.add_photo(OWNER, pdf(i) * 3, media_group_id="alb") for i in range(3)]
    await run_until(cfg, lambda: receipts(fake_tg) == ["📄 got 3 files"])
    repeat = fake_tg.redeliver(photos[2], new_update_id=True)
    fake_tg.add_text(OWNER, "still there?")
    await run_until(cfg, lambda: EN.text("stage1") in sent_texts(fake_tg))
    assert receipts(fake_tg) == ["📄 got 3 files"]
    journal = InboundJournal(cfg.journal_path)
    try:
        assert journal.state(repeat["update_id"]) == "done"
    finally:
        journal.close()

async def test_private_caption_on_one_album_item_keeps_the_whole_album_personal(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_photo(OWNER, pdf(0) * 3, media_group_id="priv")
    fake_tg.add_photo(OWNER, pdf(1) * 3, media_group_id="priv", caption="just for me")
    fake_tg.add_photo(OWNER, pdf(2) * 3, media_group_id="priv")
    await run_until(cfg, lambda: copied_count(cfg) == 3 and len(receipts(fake_tg)) == 1)
    assert {row["space_id"] for row in evidence_rows(cfg)} == {"personal:owner"}
    assert not (cfg.documents_dir / "Shared").exists()


async def test_private_album_stays_private_when_the_captioned_item_fails_to_download(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_photo(OWNER, pdf(0) * 3, media_group_id="priv", caption="just for me")
    fake_tg.add_photo(OWNER, pdf(1) * 3, media_group_id="priv")
    fake_tg.add_photo(OWNER, pdf(2) * 3, media_group_id="priv")
    fake_tg.fail("download", drop=True)  # the captioned photo is fetched first and fails once
    await run_until(cfg, lambda: copied_count(cfg) == 3)
    assert {row["space_id"] for row in evidence_rows(cfg)} == {"personal:owner"}
    assert not (cfg.documents_dir / "Shared").exists()

async def test_too_large_file_is_recorded_and_the_sender_is_asked_to_resend(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_document(OWNER, "video.mov", b"tiny", mime="video/quicktime", file_size=25 * 1024 * 1024)
    await run_until(cfg, lambda: sent_texts(fake_tg) == [EN.text("too_large")])
    row = evidence_rows(cfg)[0]
    assert (row["state"], row["copy_state"]) == ("too_large", "none")
    assert "getFile" not in fake_tg.calls


async def test_review_focus_documents_folder_unavailable(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    cfg.documents_dir.chmod(0o500)
    try:
        fake_tg.add_document(OWNER, "a.pdf", pdf(1))
        await run_until(cfg, lambda: receipts(fake_tg) == ["📄 got it"])
        assert evidence_rows(cfg)[0]["copy_state"] == "pending"
        assert len(stored_files(cfg)) == 1
    finally:
        cfg.documents_dir.chmod(0o700)
    await run_until(cfg, lambda: copied_count(cfg) == 1)


async def test_core_starts_and_receives_without_the_documents_folder(fake_tg, make_config, install):
    cfg = make_config(api_root=fake_tg.url)
    install["documents_dir"].rmdir()  # e.g. the Drive folder is not mounted yet
    install["tmp"].chmod(0o500)  # and it cannot be created
    try:
        fake_tg.add_document(OWNER, "a.pdf", pdf(1))
        await run_until(cfg, lambda: receipts(fake_tg) == ["📄 got it"])
        assert evidence_rows(cfg)[0]["copy_state"] == "pending"
    finally:
        install["tmp"].chmod(0o700)
    assert not cfg.documents_dir.exists()
    cfg.documents_dir.mkdir()
    await run_until(cfg, lambda: copied_count(cfg) == 1)

async def test_review_focus_truncated_download_is_retried_and_stored_intact(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    data = pdf(3) * 200
    fake_tg.add_document(OWNER, "big.pdf", data)
    fake_tg.fail("download", drop=True)
    await run_until(cfg, lambda: copied_count(cfg) == 1 and receipts(fake_tg) == ["📄 got it"])
    row = evidence_rows(cfg)[0]
    assert (cfg.incoming_dir / row["incoming_path"]).read_bytes() == data
    assert len(stored_files(cfg)) == 1
    assert query(cfg, "SELECT id FROM event_log WHERE kind='attachment_retry'")


async def test_a_file_that_never_downloads_is_given_up_and_the_sender_told(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    update = fake_tg.add_document(OWNER, "a.pdf", pdf(1) * 50)
    fake_tg.fail("download", drop=True, times=100)
    await run_until(cfg, lambda: sent_texts(fake_tg) == [EN.text("failed")], timeout=20)
    assert evidence_rows(cfg) == []
    journal = InboundJournal(cfg.journal_path)
    try:
        assert journal.state(update["update_id"]) == "failed"
    finally:
        journal.close()
    assert len(query(cfg, "SELECT id FROM event_log WHERE kind='attachment_retry'")) == 4

async def test_review_focus_restart_in_the_middle_of_an_album(fake_tg, make_config, monkeypatch):
    cfg = make_config(api_root=fake_tg.url)
    for i in range(5):
        fake_tg.add_photo(OWNER, pdf(i) * 3, media_group_id="alb")
    real_ingest = EvidenceStore.ingest
    calls = {"n": 0}

    async def flaky(self, f):
        calls["n"] += 1
        if calls["n"] == 4:
            raise Crash("power loss after three photos")
        return await real_ingest(self, f)

    monkeypatch.setattr(EvidenceStore, "ingest", flaky)
    await crash_run(cfg)
    assert len(stored_files(cfg)) == 3 and receipts(fake_tg) == []
    monkeypatch.setattr(EvidenceStore, "ingest", real_ingest)
    await run_until(cfg, lambda: len(receipts(fake_tg)) == 1 and copied_count(cfg) == 5)
    assert receipts(fake_tg) == ["📄 got 5 files"]
    assert len(stored_files(cfg)) == 5
