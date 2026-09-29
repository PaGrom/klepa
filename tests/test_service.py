import asyncio

import pytest

from helpers import OWNER, STRANGER, copied_count, evidence_rows, query, run_until, sent_texts
from klepa_core.app import AlreadyRunning, run_service
from klepa_core.locale import load_locale

EN = load_locale("en")


async def test_document_is_stored_copied_and_acknowledged(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    update = fake_tg.add_document(OWNER, "Invoice.pdf", b"%PDF-1.4 invoice")
    await run_until(cfg, lambda: sent_texts(fake_tg) == ["📄 got it"] and copied_count(cfg) == 1)
    row = evidence_rows(cfg)[0]
    assert (row["original_name"], row["space_id"], row["authenticated_subject"]) == ("Invoice.pdf", "shared", "owner")
    assert (cfg.documents_dir / row["documents_path"]).read_bytes() == b"%PDF-1.4 invoice"
    assert fake_tg.sent[-1]["params"]["reply_parameters"]["message_id"] == update["message"]["message_id"]


async def test_private_caption_goes_to_personal_space(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_document(OWNER, "lab-results.pdf", b"%PDF", caption="just for me")
    await run_until(cfg, lambda: copied_count(cfg) == 1)
    row = evidence_rows(cfg)[0]
    assert row["space_id"] == "personal:owner"
    assert row["documents_path"].startswith("Owner/")


async def test_text_and_commands_get_fixed_replies(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_text(OWNER, "hello")
    fake_tg.add_text(OWNER, "/start")
    fake_tg.add_text(OWNER, "/model")
    await run_until(cfg, lambda: len(fake_tg.sent) == 3)
    assert sorted(sent_texts(fake_tg)) == sorted([EN.text("stage1"), EN.text("start"), EN.text("no_commands")])


async def test_stranger_is_ignored_silently(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.add_document(STRANGER, "x.pdf", b"x")
    fake_tg.add_text(OWNER, "ping")
    await run_until(cfg, lambda: len(fake_tg.sent) == 1)
    assert sent_texts(fake_tg) == [EN.text("stage1")]
    assert "getFile" not in fake_tg.calls
    assert evidence_rows(cfg) == []
    assert query(cfg, "SELECT kind FROM event_log WHERE kind='update_rejected'")


async def test_second_instance_refuses_to_start(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    stop = asyncio.Event()
    first = asyncio.create_task(run_service(cfg, stop, copy_interval=0.05))
    await asyncio.sleep(0.3)
    with pytest.raises(AlreadyRunning):
        await run_service(cfg, asyncio.Event())
    stop.set()
    await asyncio.wait_for(first, 20)


async def test_token_never_lands_on_disk(fake_tg, make_config):
    cfg = make_config(api_root=fake_tg.url)
    fake_tg.fail("getUpdates", status=500)
    fake_tg.add_document(OWNER, "a.pdf", b"%PDF")
    await run_until(cfg, lambda: copied_count(cfg) == 1)
    for path in list(cfg.data_dir.rglob("*")) + list(cfg.documents_dir.rglob("*")):
        if path.is_file() and path.name != "family-bot.token":
            assert b"TEST-TOKEN" not in path.read_bytes(), path
