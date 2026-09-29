import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from klepa_core import db
from klepa_core.config import parse_time_of_day
from klepa_core.events import EventLog
from klepa_core.schedule import DailyJob, Scheduler

TZ = "Europe/Berlin"


def at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(TZ)).timestamp()


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "core.db")
    db.migrate(connection)
    return connection


def test_parse_time_of_day():
    assert parse_time_of_day("03:30") == time(3, 30)
    assert parse_time_of_day("off") is None
    for bad in ("3.30", "25:00", "noon", "", "03:30:00"):
        with pytest.raises(ValueError, match="HH:MM"):
            parse_time_of_day(bad)


async def test_a_job_runs_once_a_day_after_its_time_and_gets_the_local_day(conn):
    now = [at(5, 3, 0)]
    runs = []

    async def job(day):
        runs.append(day)

    scheduler = Scheduler(conn, TZ, [DailyJob("snapshot", time(3, 30), job)], EventLog(conn), clock=lambda: now[0])
    await scheduler.run_due()
    assert runs == []
    now[0] = at(5, 3, 31)
    await scheduler.run_due()
    await scheduler.run_due()
    assert runs == ["2026-10-05"]
    now[0] = at(6, 3, 29)
    await scheduler.run_due()
    assert runs == ["2026-10-05"]
    now[0] = at(6, 3, 30)
    await scheduler.run_due()
    assert runs == ["2026-10-05", "2026-10-06"]


async def test_sleeping_through_the_time_runs_each_job_once_after_wake(conn):
    now = [at(5, 11, 0)]  # the Mac slept through 03:30 and 09:00
    runs = []

    async def job(day):
        runs.append(day)

    jobs = [DailyJob("snapshot", time(3, 30), job), DailyJob("daily_line", time(9, 0), job)]
    events = EventLog(conn)
    await Scheduler(conn, TZ, jobs, events, clock=lambda: now[0]).run_due()
    assert len(runs) == 2
    # Core restarts at noon the same day: nothing runs twice.
    now[0] = at(5, 12, 0)
    await Scheduler(conn, TZ, jobs, events, clock=lambda: now[0]).run_due()
    assert len(runs) == 2
    rows = conn.execute("SELECT data FROM event_log WHERE kind='job_started'").fetchall()
    assert [json.loads(row["data"])["job"] for row in rows] == ["snapshot", "daily_line"]


async def test_a_failing_job_is_logged_and_not_retried_the_same_day(conn):
    now = [at(5, 4, 0)]
    calls = []

    async def broken(day):
        calls.append(day)
        raise RuntimeError("disk on fire")

    events = EventLog(conn)
    scheduler = Scheduler(conn, TZ, [DailyJob("snapshot", time(3, 30), broken)], events, clock=lambda: now[0])
    await scheduler.run_due()
    await scheduler.run_due()
    assert calls == ["2026-10-05"]
    assert events.kinds().count("job_failed") == 1
