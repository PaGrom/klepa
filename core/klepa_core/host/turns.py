"""Turns of the host (spec 4.5): a turn starts only when it answers a message the host was given.

A registration pairs a runId with one issued message. The host may start a turn again under the same runId (a
continuation, or recovery after a restart); that is allowed only while the paired message still has no answer.
The report that before_prompt_build ran is kept per run, and a turn without it is refused.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..db import transaction
from .queue import HostQueue

TURN_TTL_SECONDS = 600.0  # a registration is short-lived (spec 4.5)
KEEP_RUNS_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class Turn:
    registered: bool
    reason: str  # "registered" or "repeated", else why the turn was refused
    host_message_id: int | None = None
    probe: bool = False


def session_of(session_key: object, sender_id: int) -> bool:
    """agent:<agent>:telegram:direct:<peer> (spike report, point 14), where the peer is the sender."""
    parts = session_key.split(":") if isinstance(session_key, str) else []
    return (
        len(parts) == 5 and parts[0] == "agent" and parts[2:4] == ["telegram", "direct"] and parts[4] == str(sender_id)
    )


class TurnRegistry:
    def __init__(self, conn: sqlite3.Connection, queue: HostQueue, *, clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.queue = queue
        self.clock = clock
        conn.execute(
            "DELETE FROM host_run WHERE COALESCE(registered_at, prompt_built_at, 0) < ?",
            (clock() - KEEP_RUNS_SECONDS,),
        )

    def prompt_built(self, run_id: str, boot_id: str, chat_id: int | None, sender_id: int | None) -> None:
        self.conn.execute(
            "INSERT INTO host_run(run_id, boot_id, chat_id, sender_id, prompt_built_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET boot_id=excluded.boot_id, prompt_built_at=excluded.prompt_built_at",
            (run_id, boot_id, chat_id, sender_id, self.clock()),
        )

    def start(self, run_id: str, boot_id: str, chat_id: int | None, sender_id: int | None, session_key: object) -> Turn:
        if sender_id is None:
            return Turn(False, "no_sender")
        if chat_id != sender_id:
            return Turn(False, "not_private")
        if not session_of(session_key, sender_id):
            return Turn(False, "session")
        now = self.clock()
        with transaction(self.conn):
            run = self.conn.execute("SELECT * FROM host_run WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["prompt_built_at"] is None:
                return Turn(False, "no_prompt_build")
            if run["chat_id"] != chat_id or run["sender_id"] != sender_id:
                return Turn(False, "run_mismatch")
            if run["host_message_id"] is not None:
                message = self.conn.execute(
                    "SELECT kind, answered_at FROM host_message WHERE id=?", (run["host_message_id"],)
                ).fetchone()
                if message is None or message["answered_at"] is not None:
                    return Turn(False, "answered")
                self.conn.execute(
                    "UPDATE host_run SET boot_id=?, expires_at=? WHERE run_id=?",
                    (boot_id, now + TURN_TTL_SECONDS, run_id),
                )
                return Turn(True, "repeated", int(run["host_message_id"]), message["kind"] == "probe")
            busy = {
                int(row[0])
                for row in self.conn.execute(
                    "SELECT host_message_id FROM host_run WHERE host_message_id IS NOT NULL AND expires_at > ?",
                    (now,),
                )
            }
            message = self.queue.oldest_unanswered(chat_id, sender_id, exclude=busy)
            if message is None:
                return Turn(False, "no_issued_message")
            self.conn.execute(
                "UPDATE host_run SET boot_id=?, host_message_id=?, registered_at=?, expires_at=? WHERE run_id=?",
                (boot_id, message["id"], now, now + TURN_TTL_SECONDS, run_id),
            )
            return Turn(True, "registered", int(message["id"]), message["kind"] == "probe")

    def active_message(self, chat_id: int) -> int | None:
        """The issued message of the newest live turn in a chat: the host's writing there answers it."""
        row = self.conn.execute(
            "SELECT host_message_id FROM host_run WHERE chat_id=? AND host_message_id IS NOT NULL AND expires_at > ? "
            "ORDER BY registered_at DESC LIMIT 1",
            (chat_id, self.clock()),
        ).fetchone()
        return None if row is None else int(row[0])
