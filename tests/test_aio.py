import asyncio

from klepa_core import db
from klepa_core.aio import keep_running, until_stopped
from klepa_core.events import EventLog


async def test_keep_running_restarts_a_failed_loop_and_logs_each_failure(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    events = EventLog(conn)
    stop = asyncio.Event()
    runs = []

    async def flaky(stop_event: asyncio.Event) -> None:
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError("a bug in a side loop")
        stop_event.set()

    await asyncio.wait_for(keep_running("flaky", flaky, stop, events, first_pause=0.01), 5)
    assert len(runs) == 3
    assert events.kinds().count("loop_failed") == 2


async def test_until_stopped_cancels_the_awaitable_when_stopped():
    stop = asyncio.Event()
    never = asyncio.get_running_loop().create_future()
    stop.set()
    assert await until_stopped(stop, never) is None
    assert never.cancelled()
