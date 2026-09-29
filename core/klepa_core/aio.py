"""Small asyncio helpers shared by Core's loops."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from .events import EventLog

MAX_RESTART_PAUSE_SECONDS = 300.0
log = logging.getLogger("klepa_core")


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def until_stopped[T](stop: asyncio.Event, awaitable: Awaitable[T]) -> T | None:
    """Await `awaitable` unless `stop` is set first; then cancel it and return None."""
    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(stop.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if task in done:
        return task.result()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    return None


async def keep_running(
    name: str,
    loop: Callable[[asyncio.Event], Awaitable[None]],
    stop: asyncio.Event,
    events: EventLog,
    *,
    first_pause: float = 1.0,
) -> None:
    """Run a loop that must never take Core down: the service bot, the scheduler, the snapshot copier.

    A failure is logged, and the loop starts again after a pause that doubles up to five minutes.
    Family intake does not run under this: when intake fails, Core stops and launchd starts it again.
    """
    pause = first_pause
    while not stop.is_set():
        try:
            await loop(stop)
            return
        except Exception as exc:
            events.log("loop_failed", {"loop": name, "error": type(exc).__name__})
            log.warning("%s failed with %s; starting it again in %.0f s", name, type(exc).__name__, pause)
            await sleep_or_stop(stop, pause)
            pause = min(pause * 2, MAX_RESTART_PAUSE_SECONDS)
