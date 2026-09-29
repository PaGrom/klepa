"""Shared test data. Synthetic members only: real names and IDs never go into the repository."""

import asyncio
import sqlite3
from contextlib import closing

OWNER = 111111
MEMBER = 222222
STRANGER = 999999
TEST_TOKEN = "123:TEST-TOKEN"

BASE_CONFIG = """
timezone = "Europe/Berlin"
locale = "en"

[paths]
data_dir = "{data_dir}"
documents_dir = "{documents_dir}"

[telegram]
token_file = "{token_file}"
api_root = "{api_root}"

[intake]
batch_window_seconds = 0.2
poll_timeout_seconds = 1

[[members]]
person_id = "owner"
telegram_id = 111111
name = "Owner"
role = "owner"

[[members]]
person_id = "member"
telegram_id = 222222
name = "Member"
role = "member"
"""


def sent_texts(fake) -> list[str]:
    return [item["params"]["text"] for item in fake.sent]


def query(cfg, sql, *args):
    if not cfg.core_db_path.exists():
        return []
    with closing(sqlite3.connect(cfg.core_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []  # the schema is not created yet


def evidence_rows(cfg):
    return query(cfg, "SELECT * FROM evidence ORDER BY message_id")


def copied_count(cfg) -> int:
    return sum(1 for row in evidence_rows(cfg) if row["copy_state"] == "copied")


async def run_until(cfg, predicate, *, timeout=15.0):
    """Run Core against the fake Telegram until `predicate()` is true, then stop it gracefully."""
    from klepa_core.app import run_service

    stop = asyncio.Event()
    task = asyncio.create_task(run_service(cfg, stop, copy_interval=0.05, retry_seconds=0.2))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while not predicate():
            if task.done():
                task.result()
                raise AssertionError("Core stopped before the condition was reached")
            if loop.time() > deadline:
                raise AssertionError("condition not reached in time")
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)
