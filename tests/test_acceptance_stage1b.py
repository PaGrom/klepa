"""Acceptance scenarios for stage 1b (docs/architecture.md: Testing)."""

import time

from helpers import BASE_CONFIG, OWNER, copied_count, evidence_rows, pdf, receipts, run_until


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
