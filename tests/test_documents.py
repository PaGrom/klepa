import asyncio
import threading
import time

import pytest

from helpers import wait_until
from klepa_core.documents import DocumentsFolder, DocumentsTimeout


async def test_a_call_runs_on_another_thread_and_returns_its_result(tmp_path):
    folder = DocumentsFolder(tmp_path)
    assert await folder.call(threading.get_ident) != threading.get_ident()
    assert await folder.call(sorted, [3, 1, 2]) == [1, 2, 3]


async def test_errors_come_back_unchanged(tmp_path):
    folder = DocumentsFolder(tmp_path)
    with pytest.raises(FileNotFoundError):
        await folder.call((tmp_path / "missing").read_bytes)


async def test_a_hanging_call_times_out_and_later_calls_fail_at_once_until_it_returns(tmp_path):
    folder = DocumentsFolder(tmp_path, timeout=0.2)
    release = threading.Event()
    with pytest.raises(DocumentsTimeout):
        await folder.call(release.wait)
    started = time.monotonic()
    with pytest.raises(DocumentsTimeout):
        await folder.call(sorted, [2, 1])
    assert time.monotonic() - started < 0.1  # no second thread waits on the stuck folder
    release.set()
    await wait_until(lambda: not folder.stuck)
    assert await folder.call(sorted, [2, 1]) == [1, 2]


async def test_the_event_loop_runs_on_while_the_folder_hangs(tmp_path):
    folder = DocumentsFolder(tmp_path, timeout=0.5)
    release = threading.Event()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    try:
        with pytest.raises(DocumentsTimeout):
            await folder.call(release.wait)
    finally:
        task.cancel()
        release.set()
    assert ticks > 10
