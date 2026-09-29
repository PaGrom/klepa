"""Gatekeeper, stage 1a: Core owns the family bot (spec D33, §6.1).

In this plan Core answers text itself. Serving text to the OpenClaw host arrives in plan 1c.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from ..config import Config, Member
from ..events import EventLog, utc_now_iso
from ..evidence import EvidenceStore, IncomingFile
from ..journal import InboundJournal
from ..spaces import decide_space
from ..telegram.client import (Ambiguous, BadRequest, BotApi, Conflict, DownloadFailed, NotSent, TooManyRequests,
                               Unauthorized)
from .intake import Classified, classify
from .outbox import Outbox
from .receipts import ReceiptBatcher

async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


class Gatekeeper:
    def __init__(self, cfg: Config, api: BotApi, journal: InboundJournal, store: EvidenceStore, outbox: Outbox,
                 batcher: ReceiptBatcher, events: EventLog, *, copy_interval: float = 5.0,
                 retry_seconds: float = 30.0) -> None:
        self.cfg = cfg
        self.api = api
        self.journal = journal
        self.store = store
        self.outbox = outbox
        self.batcher = batcher
        self.events = events
        self.copy_interval = copy_interval
        self.retry_seconds = retry_seconds
        self.members: dict[int, Member] = cfg.members_by_telegram_id()
        self._retry_at: dict[int, float] = {}

    async def process_pending(self) -> None:
        """Finish journaled updates left 'new' by a crash or a failed attempt."""
        now = time.monotonic()
        for update_id, update in self.journal.pending():
            if self.batcher.has_update(update_id) or self._retry_at.get(update_id, 0.0) > now:
                continue
            await self.process(update_id, update)

    async def process(self, update_id: int, update: dict[str, Any]) -> None:
        c = classify(update, self.members)
        if c.action == "ignore":
            self.events.log("update_ignored", {"update_id": update_id, "reason": c.reason})
            self.journal.mark(update_id, "done", c.reason)
        elif c.action == "reject":
            self.events.log("update_rejected", {"update_id": update_id, "reason": c.reason, "from_id": c.from_id})
            self.journal.mark(update_id, "rejected", c.reason)
        elif c.action == "command":
            self._reply(update_id, c, "start" if c.command == "start" else "no_commands")
        elif c.action == "text":
            self._reply(update_id, c, "stage1")
        elif c.action == "unsupported":
            self._reply(update_id, c, "unsupported")
        else:
            await self._ingest(update_id, c)

    def _reply(self, update_id: int, c: Classified, text_key: str) -> None:
        assert c.chat_id is not None and c.message_id is not None
        self.outbox.enqueue_text(f"reply:{c.chat_id}:{c.message_id}", c.chat_id, self.cfg.locale.text(text_key),
                                 reply_to=c.message_id)
        self.journal.mark(update_id, "done")

    def _incoming(self, update_id: int, c: Classified, data: bytes) -> IncomingFile:
        att = c.attachment
        assert att is not None and c.chat_id is not None and c.message_id is not None and c.person_id is not None
        return IncomingFile(
            update_id=update_id, chat_id=c.chat_id, message_id=c.message_id,
            message_date=c.date or int(time.time()), person_id=c.person_id,
            space_id=decide_space(c.caption, c.person_id, self.cfg.default_space, self.cfg.private_keywords),
            kind=att.kind, original_name=att.file_name, mime=att.mime, data=data, file_id=att.file_id,
            file_unique_id=att.file_unique_id, media_group_id=c.media_group_id, caption=c.caption,
            tags=("forwarded", "external") if c.forwarded else ())

    async def _ingest(self, update_id: int, c: Classified) -> None:
        att = c.attachment
        assert att is not None and c.chat_id is not None and c.message_id is not None
        if att.file_size is not None and att.file_size > self.cfg.max_file_bytes:
            self._too_large(update_id, c)
            return
        self.batcher.hold(c.chat_id)  # a slow download must not split an album into two receipts
        try:
            info = await self.api.get_file(att.file_id)
            data = await self.api.download(info["file_path"], self.cfg.max_file_bytes)
        except BadRequest as exc:
            self.batcher.release(c.chat_id)
            if "too big" in exc.description.lower():
                self._too_large(update_id, c)
                return
            self.events.log("attachment_failed", {"update_id": update_id, "code": exc.code})
            self.outbox.enqueue_text(f"failed:{c.chat_id}:{c.message_id}", c.chat_id, self.cfg.locale.text("failed"),
                                     reply_to=c.message_id)
            self.journal.mark(update_id, "failed", f"getFile {exc.code}")
            return
        except (NotSent, Ambiguous, TooManyRequests, Unauthorized, DownloadFailed) as exc:
            self.batcher.release(c.chat_id)
            self.events.log("attachment_retry", {"update_id": update_id, "error": type(exc).__name__})
            self._retry_at[update_id] = time.monotonic() + self.retry_seconds
            return
        row = await self.store.ingest(self._incoming(update_id, c, data))
        self._retry_at.pop(update_id, None)
        if row["update_id"] != update_id and self.journal.state(row["update_id"]) == "done":
            # The same message came again under a new update_id after its receipt was queued.
            self.batcher.release(c.chat_id)
            self.journal.mark(update_id, "done", "repeat")
            return
        self.batcher.add(c.chat_id, update_id, c.message_id, att.kind)

    def _too_large(self, update_id: int, c: Classified) -> None:
        assert c.attachment is not None and c.chat_id is not None and c.message_id is not None
        self.store.record_too_large(self._incoming(update_id, c, b""), c.attachment.file_size)
        self.outbox.enqueue_text(f"too_large:{c.chat_id}:{c.message_id}", c.chat_id,
                                 self.cfg.locale.text("too_large"), reply_to=c.message_id)
        self.journal.mark(update_id, "done", "too_large")

    async def _get_updates_or_stop(self, stop: asyncio.Event, offset: int | None) -> list[dict[str, Any]] | None:
        poll = asyncio.ensure_future(self.api.get_updates(offset, self.cfg.poll_timeout_seconds))
        waiter = asyncio.ensure_future(stop.wait())
        try:
            done, _ = await asyncio.wait({poll, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if poll in done:
            return poll.result()
        poll.cancel()  # updates of a cancelled poll were not acknowledged; Telegram delivers them again
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await poll
        return None

    async def poll_forever(self, stop: asyncio.Event) -> None:
        offset = self.journal.next_offset()
        backoff = 1.0
        while not stop.is_set():
            await self.process_pending()
            try:
                updates = await self._get_updates_or_stop(stop, offset)
                backoff = 1.0
            except Unauthorized:
                self.events.log("channel_unauthorized")
                await _sleep_or_stop(stop, 30)
                continue
            except Conflict:
                self.events.log("channel_conflict")
                await _sleep_or_stop(stop, 30)
                continue
            except TooManyRequests as exc:
                await _sleep_or_stop(stop, exc.retry_after or 1.0)
                continue
            except (NotSent, Ambiguous, BadRequest) as exc:
                self.events.log("poll_error", {"error": type(exc).__name__})
                await _sleep_or_stop(stop, backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            if not updates:
                continue
            # Durable before the next getUpdates offset acknowledges them to Telegram.
            new_ids = self.journal.append_batch(updates, utc_now_iso())
            offset = max(int(update["update_id"]) for update in updates) + 1
            by_id = {int(update["update_id"]): update for update in updates}
            for update_id in sorted(new_ids):
                await self.process(update_id, by_id[update_id])

    async def copy_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.store.copy_pending()
            await _sleep_or_stop(stop, self.copy_interval)
