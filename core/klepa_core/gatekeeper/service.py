"""Gatekeeper, stage 1a: Core owns the family bot (docs/architecture.md: Intake, D33).

In this plan Core answers text itself. Serving text to the OpenClaw host arrives in plan 1c.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..aio import sleep_or_stop, until_stopped
from ..config import Config, Member
from ..events import EventLog, utc_now_iso
from ..evidence import EvidenceStore, IncomingFile
from ..journal import InboundJournal
from ..spaces import decide_space, is_private_caption
from ..telegram.client import (
    Ambiguous,
    BadRequest,
    BotApi,
    Conflict,
    DownloadFailed,
    NotSent,
    TooManyRequests,
    Unauthorized,
)
from .intake import Classified, classify
from .outbox import Outbox
from .receipts import ReceiptBatcher

MAX_FILE_ATTEMPTS = 5  # failed downloads or saves of one file before the sender is asked to send it again


class Gatekeeper:
    def __init__(
        self,
        cfg: Config,
        api: BotApi,
        journal: InboundJournal,
        store: EvidenceStore,
        outbox: Outbox,
        batcher: ReceiptBatcher,
        events: EventLog,
        *,
        copy_interval: float = 5.0,
        retry_seconds: float = 30.0,
    ) -> None:
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
        self._attempts: dict[int, int] = {}

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
        assert c.chat_id is not None
        assert c.message_id is not None
        self.outbox.enqueue_text(
            f"reply:{c.chat_id}:{c.message_id}", c.chat_id, self.cfg.locale.text(text_key), reply_to=c.message_id
        )
        self.journal.mark(update_id, "done")

    def _space_for(self, c: Classified) -> str:
        """The caption decides the space (D32). Telegram puts an album's caption on one of its items,
        so a private caption on any item of an album makes the whole album personal."""
        assert c.person_id is not None
        assert c.chat_id is not None
        personal = f"personal:{c.person_id}"
        space_id = decide_space(c.caption, c.person_id, self.cfg.default_space, self.cfg.private_keywords)
        if c.media_group_id is None:
            return space_id
        if space_id != personal and (
            self._album_caption_is_private(c.chat_id, c.media_group_id)
            or personal in self.store.album_spaces(c.chat_id, c.media_group_id)
        ):
            space_id = personal
        if space_id == personal:
            self.store.move_album(c.chat_id, c.media_group_id, personal)
        return space_id

    def _album_caption_is_private(self, chat_id: int, media_group_id: str) -> bool:
        """Look at every journaled item of the album, including ones whose download has not succeeded yet."""
        for _, update in self.journal.pending():
            msg = update.get("message") or {}
            if (
                msg.get("media_group_id") == media_group_id
                and (msg.get("chat") or {}).get("id") == chat_id
                and is_private_caption(msg.get("caption"), self.cfg.private_keywords)
            ):
                return True
        return False

    def _ready_to_copy(self, row: Any) -> bool:
        """Copy only closed batches, and album items only after the album has been quiet for a while:
        until then a late item may still carry a private caption (#15)."""
        if self.journal.state(row["update_id"]) != "done":
            return False
        if row["media_group_id"] is None:
            return True
        last = self.store.album_last_received(row["chat_id"], row["media_group_id"])
        return last is not None and time.time() - last >= self.cfg.album_quiet_seconds

    def _incoming(self, update_id: int, c: Classified, data: bytes, space_id: str) -> IncomingFile:
        att = c.attachment
        assert att is not None
        assert c.chat_id is not None
        assert c.message_id is not None
        assert c.person_id is not None
        return IncomingFile(
            update_id=update_id,
            chat_id=c.chat_id,
            message_id=c.message_id,
            message_date=c.date or int(time.time()),
            person_id=c.person_id,
            space_id=space_id,
            kind=att.kind,
            original_name=att.file_name,
            mime=att.mime,
            data=data,
            file_id=att.file_id,
            file_unique_id=att.file_unique_id,
            media_group_id=c.media_group_id,
            caption=c.caption,
            tags=("forwarded", "external") if c.forwarded else (),
        )

    async def _ingest(self, update_id: int, c: Classified) -> None:
        att = c.attachment
        assert att is not None
        assert c.chat_id is not None
        assert c.message_id is not None
        space_id = self._space_for(c)  # before the download: a failed download must not reopen the album
        if att.file_size is not None and att.file_size > self.cfg.max_file_bytes:
            self._too_large(update_id, c, space_id)
            return
        self.batcher.hold(c.chat_id)  # a slow download must not split an album into two receipts
        try:
            info = await self.api.get_file(att.file_id)
            data = await self.api.download(info["file_path"], self.cfg.max_file_bytes)
        except BadRequest as exc:
            self.batcher.release(c.chat_id)
            if "too big" in exc.description.lower():
                self._too_large(update_id, c, space_id)
                return
            self._give_up(update_id, c, f"getFile {exc.code}")
            return
        except (NotSent, Ambiguous, TooManyRequests, Unauthorized, DownloadFailed) as exc:
            self._retry_later(update_id, c, exc)
            return
        try:
            row = await self.store.ingest(self._incoming(update_id, c, data, space_id))
        except OSError as exc:  # a full or failing data disk: keep the update and try again later
            self._retry_later(update_id, c, exc)
            return
        self._retry_at.pop(update_id, None)
        self._attempts.pop(update_id, None)
        if row["update_id"] != update_id and self.journal.state(row["update_id"]) == "done":
            # The same message came again under a new update_id after its receipt was queued.
            self.batcher.release(c.chat_id)
            self.journal.mark(update_id, "done", "repeat")
            return
        self.batcher.add(c.chat_id, update_id, c.message_id, att.kind)

    def _retry_later(self, update_id: int, c: Classified, exc: Exception) -> None:
        assert c.chat_id is not None
        self.batcher.release(c.chat_id)
        if isinstance(exc, (DownloadFailed, Ambiguous, OSError)):  # trouble with this file, not with the channel
            self._attempts[update_id] = self._attempts.get(update_id, 0) + 1
            if self._attempts[update_id] >= MAX_FILE_ATTEMPTS:
                self._give_up(update_id, c, type(exc).__name__)
                return
        # Type and errno only: an OSError's text carries a file path.
        self.events.log(
            "attachment_retry",
            {"update_id": update_id, "error": type(exc).__name__, "errno": getattr(exc, "errno", None)},
        )
        self._retry_at[update_id] = time.monotonic() + self.retry_seconds

    def _give_up(self, update_id: int, c: Classified, reason: str) -> None:
        assert c.chat_id is not None
        assert c.message_id is not None
        self._attempts.pop(update_id, None)
        self._retry_at.pop(update_id, None)
        self.events.log("attachment_failed", {"update_id": update_id, "reason": reason})
        self.outbox.enqueue_text(
            f"failed:{c.chat_id}:{c.message_id}", c.chat_id, self.cfg.locale.text("failed"), reply_to=c.message_id
        )
        self.journal.mark(update_id, "failed", reason)

    def _too_large(self, update_id: int, c: Classified, space_id: str) -> None:
        assert c.attachment is not None
        assert c.chat_id is not None
        assert c.message_id is not None
        self.store.record_too_large(self._incoming(update_id, c, b"", space_id), c.attachment.file_size)
        self.outbox.enqueue_text(
            f"too_large:{c.chat_id}:{c.message_id}", c.chat_id, self.cfg.locale.text("too_large"), reply_to=c.message_id
        )
        self.journal.mark(update_id, "done", "too_large")

    async def _get_updates_or_stop(self, stop: asyncio.Event, offset: int | None) -> list[dict[str, Any]] | None:
        # Updates of a cancelled poll were not acknowledged; Telegram delivers them again.
        return await until_stopped(stop, self.api.get_updates(offset, self.cfg.poll_timeout_seconds))

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
                await sleep_or_stop(stop, 30)
                continue
            except Conflict:
                self.events.log("channel_conflict")
                await sleep_or_stop(stop, 30)
                continue
            except TooManyRequests as exc:
                await sleep_or_stop(stop, exc.retry_after or 1.0)
                continue
            except (NotSent, Ambiguous, BadRequest) as exc:
                self.events.log("poll_error", {"error": type(exc).__name__})
                await sleep_or_stop(stop, backoff)
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
            await self.store.copy_pending(ready=self._ready_to_copy)
            await sleep_or_stop(stop, self.copy_interval)
