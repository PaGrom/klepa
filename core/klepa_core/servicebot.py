"""The service bot: the owner's narrow channel for alerts, the status line and buttons
(docs/architecture.md: Service bot)."""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Callable

from .config import Config
from .db import transaction
from .events import utc_now_iso
from .telegram.client import BotApi

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
