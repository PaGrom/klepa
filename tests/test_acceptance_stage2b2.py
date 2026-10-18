"""Acceptance scenarios for stage 2b-2, Core's side: the attachment reaches the host as text, the chat's order, the
interim answer and cancel (spec 6.1 items 4-6; scenarios 1, 6, 34). The host is the fake one of tests/fakehost.py,
the readers are the stand-ins of fakereaders.py in the real sandbox; the real gateway's run is in
test_gateway_live.py."""

import pytest

from fakereaders import processing_config, processing_section
from helpers import BASE_CONFIG, OWNER, free_port, wait_until, with_host, with_service_bot
from klepa_core import db
from klepa_core.app import init_layout
from klepa_core.processing.sandbox import sandbox_available
from test_acceptance_stage1c import running, stand

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="the readers run in sandbox-exec, which macOS has")


@pytest.fixture
def cfg(make_config, install, fake_tg, service_tg, short_dir, tmp_path):
    api_port = free_port()
    proxy_port = free_port()
    while proxy_port == api_port:
        proxy_port = free_port()
    text = with_service_bot(BASE_CONFIG, install["service_token_file"], service_tg.url)
    text = with_host(text, short_dir / "adapter.sock", api_port, proxy_port)
    cfg = make_config(api_root=fake_tg.url, text=text + processing_section(processing_config(tmp_path)))
    init_layout(cfg)
    conn = db.connect(cfg.core_db_path)
    conn.execute("INSERT INTO service_binding(person_id, chat_id, bound_at) VALUES ('owner', ?, 't')", (OWNER,))
    conn.close()
    return cfg


def host_texts(s):
    return [text for text in s.host.texts() if not text.startswith("Klepa ")]  # the probes are not people's


async def test_a_text_after_a_voice_note_whose_download_is_retried_waits_for_it(cfg, fake_tg):
    """Review 1's I2: a file Telegram did not give at the first try is fetched again later; the chat's next message
    waits for it, so a "cancel that" never overtakes the voice note it answers."""
    async with stand(cfg) as s:
        await running(cfg)
        fake_tg.fail("getFile", status=500, description="Internal Server Error")
        fake_tg.add_voice(OWNER, b"remind me tomorrow")
        fake_tg.add_text(OWNER, "cancel that")
        await wait_until(lambda: len(host_texts(s)) == 2, timeout=30)
        first, second = host_texts(s)
        assert "transcript: remind me tomorrow" in first
        assert second == "cancel that"
