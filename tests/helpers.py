"""Shared test data. Synthetic members only: real names and IDs never go into the repository."""

import asyncio
import socket
import sqlite3
from contextlib import closing

OWNER = 111111
MEMBER = 222222
STRANGER = 999999
TEST_TOKEN = "123:TEST-TOKEN"
SERVICE_TOKEN = "456:SERVICE-TOKEN"


def with_service_bot(text: str, token_file, api_root: str, *, snapshot_at="off", daily_line_at="off") -> str:
    """Config text with a service bot and the given schedule."""
    text = text.replace('snapshot_at = "off"', f'snapshot_at = "{snapshot_at}"')
    text = text.replace('daily_line_at = "off"', f'daily_line_at = "{daily_line_at}"')
    return text + f'\n[service_bot]\ntoken_file = "{token_file}"\napi_root = "{api_root}"\n'


def with_host(text: str, socket_path, api_port: int, proxy_port: int, allow: tuple[str, ...] = ()) -> str:
    """Config text with a host behind the gatekeeper."""
    entries = ", ".join(f'"{entry}"' for entry in allow)
    return text + (
        f'\n[host]\napi_port = {api_port}\nproxy_port = {proxy_port}\nsocket = "{socket_path}"\n'
        f"egress_allow = [{entries}]\n"
    )


def free_port() -> int:
    """A port nobody listens on right now. Tests only: another process could take it first."""
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Clock:
    """A clock that tests set by hand."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
album_quiet_seconds = 0.3

[schedule]
snapshot_at = "off"
daily_line_at = "off"

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


def pdf(i: int) -> bytes:
    return b"%PDF-1.4\n" + f"test document {i}\n".encode() * 50


def receipts(fake) -> list[str]:
    return [text for text in sent_texts(fake) if text.startswith(("📄", "🎧"))]


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
    task = asyncio.create_task(run_service(cfg, stop, copy_interval=0.05, retry_seconds=0.2, documents_grace=0.0))
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


async def wait_until(condition, *, timeout=5.0):
    """Poll `condition()` until it is true."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)
