"""Core process wiring."""

from __future__ import annotations

import asyncio
import fcntl
import functools
import logging
import os
import resource
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from . import db
from .aio import keep_running, sleep_or_stop, until_stopped
from .alerts import DOCUMENTS_GRACE_SECONDS, Alerts
from .config import Config, ConfigError, bot_id, read_service_token, read_token
from .documents import DocumentsFolder
from .events import EventLog
from .evidence import EvidenceStore
from .gatekeeper.outbox import Outbox
from .gatekeeper.receipts import ReceiptBatcher
from .gatekeeper.service import Gatekeeper
from .health import Health, check_documents
from .host import reference
from .host.adapter import AdapterServer, probe_peer
from .host.api import HostApi
from .host.egress import EgressProxy
from .host.gateway import LaunchdGateway
from .host.outbox import HostOutbox
from .host.queue import HostQueue
from .host.runtime import OPENCLAW_VERSION, Runtime, adapter_sha256
from .host.supervisor import Expectations, GatewayControl, HostTiming, Supervisor
from .host.turns import TurnRegistry
from .journal import InboundJournal
from .keys import KeyFileError, ensure_host_token, ensure_private_dir, load_or_create_key
from .schedule import DailyJob, Scheduler
from .servicebot import ServiceBot
from .snapshot import SnapshotError, Snapshotter
from .telegram.client import BotApi

SNAPSHOT_COPY_RETRY_SECONDS = 300.0
OPEN_FILES = 8192  # launchd gives an agent 256; sockets, the host's tunnels and SQLite need more
SNAPSHOT_FOLDER_TIMEOUT_SECONDS = 600.0  # copying a snapshot may take a while on a slow volume
Loop = Callable[[asyncio.Event], Awaitable[None]]
log = logging.getLogger("klepa_core")


class AlreadyRunning(Exception):
    """Another Core instance holds the lock on this data directory."""


def init_layout(cfg: Config) -> None:
    """Create the service data layout (0700), the signing key and core.db. Idempotent.

    The documents folder is not touched: it may live on a volume that is not mounted yet.
    """
    for directory in (cfg.data_dir, cfg.keys_dir, cfg.incoming_dir, cfg.journal_path.parent):
        ensure_private_dir(directory)
    (cfg.data_dir / ".metadata_never_index").touch(exist_ok=True)
    load_or_create_key(cfg.signing_key_path)
    conn = db.connect(cfg.core_db_path)
    try:
        db.migrate(conn)
        db.seed(conn, cfg)
    finally:
        conn.close()


def raise_file_limit(wanted: int = OPEN_FILES) -> int:
    """Raise the soft limit on open files towards `wanted`; returns the limit in force."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
    if soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError) as exc:
            log.warning("cannot raise the open file limit: %s", type(exc).__name__)
    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


def acquire_lock(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AlreadyRunning(str(path)) from None
    return fd


def _stopped_unexpectedly(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT kind FROM event_log WHERE kind IN ('core_started', 'core_stopped') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row is not None and row["kind"] == "core_started"


def _service_token(cfg: Config, events: EventLog) -> str | None:
    """A broken service bot token turns the service bot off; it never keeps family intake down."""
    if cfg.service_token_file is None:
        return None
    try:
        return read_service_token(cfg)
    except ConfigError as exc:
        events.log("service_bot_off", {"reason": str(exc)})  # the message never carries the token
        log.warning("the service bot is off: %s", exc)
        return None


async def _sweep_snapshots(snapshots: Snapshotter, events: EventLog) -> None:
    """Clean up half-built snapshots of a crash (the data disk only). A failure here never keeps intake down."""
    try:
        await asyncio.to_thread(snapshots.sweep)
    except Exception as exc:
        events.log("snapshot_sweep_failed", {"error": type(exc).__name__})
        log.warning("the snapshot sweep failed: %s", type(exc).__name__)


class Backups:
    """The daily snapshot, and retries of its copy into the documents folder."""

    def __init__(self, snapshots: Snapshotter, documents: DocumentsFolder, alerts: Alerts, events: EventLog) -> None:
        self.snapshots = snapshots
        self.documents = documents
        self.alerts = alerts
        self.events = events

    async def daily(self, day: str) -> None:
        try:
            await asyncio.to_thread(self.snapshots.take)
        except (SnapshotError, OSError, sqlite3.Error) as exc:
            self.alerts.raise_("snapshot_failed", error=type(exc).__name__)
            return
        await self.copy()
        try:
            await self.documents.call(self.snapshots.prune, timeout=SNAPSHOT_FOLDER_TIMEOUT_SECONDS)
        except OSError as exc:
            self.events.log("snapshot_prune_deferred", {"error": type(exc).__name__})

    async def copy(self) -> None:
        try:
            copied = await self.documents.call(self.snapshots.copy_pending, timeout=SNAPSHOT_FOLDER_TIMEOUT_SECONDS)
        except SnapshotError as exc:
            self.alerts.raise_("snapshot_failed", error=type(exc).__name__)
        except OSError as exc:
            self.alerts.documents_failed(type(exc).__name__, exc.errno)
        else:
            if copied:
                self.alerts.documents_ok()

    async def copy_forever(self, stop: asyncio.Event) -> None:
        """A copy that failed while the folder was unavailable is retried every few minutes."""
        while not stop.is_set():
            await until_stopped(stop, self.copy())
            await sleep_or_stop(stop, SNAPSHOT_COPY_RETRY_SECONDS)


async def _check_documents_at_start(documents: DocumentsFolder, alerts: Alerts, stop: asyncio.Event) -> None:
    """Probe the documents folder at start, and again after the grace period if that failed: at login the
    volume that holds it may mount a little after Core starts."""
    while not stop.is_set():
        probe = await until_stopped(stop, check_documents(documents))
        if probe is None:
            return
        if probe.ok:
            alerts.documents_ok()
            return
        if alerts.documents_failed(probe.error or "OSError", probe.code):
            return
        await sleep_or_stop(stop, alerts.documents_grace)


@dataclass(frozen=True)
class HostParts:
    queue: HostQueue
    supervisor: Supervisor
    loops: list[tuple[str, Loop]]


def _host(
    cfg: Config,
    conn: sqlite3.Connection,
    journal: InboundJournal,
    api: BotApi,
    outbox: Outbox,
    alerts: Alerts,
    events: EventLog,
    token: str,
    timing: HostTiming | None,
) -> HostParts | None:
    """The host behind the gatekeeper (docs/architecture.md: Host interface). None when no host is set up."""
    if cfg.host is None:
        return None
    host_token = ensure_host_token(cfg.host_token_path, bot_id(token))
    key = load_or_create_key(cfg.adapter_key_path)
    members = cfg.members_by_telegram_id()
    probe = probe_peer(key)
    queue = HostQueue(conn, journal, members, probe)
    turns = TurnRegistry(conn, queue)
    hold_text = cfg.locale.text("hold")
    expect, control = _gateway(cfg, probe)
    supervisor = Supervisor(
        conn,
        queue,
        events,
        alerts=alerts,
        expect=expect,
        control=control,
        timing=timing,
        on_hold=functools.partial(queue.send_hold_replies, outbox, hold_text),
    )
    host_outbox = HostOutbox(
        conn, api, supervisor, queue, turns, outbox, events, cfg.locale, timing=timing, alerts=alerts
    )
    host_api = HostApi(
        host_token, cfg.host.api_port, members, queue, supervisor, host_outbox, api, events, alerts=alerts
    )
    adapter = AdapterServer(cfg.host.socket_path, key, supervisor, turns, queue, events, cfg.locale)
    gatekeeper = [("127.0.0.1", cfg.host.api_port)]
    egress = EgressProxy(cfg.host.proxy_port, cfg.host.egress_allow, gatekeeper, events, alerts=alerts)
    loops: list[tuple[str, Loop]] = [
        ("host_api", host_api.serve_forever),
        ("adapter", adapter.serve_forever),
        ("egress", egress.serve_forever),
        ("supervisor", supervisor.run),
        ("host_outbox", host_outbox.run),
    ]
    return HostParts(queue, supervisor, loops)


def _gateway(cfg: Config, probe: int) -> tuple[Expectations | None, GatewayControl | None]:
    """Core's own gateway under launchd, held to the reference table (spec 4.6, 7.1); or one somebody else runs,
    which only the heartbeat and the live probe gate."""
    assert cfg.host is not None
    if cfg.host.gateway != "launchd":
        return None, None
    layout = reference.layout_of(cfg)
    table = reference.table(reference.settings_of(cfg, probe), layout)
    control = LaunchdGateway(Runtime(cfg.host.runtime_dir), layout, table, cfg.host.gateway_port)
    return reference.expectations(table, adapter_sha256(), OPENCLAW_VERSION), control


def _daily_jobs(cfg: Config, backups: Backups, service_bot: ServiceBot | None) -> list[DailyJob]:
    jobs: list[DailyJob] = []
    if cfg.snapshot_at is not None:
        jobs.append(DailyJob("snapshot", cfg.snapshot_at, backups.daily))
    if cfg.daily_line_at is not None and service_bot is not None:
        jobs.append(DailyJob("daily_line", cfg.daily_line_at, service_bot.send_daily_line))
    return jobs


async def run_service(
    cfg: Config,
    stop: asyncio.Event | None = None,
    *,
    copy_interval: float = 5.0,
    retry_seconds: float = 30.0,
    documents_grace: float = DOCUMENTS_GRACE_SECONDS,
    host_timing: HostTiming | None = None,
) -> None:
    ensure_private_dir(cfg.data_dir)
    raise_file_limit()
    lock_fd = acquire_lock(cfg.data_dir / "core.lock")
    try:
        init_layout(cfg)
        token = read_token(cfg)
        key = load_or_create_key(cfg.signing_key_path)
        snapshots = Snapshotter(cfg, key)
        documents = DocumentsFolder(cfg.documents_dir)
        conn = db.connect(cfg.core_db_path)
        journal = InboundJournal(cfg.journal_path)
        try:
            events = EventLog(conn)
            crashed = _stopped_unexpectedly(conn)
            await _sweep_snapshots(snapshots, events)
            service_token = _service_token(cfg, events)
            store = EvidenceStore(
                conn, cfg.incoming_dir, cfg.documents_dir, key, cfg.timezone, events, documents=documents
            )
            store.retry_failed_copies()
            stop = stop or asyncio.Event()
            async with aiohttp.ClientSession() as session:
                api = BotApi(session, cfg.api_root, token)
                outbox = Outbox(conn, api, events)
                outbox.recover()
                service_outbox: Outbox | None = None
                service_api: BotApi | None = None
                if service_token is not None:
                    service_api = BotApi(session, cfg.service_api_root, service_token)
                    service_outbox = Outbox(conn, service_api, events, bot="service")
                    service_outbox.recover()
                alerts = Alerts(conn, service_outbox, events, cfg.locale, documents_grace=documents_grace)
                try:
                    host = _host(cfg, conn, journal, api, outbox, alerts, events, token, host_timing)
                except (KeyFileError, OSError, ValueError) as exc:
                    # Broken host secrets turn the host off. Core keeps receiving and answers text itself.
                    events.log("host_off", {"error": type(exc).__name__})
                    log.warning("the host is off: %s", type(exc).__name__)
                    alerts.raise_("host_failed", reason=f"secrets:{type(exc).__name__}")
                    host = None
                supervisor = None if host is None else host.supervisor
                health = Health(cfg, conn, journal, host_state=None if supervisor is None else supervisor.describe)
                service_bot: ServiceBot | None = None
                if service_api is not None and service_outbox is not None:
                    probe = functools.partial(check_documents, documents)
                    service_bot = ServiceBot(
                        cfg, service_api, conn, service_outbox, health, events, probe=probe, supervisor=supervisor
                    )
                backups = Backups(snapshots, documents, alerts, events)
                batcher = ReceiptBatcher(outbox, journal, cfg.batch_window_seconds, cfg.locale)
                gatekeeper = Gatekeeper(
                    cfg,
                    api,
                    journal,
                    store,
                    outbox,
                    batcher,
                    events,
                    copy_interval=copy_interval,
                    retry_seconds=retry_seconds,
                    alerts=alerts,
                    host=None if host is None else host.queue,
                )
                scheduler = Scheduler(conn, cfg.timezone, _daily_jobs(cfg, backups, service_bot), events)
                side_loops: list[tuple[str, Loop]] = [
                    ("documents_check", functools.partial(_check_documents_at_start, documents, alerts)),
                    ("scheduler", scheduler.run_forever),
                    ("snapshot_copy", backups.copy_forever),
                ]
                if service_bot is not None and service_outbox is not None:
                    side_loops += [("service_bot", service_bot.poll_forever), ("service_outbox", service_outbox.run)]
                if host is not None:
                    side_loops += host.loops
                events.log("core_started")
                if crashed:
                    alerts.raise_("restarted")
                try:
                    async with asyncio.TaskGroup() as tasks:
                        # Family intake: when it fails, Core stops and launchd starts it again.
                        tasks.create_task(gatekeeper.poll_forever(stop))
                        tasks.create_task(gatekeeper.copy_forever(stop))
                        tasks.create_task(outbox.run(stop))
                        # Everything else must never take intake down.
                        for name, loop in side_loops:
                            tasks.create_task(keep_running(name, loop, stop, events))
                except BaseException:
                    batcher.cancel_all()  # like a crash: unfinished batches are redone after restart
                    raise
                batcher.flush_all()
                await outbox.send_due()
                if service_outbox is not None:
                    await service_outbox.send_due()
                events.log("core_stopped")
        finally:
            journal.close()
            conn.close()
    finally:
        os.close(lock_fd)
