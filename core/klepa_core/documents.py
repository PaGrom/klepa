"""The documents folder (docs/architecture.md: Storage): a plain folder that may hang.

A stalled sync client, a network volume that went away or a macOS permission prompt nobody has answered can
block a file call for minutes. So every call into the folder runs on a thread of its own, one call at a time,
with a timeout. A call that overruns is left behind; until it returns, every other call fails at once with
DocumentsTimeout instead of piling up more stuck threads. The threads are daemon threads, so a stuck call
never keeps Core from exiting.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import errno
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 20.0


class DocumentsTimeout(TimeoutError):
    """The documents folder did not answer in time, or an earlier call into it has not returned yet."""

    def __init__(self) -> None:
        super().__init__(errno.ETIMEDOUT, "the documents folder does not answer")


def _work[T](future: concurrent.futures.Future[T], fn: Callable[..., T], args: tuple[Any, ...]) -> None:
    if not future.set_running_or_notify_cancel():
        return
    try:
        result = fn(*args)
    except BaseException as exc:
        future.set_exception(exc)
    else:
        future.set_result(result)


class DocumentsFolder:
    def __init__(self, root: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.root = root
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._stuck: concurrent.futures.Future[Any] | None = None

    @property
    def stuck(self) -> bool:
        """An earlier call overran its timeout and has not returned yet."""
        return self._stuck is not None and not self._stuck.done()

    async def call[T](self, fn: Callable[..., T], *args: Any, timeout: float | None = None) -> T:
        """Run `fn(*args)` on a thread of its own and return its result or raise its exception.

        Raises DocumentsTimeout when the call takes longer than `timeout`, and at once while an earlier call
        is still stuck.
        """
        async with self._lock:
            if self.stuck:
                raise DocumentsTimeout
            future: concurrent.futures.Future[T] = concurrent.futures.Future()
            threading.Thread(target=_work, args=(future, fn, args), name="klepa-documents", daemon=True).start()
            limit = self.timeout if timeout is None else timeout
            try:
                return await asyncio.wait_for(asyncio.wrap_future(future), limit)
            except TimeoutError:  # ours, or the call's own ETIMEDOUT: either way the folder does not answer
                if not future.done():
                    self._stuck = future  # still running: every call fails at once until it returns
                raise DocumentsTimeout from None
            except asyncio.CancelledError:
                if not future.done():
                    self._stuck = future
                raise
