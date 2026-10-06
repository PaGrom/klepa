"""Core's own outbound messages: receipts and fixed replies (stage 1).

Simplified delivery: PENDING → SENDING → CONFIRMED | RETRY_WAIT | UNKNOWN | FAILED.
UNKNOWN is never retried automatically. The fenced state machine arrives in stage 4 (docs/architecture.md: Delivery).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..db import transaction
from ..events import EventLog, utc_now_iso
from ..telegram.client import Ambiguous, BadRequest, BotApi, Conflict, NotSent, TooManyRequests, Unauthorized

MAX_ATTEMPTS = 20


def backoff_seconds(attempts: int) -> float:
    return float(min(300, 2 ** min(attempts, 9)))


def tell_failed_original(outbox: Outbox, text: str, key: str, chat_id: int, payload: dict[str, Any]) -> None:
    """An original that will not go out: the person who asked for it hears so, once, in Core's words ({name})."""
    outbox.enqueue_text(f"failed:{key}", chat_id, text.format(name=payload["name"]))


class Outbox:
    def __init__(
        self,
        conn: sqlite3.Connection,
        api: BotApi | None,
        events: EventLog,
        clock: Callable[[], float] = time.time,
        *,
        bot: str = "family",
        on_failed: Callable[[str, int, dict[str, Any]], object] | None = None,
    ) -> None:
        self.conn = conn
        self.api = api
        self.events = events
        self.clock = clock
        self.bot = bot
        self.on_failed = on_failed  # an original that will not go out: the person who asked for it hears so
        self._wake = asyncio.Event()

    def enqueue_text(
        self, key: str, chat_id: int, text: str, reply_to: int | None = None, reply_markup: dict[str, Any] | None = None
    ) -> bool:
        now = utc_now_iso()
        payload: dict[str, Any] = {"text": text, "reply_to": reply_to}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO outbound(idempotency_key, origin, bot, method, chat_id, payload, state,
                   created_at, updated_at)
               VALUES (?, 'core', ?, 'sendMessage', ?, ?, 'PENDING', ?, ?)""",
            (key, self.bot, chat_id, json.dumps(payload, ensure_ascii=False), now, now),
        )
        self._wake.set()
        return cursor.rowcount == 1

    def enqueue_document(self, key: str, chat_id: int, document: dict[str, Any], reply_to: int | None = None) -> bool:
        """A stored original to send: its path, sha256, size, MIME and the name it came with (spec 6.5)."""
        now = utc_now_iso()
        payload = document | {"reply_to": reply_to}
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO outbound(idempotency_key, origin, bot, method, chat_id, payload, state,
                   created_at, updated_at)
               VALUES (?, 'core', ?, 'sendDocument', ?, ?, 'PENDING', ?, ?)""",
            (key, self.bot, chat_id, json.dumps(payload, ensure_ascii=False), now, now),
        )
        self._wake.set()
        return cursor.rowcount == 1

    def message_id(self, key: str) -> int | None:
        """Telegram's message id of a confirmed send, by idempotency key."""
        row = self.conn.execute("SELECT telegram_message_id FROM outbound WHERE idempotency_key=?", (key,)).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def recover(self) -> int:
        """After a crash, a send that may have left becomes UNKNOWN (never resent automatically)."""
        return self.conn.execute(
            "UPDATE outbound SET state='UNKNOWN', updated_at=? WHERE state='SENDING' AND bot=?",
            (utc_now_iso(), self.bot),
        ).rowcount

    def _set(self, row_id: int, state: str, **fields: Any) -> None:
        columns = "".join(f", {name}=?" for name in fields)
        self.conn.execute(
            f"UPDATE outbound SET state=?, updated_at=?{columns} WHERE id=?",
            (state, utc_now_iso(), *fields.values(), row_id),
        )

    async def send_due(self) -> int:
        assert self.api is not None
        rows = self.conn.execute(
            """SELECT * FROM outbound WHERE origin='core' AND bot=? AND state IN ('PENDING','RETRY_WAIT')
               AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT 20""",
            (self.bot, self.clock()),
        ).fetchall()
        confirmed = 0
        for row in rows:
            with transaction(self.conn):
                claimed = self.conn.execute(
                    "UPDATE outbound SET state='SENDING', attempts=attempts+1, updated_at=? "
                    "WHERE id=? AND state IN ('PENDING','RETRY_WAIT')",
                    (utc_now_iso(), row["id"]),
                ).rowcount
            if not claimed:
                continue
            payload = json.loads(row["payload"])
            attempts = row["attempts"] + 1
            try:
                if row["method"] == "sendDocument":
                    result = await self._send_document(row["chat_id"], payload)
                else:
                    result = await self.api.send_message(
                        row["chat_id"], payload["text"], payload.get("reply_to"), payload.get("reply_markup")
                    )
            except (NotSent, Unauthorized) as exc:
                state = "FAILED" if attempts >= MAX_ATTEMPTS else "RETRY_WAIT"
                self._set(
                    row["id"],
                    state,
                    next_attempt_at=self.clock() + backoff_seconds(attempts),
                    last_error=type(exc).__name__,
                )
                if state == "FAILED":
                    self._told(row, payload)
            except TooManyRequests as exc:
                self._set(
                    row["id"],
                    "RETRY_WAIT",
                    next_attempt_at=self.clock() + (exc.retry_after or 1.0),
                    last_error="TooManyRequests",
                )
            except (BadRequest, Conflict) as exc:
                self._set(row["id"], "FAILED", last_error=str(exc.code))
                self.events.log("outbound_failed", {"outbound_id": row["id"], "code": exc.code})
                self._told(row, payload)
            except Ambiguous as exc:
                self._set(row["id"], "UNKNOWN", last_error=type(exc).__name__)
                self.events.log("outbound_unknown", {"outbound_id": row["id"]})
            except Exception as exc:  # a request that could not even be built: this send fails, Core goes on
                self._set(row["id"], "FAILED", last_error=type(exc).__name__)
                self.events.log("outbound_failed", {"outbound_id": row["id"], "error": type(exc).__name__})
                self._told(row, payload)
            else:
                self._set(row["id"], "CONFIRMED", telegram_message_id=int(result["message_id"]))
                if self.bot == "service":  # every send of the service bot is an event of its own (spec §8)
                    self.events.log("service_sent", {"outbound_id": row["id"], "key": row["idempotency_key"]})
                confirmed += 1
        return confirmed

    def _told(self, row: sqlite3.Row, payload: dict[str, Any]) -> None:
        if row["method"] == "sendDocument" and self.on_failed is not None:
            self.on_failed(row["idempotency_key"], int(row["chat_id"]), payload)

    async def _send_document(self, chat_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Read the file once, check it is the stored original, and send exactly those bytes (spec 6.5)."""
        assert self.api is not None
        try:
            data = Path(payload["path"]).read_bytes()
        except OSError as exc:
            raise BadRequest("sendDocument", 400, f"the original is unreadable: {type(exc).__name__}") from None
        if len(data) != payload["size"] or hashlib.sha256(data).hexdigest() != payload["sha256"]:
            raise BadRequest("sendDocument", 400, "the file is not the stored original")
        return await self.api.send_document(chat_id, data, payload["name"], payload["mime"], payload.get("reply_to"))

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.send_due()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
