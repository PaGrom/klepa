"""Accept-and-hold for the host's messages (spec 4.6, D22).

Every sendMessage of the host is written to `outbound` with F_FULLFSYNC before the host hears "sent", under a
message id from a range of Core's own. A worker releases the messages chat by chat, in order, while the adapter's
heartbeat is fresh. A message that has not gone out within ten minutes is dropped together with the rest of its
chat's queue, and the person gets Core's apology instead; so does a message Telegram refuses, and the owner gets
an alert. The host never sees a Telegram error that it would answer by writing its own English text to the
person. Held text leaves core.db as soon as its send ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from ..alerts import Alerts
from ..db import transaction
from ..events import EventLog, utc_now_iso
from ..gatekeeper.outbox import MAX_ATTEMPTS, Outbox, backoff_seconds
from ..locale import Locale
from ..telegram.client import Ambiguous, BadRequest, BotApi, Conflict, NotSent, TooManyRequests, Unauthorized
from .queue import HostQueue
from .supervisor import HostTiming, Supervisor
from .turns import TurnRegistry

HELD_ID_BASE = 2**41  # message ids Core gives the host; Telegram's own are far below
MAX_WAITING_PER_CHAT = 50  # more unsent messages in one chat than any answer needs
_FINAL = "{}"  # a finished send keeps no text
_WAITING = ("PENDING", "SENDING", "RETRY_WAIT")


class HostOutbox:
    def __init__(
        self,
        conn: sqlite3.Connection,
        api: BotApi,
        supervisor: Supervisor,
        queue: HostQueue,
        turns: TurnRegistry,
        notices: Outbox,
        events: EventLog,
        locale: Locale,
        *,
        clock: Callable[[], float] = time.time,
        timing: HostTiming | None = None,
        alerts: Alerts | None = None,
    ) -> None:
        self.conn = conn
        self.api = api
        self.supervisor = supervisor
        self.queue = queue
        self.turns = turns
        self.notices = notices
        self.events = events
        self.locale = locale
        self.clock = clock
        self.timing = timing or HostTiming()
        self.alerts = alerts
        self._wake = asyncio.Event()
        # Sends left SENDING by a crash became UNKNOWN in Outbox.recover; none of them keeps its text.
        conn.execute(
            "UPDATE outbound SET payload=? WHERE origin='host' AND state IN ('CONFIRMED','FAILED','UNKNOWN') "
            "AND payload != ?",
            (_FINAL, _FINAL),
        )

    def accept(self, chat_id: int, html: str, reply_to: int | None) -> int:
        """Take the host's message into the durable queue and answer with Core's message id for it."""
        now = utc_now_iso()
        payload = {"text": html, "reply_to": reply_to, "accepted_at": self.clock()}
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO outbound(idempotency_key, origin, bot, method, chat_id, payload, state, created_at, "
                "updated_at) VALUES (?, 'host', 'family', 'sendMessage', ?, ?, 'PENDING', ?, ?)",
                (f"host:{secrets.token_hex(16)}", chat_id, json.dumps(payload, ensure_ascii=False), now, now),
            )
            self.queue.mark_answered(chat_id, prefer=self.turns.active_message(chat_id))
        assert cursor.lastrowid is not None
        if not self.supervisor.may_release():  # the trace of holding (scenario 38)
            self.events.log("host_send_held", {"chat_id": chat_id, "outbound_id": cursor.lastrowid})
        self._wake.set()
        return HELD_ID_BASE + cursor.lastrowid

    def waiting(self, chat_id: int) -> int:
        """How many of the host's messages to a chat have not gone out yet."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM outbound WHERE origin='host' AND chat_id=? AND state IN (?, ?, ?)",
            (chat_id, *_WAITING),
        ).fetchone()
        return int(row[0])

    def owns(self, chat_id: int, message_id: int) -> bool:
        """Whether a message id is one Core gave the host for its own message in this chat."""
        if message_id < HELD_ID_BASE:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM outbound WHERE id=? AND origin='host' AND chat_id=?", (message_id - HELD_ID_BASE, chat_id)
        ).fetchone()
        return row is not None

    def _telegram_id(self, message_id: int | None) -> int | None:
        """An issued message keeps its Telegram id; one of the host's own maps to what Telegram gave it, if sent."""
        if message_id is None or message_id < HELD_ID_BASE:
            return message_id
        row = self.conn.execute(
            "SELECT telegram_message_id FROM outbound WHERE id=? AND origin='host'", (message_id - HELD_ID_BASE,)
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    async def release_due(self) -> int:
        rows = self.conn.execute(
            "SELECT * FROM outbound WHERE origin='host' AND state IN (?, ?, ?) ORDER BY id", _WAITING
        ).fetchall()
        heads: dict[int, sqlite3.Row] = {}
        for row in rows:
            heads.setdefault(int(row["chat_id"]), row)  # one chat strictly in order
        sent = 0
        now = self.clock()
        for chat_id, row in heads.items():
            if row["state"] == "SENDING":
                continue
            try:
                payload = json.loads(row["payload"])
                accepted_at = float(payload["accepted_at"])
                if not isinstance(payload["text"], str):
                    raise TypeError("text")
            except (ValueError, KeyError, TypeError):  # never stuck on a row that cannot be sent
                self._drop(chat_id, "unreadable")
                continue
            if now - accepted_at >= self.timing.drop_after:  # a retry or a sleep, all the same
                self._drop(chat_id, "held_expired")
                continue
            if not self.supervisor.may_release():
                continue
            if row["state"] == "RETRY_WAIT" and float(row["next_attempt_at"] or 0.0) > now:
                continue
            sent += await self._send(row, payload)
        return sent

    def _drop(self, chat_id: int, reason: str) -> None:
        """The chat's unsent messages go, and the person is asked to repeat (spec 4.6)."""
        with transaction(self.conn):
            dropped = self.conn.execute(
                "UPDATE outbound SET state='FAILED', last_error=?, payload=?, updated_at=? "
                "WHERE origin='host' AND chat_id=? AND state IN ('PENDING','RETRY_WAIT')",
                (reason, _FINAL, utc_now_iso(), chat_id),
            ).rowcount
            self._apologize(chat_id)
        self.events.log("host_held_dropped", {"chat_id": chat_id, "count": dropped, "reason": reason})

    def _apologize(self, chat_id: int) -> None:
        if self.queue.claim_notice(chat_id, "dropped"):
            self.notices.enqueue_text(
                f"held_dropped:{chat_id}:{self.clock():.3f}", chat_id, self.locale.text("held_dropped")
            )

    def _set(self, row_id: int, state: str, **fields: Any) -> None:
        if state in ("CONFIRMED", "FAILED", "UNKNOWN"):
            fields["payload"] = _FINAL
        columns = "".join(f", {name}=?" for name in fields)
        self.conn.execute(
            f"UPDATE outbound SET state=?, updated_at=?{columns} WHERE id=?",
            (state, utc_now_iso(), *fields.values(), row_id),
        )

    async def _send(self, row: sqlite3.Row, payload: dict[str, Any]) -> int:
        with transaction(self.conn):
            claimed = self.conn.execute(
                "UPDATE outbound SET state='SENDING', attempts=attempts+1, updated_at=? "
                "WHERE id=? AND state IN ('PENDING','RETRY_WAIT')",
                (utc_now_iso(), row["id"]),
            ).rowcount
        if not claimed:
            return 0
        attempts = int(row["attempts"]) + 1
        try:
            result = await self.api.send_message(
                int(row["chat_id"]), payload["text"], self._telegram_id(payload.get("reply_to")), parse_mode="HTML"
            )
        except (NotSent, Unauthorized) as exc:
            if attempts >= MAX_ATTEMPTS:
                self._refused(row, type(exc).__name__)
            else:
                self._set(
                    row["id"],
                    "RETRY_WAIT",
                    next_attempt_at=self.clock() + backoff_seconds(attempts),
                    last_error=type(exc).__name__,
                )
            return 0
        except TooManyRequests as exc:
            self._set(row["id"], "RETRY_WAIT", next_attempt_at=self.clock() + (exc.retry_after or 1.0))
            return 0
        except (BadRequest, Conflict) as exc:
            self._refused(row, str(exc.code))
            return 0
        except Ambiguous as exc:
            self._set(row["id"], "UNKNOWN", last_error=type(exc).__name__)
            self.events.log("outbound_unknown", {"outbound_id": row["id"]})
            return 0
        except Exception as exc:  # whatever happened, the row must not stay SENDING and block its chat
            self._set(row["id"], "UNKNOWN", last_error=type(exc).__name__)
            self.events.log("host_send_error", {"outbound_id": row["id"], "error": type(exc).__name__})
            return 0
        self._set(row["id"], "CONFIRMED", telegram_message_id=int(result["message_id"]))
        return 1

    def _refused(self, row: sqlite3.Row, error: str) -> None:
        """Telegram will not take it: the person is asked to repeat, and the owner hears of it."""
        with transaction(self.conn):
            self._set(row["id"], "FAILED", last_error=error)
            self._apologize(int(row["chat_id"]))
        self.events.log("host_send_failed", {"outbound_id": row["id"], "error": error})
        if self.alerts is not None:
            self.alerts.raise_("host_send_failed", error=error)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.release_due()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=0.2)
