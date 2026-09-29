"""Core process wiring (stage 1a)."""
from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path

import aiohttp

from . import db
from .config import Config, read_token
from .events import EventLog
from .evidence import EvidenceStore
from .gatekeeper.outbox import Outbox
from .gatekeeper.receipts import ReceiptBatcher
from .gatekeeper.service import Gatekeeper
from .journal import InboundJournal
from .keys import ensure_private_dir, load_or_create_key
from .telegram.client import BotApi


class AlreadyRunning(Exception):
    """Another Core instance holds the lock on this data directory."""


def init_layout(cfg: Config) -> None:
    """Create the service data layout (0700), the signing key and core.db. Idempotent."""
    for directory in (cfg.data_dir, cfg.keys_dir, cfg.incoming_dir, cfg.journal_path.parent):
        ensure_private_dir(directory)
    (cfg.data_dir / ".metadata_never_index").touch(exist_ok=True)
    cfg.documents_dir.mkdir(parents=True, exist_ok=True)
    load_or_create_key(cfg.signing_key_path)
    conn = db.connect(cfg.core_db_path)
    try:
        db.migrate(conn)
        db.seed(conn, cfg)
    finally:
        conn.close()


def acquire_lock(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AlreadyRunning(str(path)) from None
    return fd


async def run_service(cfg: Config, stop: asyncio.Event | None = None, *, copy_interval: float = 5.0,
                      retry_seconds: float = 30.0) -> None:
    ensure_private_dir(cfg.data_dir)
    lock_fd = acquire_lock(cfg.data_dir / "core.lock")
    try:
        init_layout(cfg)
        token = read_token(cfg)
        key = load_or_create_key(cfg.signing_key_path)
        conn = db.connect(cfg.core_db_path)
        journal = InboundJournal(cfg.journal_path)
        try:
            events = EventLog(conn)
            store = EvidenceStore(conn, cfg.incoming_dir, cfg.documents_dir, key, cfg.timezone, events)
            stop = stop or asyncio.Event()
            async with aiohttp.ClientSession() as session:
                api = BotApi(session, cfg.api_root, token)
                outbox = Outbox(conn, api, events)
                outbox.recover()
                batcher = ReceiptBatcher(outbox, journal, cfg.batch_window_seconds, cfg.locale)
                gatekeeper = Gatekeeper(cfg, api, journal, store, outbox, batcher, events,
                                        copy_interval=copy_interval, retry_seconds=retry_seconds)
                events.log("core_started")
                try:
                    async with asyncio.TaskGroup() as tasks:
                        tasks.create_task(gatekeeper.poll_forever(stop))
                        tasks.create_task(gatekeeper.copy_forever(stop))
                        tasks.create_task(outbox.run(stop))
                except BaseException:
                    batcher.cancel_all()  # like a crash: unfinished batches are redone after restart
                    raise
                batcher.flush_all()
                await outbox.send_due()
                events.log("core_stopped")
        finally:
            journal.close()
            conn.close()
    finally:
        os.close(lock_fd)
