"""The service bot: the owner's narrow channel for alerts, the status line and buttons
(docs/architecture.md: Service bot)."""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .aio import sleep_or_stop, until_stopped
from .config import Config
from .db import owner_service_chat, transaction
from .events import EventLog, utc_now_iso
from .gatekeeper.outbox import Outbox
from .health import Health, Probe
from .host.supervisor import GatewayState, Supervisor
from .telegram.client import (
    Ambiguous,
    BadRequest,
    BotApi,
    Conflict,
    NotSent,
    TelegramError,
    TooManyRequests,
    Unauthorized,
)

BIND_CODE_BYTES = 16  # 128 bits


def new_bind_code() -> str:
    return secrets.token_urlsafe(BIND_CODE_BYTES)


async def bind_owner_chat(
    cfg: Config,
    api: BotApi,
    conn: sqlite3.Connection,
    code: str,
    confirm: Callable[[str], bool],
    *,
    deadline_seconds: float = 600.0,
    poll_timeout: int = 30,
) -> int | None:
    """Wait for '/start <code>' from the owner's own account in a private chat, confirm in the terminal and store
    the chat. The code alone is not enough: it must come from the configured owner."""
    owner = next(member for member in cfg.members if member.role == "owner")
    expected = f"/start {code}"
    offset: int | None = None
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        for update in await api.get_updates(offset, poll_timeout):
            offset = int(update["update_id"]) + 1
            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            sender = msg.get("from") or {}
            if (msg.get("text") or "").strip() != expected:
                continue
            from_owner = sender.get("id") == owner.telegram_id and chat.get("id") == owner.telegram_id
            if chat.get("type") != "private" or not from_owner:
                continue
            name = sender.get("first_name", "?")
            question = f"Bind the service bot to the chat of {name} (Telegram id {sender['id']})? [y/N] "
            if not confirm(question):
                return None
            with transaction(conn):
                conn.execute(
                    "INSERT INTO service_binding(person_id, chat_id, bound_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(person_id) DO UPDATE SET chat_id=excluded.chat_id, bound_at=excluded.bound_at",
                    (owner.person_id, int(chat["id"]), utc_now_iso()),
                )
            await api.get_updates(offset, 0)  # acknowledge, so Core never sees the code
            return int(chat["id"])
    return None


BUTTON_TTL_SECONDS = 7 * 24 * 3600
REJECTED_LOG_SECONDS = 3600.0
_REJECTED_SENDERS_KEPT = 1000


class ServiceBot:
    def __init__(
        self,
        cfg: Config,
        api: BotApi,
        conn: sqlite3.Connection,
        outbox: Outbox,
        health: Health,
        events: EventLog,
        *,
        probe: Callable[[], Awaitable[Probe]],
        clock: Callable[[], float] = time.time,
        supervisor: Supervisor | None = None,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.conn = conn
        self.outbox = outbox
        self.health = health
        self.events = events
        self.probe = probe
        self.clock = clock
        self.supervisor = supervisor  # the Pause and Resume buttons, when a host is set up
        self.owner = next(member for member in cfg.members if member.role == "owner")
        self._rejected_at: dict[int | None, float] = {}

    def _is_owner(self, chat: dict[str, Any], sender: dict[str, Any]) -> bool:
        bound = owner_service_chat(self.conn)
        return (
            bound is not None
            and chat.get("type") == "private"
            and chat.get("id") == bound
            and sender.get("id") == self.owner.telegram_id
        )

    def _rejected(self, sender: dict[str, Any]) -> None:
        """Log a rejected update at most once an hour per sender, so strangers cannot flood the event log."""
        from_id = sender.get("id") if isinstance(sender.get("id"), int) else None
        now = self.clock()
        if now - self._rejected_at.get(from_id, float("-inf")) < REJECTED_LOG_SECONDS:
            return
        if len(self._rejected_at) >= _REJECTED_SENDERS_KEPT:
            self._rejected_at.clear()
        self._rejected_at[from_id] = now
        self.events.log("service_update_rejected", {"from_id": from_id})

    def _actions(self) -> list[str]:
        """Status, and with a host set up Pause, or Resume while the host is paused or Core stopped it: a stopped
        host stays down until Resume."""
        if self.supervisor is None:
            return ["status"]
        held = self.supervisor.paused or self.supervisor.state is GatewayState.STOPPED
        return ["status", "resume" if held else "pause"]

    async def send_status(self, key: str) -> bool:
        """Queue the status line with fresh one-time buttons. Idempotent by key."""
        chat_id = owner_service_chat(self.conn)
        if chat_id is None:
            return False
        text, _ = self.health.line(await self.probe())
        return self._send_with_buttons(key, chat_id, text)

    def _send_with_buttons(self, key: str, chat_id: int, text: str, reply_to: int | None = None) -> bool:
        buttons = [(secrets.token_hex(16), action) for action in self._actions()]  # 128 bits each
        row = [
            {"text": self.cfg.locale.service_text(f"{action}_button"), "callback_data": action_id}
            for action_id, action in buttons
        ]
        with transaction(self.conn):
            if not self.outbox.enqueue_text(key, chat_id, text, reply_to, reply_markup={"inline_keyboard": [row]}):
                return False
            for action_id, action in buttons:
                self.conn.execute(
                    "INSERT INTO button_action(id, action, chat_id, outbound_key, expires_at) VALUES (?, ?, ?, ?, ?)",
                    (action_id, action, chat_id, key, self.clock() + BUTTON_TTL_SECONDS),
                )
        return True

    async def send_daily_line(self, day: str) -> None:
        await self.send_status(f"daily:{day}")

    async def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._press(update["callback_query"])
        elif "message" in update:
            await self._message(update["message"])

    async def _message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if not self._is_owner(chat, sender):
            self._rejected(sender)
            return
        words = (msg.get("text") or "").split()
        if words[:1] in (["/status"], ["/start"]):
            await self.send_status(f"status:msg:{msg['message_id']}")
            return
        # The buttons come with the reply, so a person who types "pause" finds Pause one tap away.
        self._send_with_buttons(
            f"service_reply:{msg['message_id']}",
            int(chat["id"]),
            self.cfg.locale.service_text("not_yet"),
            reply_to=int(msg["message_id"]),
        )

    async def _press(self, callback: dict[str, Any]) -> None:
        msg = callback.get("message") or {}
        chat = msg.get("chat") or {}
        sender = callback.get("from") or {}
        action_id = str(callback.get("data") or "")
        valid = False
        action = "status"
        if self._is_owner(chat, sender):
            row = self.conn.execute("SELECT * FROM button_action WHERE id=?", (action_id,)).fetchone()
            if (
                row is not None
                and row["used_at"] is None
                and float(row["expires_at"]) > self.clock()
                and row["chat_id"] == chat.get("id")
                and self.outbox.message_id(str(row["outbound_key"])) == msg.get("message_id")
            ):
                claimed = self.conn.execute(
                    "UPDATE button_action SET used_at=? WHERE id=? AND used_at IS NULL", (utc_now_iso(), action_id)
                )
                valid = claimed.rowcount == 1
                action = str(row["action"])
        else:
            self._rejected(sender)
        if valid:
            if self.supervisor is not None and action == "pause":
                self.supervisor.pause()
            elif self.supervisor is not None and action == "resume":
                self.supervisor.resume()
            await self.send_status(f"status:press:{action_id}")
        try:
            await self.api.answer_callback_query(
                str(callback["id"]), None if valid else self.cfg.locale.service_text("button_expired")
            )
        except (TelegramError, NotSent, Ambiguous) as exc:
            self.events.log("service_answer_failed", {"error": type(exc).__name__})

    async def poll_forever(self, stop: asyncio.Event) -> None:
        offset: int | None = None
        backoff = 1.0
        while not stop.is_set():
            try:
                updates = await until_stopped(stop, self.api.get_updates(offset, self.cfg.poll_timeout_seconds))
                backoff = 1.0
            except (Unauthorized, Conflict) as exc:
                self.events.log("service_channel_error", {"error": type(exc).__name__})
                await sleep_or_stop(stop, 60)
                continue
            except TooManyRequests as exc:
                await sleep_or_stop(stop, exc.retry_after or 1.0)
                continue
            except (NotSent, Ambiguous, BadRequest) as exc:
                self.events.log("service_poll_error", {"error": type(exc).__name__})
                await sleep_or_stop(stop, backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            for update in updates or []:
                offset = int(update["update_id"]) + 1  # acknowledged with the next poll, handled or not
                try:
                    # Handling may wait on the documents folder; stopping Core must not wait with it.
                    await until_stopped(stop, self.handle(update))
                except Exception as exc:
                    # One bad update must neither stop the bot nor come back after every restart.
                    self.events.log("service_update_failed", {"update_id": offset - 1, "error": type(exc).__name__})
