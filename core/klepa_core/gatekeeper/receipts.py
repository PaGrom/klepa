"""Batches of attachments (docs/architecture.md: Delivery).

A batch is the attachments of one chat with less than `window_seconds` between neighbours (an album
arrives that way). Without a host, Core says "got it" once per batch. With one, the batch becomes one message for
the host, issued once its files are read (spec 6.1 item 4), and the model answers on what they say: no receipt
comes first, so nobody asks about a file before it is read. Journal entries of a batch become 'done' only when its
receipt or its host message is queued, so after a crash they are processed again, and the idempotency key or the
message's items prevent a second one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..host.queue import HostQueue
from ..journal import InboundJournal
from ..locale import Locale
from .outbox import Outbox


@dataclass
class _Batch:
    items: list[tuple[int, int, str, str]] = field(default_factory=list)  # (update_id, message_id, kind, evidence_id)
    repeats: list[int] = field(default_factory=list)  # update_ids that brought a message already in items
    timer: asyncio.TimerHandle | None = None


class ReceiptBatcher:
    def __init__(
        self,
        outbox: Outbox,
        journal: InboundJournal,
        window_seconds: float,
        locale: Locale,
        *,
        host: HostQueue | None = None,
    ) -> None:
        self.outbox = outbox
        self.journal = journal
        self.window = window_seconds
        self.locale = locale
        self.host = host  # set: a batch is a message for the host, and no receipt comes first
        self._batches: dict[int, _Batch] = {}

    def add(self, chat_id: int, update_id: int, message_id: int, kind: str, evidence_id: str) -> None:
        batch = self._batches.setdefault(chat_id, _Batch())
        known = next((item for item in batch.items if item[1] == message_id), None)
        if known is not None:
            if known[0] != update_id:
                batch.repeats.append(update_id)  # the same message under a new update_id: this receipt covers it
            self.release(chat_id)  # hold() may have stopped the timer for this download
            return
        batch.items.append((update_id, message_id, kind, evidence_id))
        if batch.timer is not None:
            batch.timer.cancel()
        batch.timer = asyncio.get_running_loop().call_later(self.window, self.flush, chat_id)

    def hold(self, chat_id: int) -> None:
        """An attachment of this chat is being downloaded: keep its batch open."""
        batch = self._batches.get(chat_id)
        if batch is not None and batch.timer is not None:
            batch.timer.cancel()
            batch.timer = None

    def release(self, chat_id: int) -> None:
        """A download ended without a new item: re-arm the timer for what the batch already has."""
        batch = self._batches.get(chat_id)
        if batch is not None and batch.items and batch.timer is None:
            batch.timer = asyncio.get_running_loop().call_later(self.window, self.flush, chat_id)

    def has_update(self, update_id: int) -> bool:
        return any(
            update_id in batch.repeats or any(item[0] == update_id for item in batch.items)
            for batch in self._batches.values()
        )

    def pending_count(self) -> int:
        return sum(len(batch.items) for batch in self._batches.values())

    def flush(self, chat_id: int) -> None:
        batch = self._batches.pop(chat_id, None)
        if batch is None or not batch.items:
            return
        if batch.timer is not None:
            batch.timer.cancel()
        items = sorted(batch.items, key=lambda item: item[1])
        first_message_id = items[0][1]
        if self.host is not None:
            self.host.enqueue_attachment(chat_id, [(item[0], item[1], item[3]) for item in items])
        else:
            self.outbox.enqueue_text(
                f"receipt:{chat_id}:{first_message_id}",
                chat_id,
                self.locale.receipt([item[2] for item in items]),
                reply_to=first_message_id,
            )
        # One transaction per list: a kill in the middle must not leave half a batch to be receipted again.
        self.journal.mark_many([item[0] for item in items], "done")
        self.journal.mark_many(batch.repeats, "done", "repeat")

    def flush_all(self) -> None:
        for chat_id in list(self._batches):
            self.flush(chat_id)

    def cancel_all(self) -> None:
        """Drop pending batches without receipts (the crash path: they are redone after restart)."""
        for batch in self._batches.values():
            if batch.timer is not None:
                batch.timer.cancel()
        self._batches.clear()
