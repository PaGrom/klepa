import asyncio

import pytest

from helpers import OWNER, TEST_TOKEN
from klepa_core.telegram.client import (
    Ambiguous,
    BadRequest,
    Conflict,
    DownloadFailed,
    NotSent,
    TooManyRequests,
    Unauthorized,
)


async def test_get_updates_and_offset_ack(fake_tg, api):
    first = fake_tg.add_text(OWNER, "hello")
    updates = await api.get_updates(None, 0)
    assert [u["update_id"] for u in updates] == [first["update_id"]]
    assert await api.get_updates(first["update_id"] + 1, 0) == []


@pytest.mark.parametrize(
    ("status", "error"), [(401, Unauthorized), (409, Conflict), (400, BadRequest), (500, Ambiguous)]
)
async def test_error_classification(fake_tg, api, status, error):
    fake_tg.fail("sendMessage", status=status, description="nope")
    with pytest.raises(error) as info:
        await api.send_message(OWNER, "x")
    assert TEST_TOKEN not in str(info.value)


async def test_too_many_requests_carries_retry_after(fake_tg, api):
    fake_tg.fail("sendMessage", status=429, description="Too Many Requests", retry_after=7)
    with pytest.raises(TooManyRequests) as info:
        await api.send_message(OWNER, "x")
    assert info.value.retry_after == 7


async def test_dropped_connection_is_ambiguous(fake_tg, api):
    fake_tg.fail("sendMessage", drop=True)
    with pytest.raises(Ambiguous):
        await api.send_message(OWNER, "x")


async def test_unreachable_server_is_not_sent(fake_tg, api):
    await fake_tg.stop()
    with pytest.raises(NotSent) as info:
        await api.send_message(OWNER, "x")
    assert TEST_TOKEN not in str(info.value)


async def test_send_message_disables_previews_and_uses_plain_text(fake_tg, api):
    result = await api.send_message(OWNER, "see https://example.com", reply_to_message_id=5)
    params = fake_tg.sent[-1]["params"]
    assert params["link_preview_options"] == {"is_disabled": True}
    assert "parse_mode" not in params
    assert params["reply_parameters"]["message_id"] == 5
    assert result["message_id"] == fake_tg.sent[-1]["message"]["message_id"]


async def test_download_and_limit(fake_tg, api):
    update = fake_tg.add_document(OWNER, "a.pdf", b"x" * 1000)
    info = await api.get_file(update["message"]["document"]["file_id"])
    assert await api.download(info["file_path"], 10_000) == b"x" * 1000
    with pytest.raises(DownloadFailed):
        await api.download(info["file_path"], 10)


async def test_truncated_download_fails_without_token_in_message(fake_tg, api):
    update = fake_tg.add_document(OWNER, "a.pdf", b"y" * 100_000)
    info = await api.get_file(update["message"]["document"]["file_id"])
    fake_tg.fail("download", drop=True)
    with pytest.raises(DownloadFailed) as failure:
        await api.download(info["file_path"], 1_000_000)
    assert TEST_TOKEN not in str(failure.value)


async def test_get_file_too_big(fake_tg, api):
    update = fake_tg.add_document(OWNER, "big.pdf", b"z", file_size=25 * 1024 * 1024)
    with pytest.raises(BadRequest, match="too big"):
        await api.get_file(update["message"]["document"]["file_id"])


async def test_stalled_download_fails_fast(fake_tg, api):
    update = fake_tg.add_document(OWNER, "a.pdf", b"y" * 100_000)
    info = await api.get_file(update["message"]["document"]["file_id"])
    fake_tg.fail("download", stall=True)
    started = asyncio.get_running_loop().time()
    with pytest.raises(DownloadFailed):
        await api.download(info["file_path"], 1_000_000, read_timeout=0.3)
    assert asyncio.get_running_loop().time() - started < 1.5
