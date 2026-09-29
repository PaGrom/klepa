"""Daily jobs at a local time of day, with one catch-up run after sleep or downtime.

A job is marked as run for the day before it starts, so a job that brings Core down is not repeated in a
loop; a missed day is visible in the daily line instead.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from datetime import time as time_of_day
from zoneinfo import ZoneInfo

from .aio import sleep_or_stop, until_stopped
from .events import EventLog


@dataclass(frozen=True)
class DailyJob:
    name: str
    at: time_of_day
    run: Callable[[str], Awaitable[None]]  # gets the local day, YYYY-MM-DD


class Scheduler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        timezone: str,
        jobs: list[DailyJob],
        events: EventLog,
        *,
        clock: Callable[[], float] = time.time,
        tick_seconds: float = 30.0,
    ) -> None:
        self.conn = conn
        self.tz = ZoneInfo(timezone)
        self.jobs = jobs
        self.events = events
        self.clock = clock
        self.tick_seconds = tick_seconds

    def _last_day(self, name: str) -> str | None:
        row = self.conn.execute("SELECT last_day FROM job_run WHERE name=?", (name,)).fetchone()
        return None if row is None else str(row["last_day"])

    def _mark(self, name: str, day: str) -> None:
        self.conn.execute(
            "INSERT INTO job_run(name, last_day) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_day=excluded.last_day",
            (name, day),
        )

    async def run_due(self) -> None:
        local = datetime.fromtimestamp(self.clock(), self.tz)
        today = local.date().isoformat()
        for job in self.jobs:
            if local.time() < job.at or self._last_day(job.name) == today:
                continue
            self._mark(job.name, today)
            self.events.log("job_started", {"job": job.name, "day": today})
            try:
                await job.run(today)
            except Exception as exc:
                self.events.log("job_failed", {"job": job.name, "error": type(exc).__name__})

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            # A job may wait on the documents folder; stopping Core must not wait with it.
            await until_stopped(stop, self.run_due())
            await sleep_or_stop(stop, self.tick_seconds)
