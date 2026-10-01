"""What the host receives (docs/architecture.md: Host interface; spec 4.2, 4.6, 6.1).

Members' text messages wait here until the host may have them. The host takes them with getUpdates, under
Core's own update numbering, each chat strictly in order. A served message is built from a fixed list of fields
when the host polls, from the inbound journal: message text never lands in core.db, which snapshots copy.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable, Collection
from typing import Any

from ..config import Member
from ..db import transaction
from ..gatekeeper.outbox import Outbox
from ..journal import InboundJournal

MAX_UPDATES = 100
ANSWER_WINDOW_SECONDS = 600.0  # the host may go on writing to a chat this long after an answer (spec 4.2)
NOTICE_EVERY_SECONDS = 600.0  # a fixed notice goes to a chat at most once per ten minutes (spec 4.6)
PROBE_NAME = "Klepa probe"
PROBE_TEXT = "Klepa start probe"
FORWARDED_MARK = "[forwarded message]"

MayServe = Callable[[int, str], bool]


class HostQueue:
    def __init__(
        self,
        conn: sqlite3.Connection,
        journal: InboundJournal,
        members: dict[int, Member],
        probe_peer: int,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.conn = conn
        self.journal = journal
        self.members = members
        self.probe_peer = probe_peer
        self.clock = clock
        self.changed = asyncio.Event()

    def wake(self) -> None:
        """Something may have become servable: a waiting getUpdates looks again."""
        self.changed.set()

    def enqueue(
        self, update_id: int, chat_id: int, message_id: int, sender_id: int, date: int, *, forwarded: bool = False
    ) -> bool:
        """A member's text for the host. The same message under a new update id is kept once."""
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO host_message(source_update_id, kind, chat_id, message_id, sender_id, date, "
            "forwarded, created_at) VALUES (?, 'text', ?, ?, ?, ?, ?, ?)",
            (update_id, chat_id, message_id, sender_id, date, int(forwarded), self.clock()),
        )
        self.wake()
        return cursor.rowcount == 1

    def add_probe(self) -> int:
        """The synthetic message of the live probe (spec 4.6), in the probe peer's own chat. Every earlier probe is
        retired first.

        Its message id grows with the clock as well, so the host, which remembers chat_id:message_id pairs on
        disk, never takes a new probe for a repeat, even after core.db was replaced.
        """
        now = self.clock()
        with transaction(self.conn):
            # A probe that a stopped Core never retired must not take this probe's turn.
            self.conn.execute(
                "UPDATE host_message SET answered_at=? WHERE kind='probe' AND answered_at IS NULL", (now,)
            )
            last = self.conn.execute(
                "SELECT COALESCE(MAX(message_id), 0) FROM host_message WHERE chat_id=?", (self.probe_peer,)
            ).fetchone()[0]
            cursor = self.conn.execute(
                "INSERT INTO host_message(source_update_id, kind, chat_id, message_id, sender_id, date, created_at) "
                "VALUES (NULL, 'probe', ?, ?, ?, ?, ?)",
                (self.probe_peer, max(int(last) + 1, int(now)), self.probe_peer, int(now), now),
            )
        self.wake()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def retire(self, host_message_id: int) -> None:
        """A probe that is over is never served later."""
        self.conn.execute(
            "UPDATE host_message SET answered_at=COALESCE(answered_at, ?) WHERE id=? AND kind='probe'",
            (self.clock(), host_message_id),
        )

    def serve(self, offset: int | None, limit: int, may_serve: MayServe) -> list[dict[str, Any]]:
        """getUpdates: acknowledge what lies below `offset`, repeat what the host has not acknowledged yet, then
        issue waiting messages, oldest first. Only what `may_serve` allows is served, repeats included."""
        now = self.clock()
        limit = max(1, min(limit, MAX_UPDATES))
        updates: list[dict[str, Any]] = []
        with transaction(self.conn):
            if offset is not None and offset > 0:
                self.conn.execute(
                    "UPDATE host_update SET acked_at=? WHERE update_id < ? AND acked_at IS NULL", (now, offset)
                )
            issued = self.conn.execute(
                "SELECT u.update_id, m.* FROM host_update u JOIN host_message m ON m.id = u.host_message_id "
                "WHERE u.acked_at IS NULL AND u.update_id >= ? ORDER BY u.update_id LIMIT ?",
                (max(offset or 0, 0), limit),
            ).fetchall()
            for row in issued:
                if not may_serve(int(row["id"]), str(row["kind"])):
                    continue  # a gateway that has not passed the start gate gets nothing, not even repeats
                message = self._message(row)
                if message is not None:
                    updates.append({"update_id": int(row["update_id"]), "message": message})
            for row in self._waiting():
                if len(updates) >= limit:
                    break
                if not may_serve(int(row["id"]), str(row["kind"])):
                    continue
                message = self._message(row)
                if message is None:  # its journal entry is gone: never leave the chat waiting on it
                    self.conn.execute("UPDATE host_message SET answered_at=? WHERE id=?", (now, row["id"]))
                    continue
                update_id = self._next_update_id(now)
                self.conn.execute(
                    "INSERT INTO host_update(update_id, host_message_id, issued_at) VALUES (?, ?, ?)",
                    (update_id, row["id"], now),
                )
                updates.append({"update_id": update_id, "message": message})
        return updates

    def _waiting(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT m.* FROM host_message m LEFT JOIN host_update u ON u.host_message_id = m.id "
            "WHERE u.update_id IS NULL AND m.answered_at IS NULL ORDER BY m.id"
        ).fetchall()

    def _next_update_id(self, now: float) -> int:
        last = self.conn.execute("SELECT COALESCE(MAX(update_id), 0) FROM host_update").fetchone()[0]
        return max(int(last) + 1, int(now))

    def _message(self, row: sqlite3.Row) -> dict[str, Any] | None:
        """The served message: message_id, from, chat, date and text, nothing else (spec 4.5)."""
        sender_id = int(row["sender_id"])
        if row["kind"] == "probe":
            name, text = PROBE_NAME, PROBE_TEXT
        else:
            update = self.journal.get(int(row["source_update_id"])) or {}
            original = (update.get("message") or {}).get("text")
            member = self.members.get(sender_id)
            if not isinstance(original, str) or member is None:
                return None
            name = member.name
            text = f"{FORWARDED_MARK}\n{original}" if row["forwarded"] else original
        person = {"id": sender_id, "is_bot": False, "first_name": name}
        return {
            "message_id": int(row["message_id"]),
            "from": person,
            "chat": {"id": int(row["chat_id"]), "type": "private", "first_name": name},
            "date": int(row["date"]),
            "text": text,
        }

    # ---- conversations -------------------------------------------------------------------------------------------
    def conversation_open(self, chat_id: int) -> bool:
        """The host may write to a chat that has an issued message without an answer, or whose last answer came
        less than ten minutes ago (spec 4.2)."""
        row = self.conn.execute(
            "SELECT 1 FROM host_message m JOIN host_update u ON u.host_message_id = m.id "
            "WHERE m.chat_id=? AND (m.answered_at IS NULL OR m.answered_at >= ?) LIMIT 1",
            (chat_id, self.clock() - ANSWER_WINDOW_SECONDS),
        ).fetchone()
        return row is not None

    def issued_in_chat(self, chat_id: int, message_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM host_message m JOIN host_update u ON u.host_message_id = m.id "
            "WHERE m.chat_id=? AND m.message_id=?",
            (chat_id, message_id),
        ).fetchone()
        return row is not None

    def oldest_unanswered(
        self, chat_id: int, sender_id: int | None = None, exclude: Collection[int] = ()
    ) -> sqlite3.Row | None:
        """The oldest issued message of a chat that has no answer yet."""
        rows: list[sqlite3.Row] = self.conn.execute(
            "SELECT m.* FROM host_message m JOIN host_update u ON u.host_message_id = m.id "
            "WHERE m.chat_id=? AND m.answered_at IS NULL ORDER BY m.id",
            (chat_id,),
        ).fetchall()
        for row in rows:
            if (sender_id is None or row["sender_id"] == sender_id) and row["id"] not in exclude:
                return row
        return None

    def mark_answered(self, chat_id: int, prefer: int | None = None) -> int | None:
        """Record that the host wrote to a chat: the answer belongs to the message of the turn that wrote it, and
        without a turn (stage 1 answers in before_dispatch) to the oldest message still waiting for one."""
        now = self.clock()
        if prefer is not None:
            row = self.conn.execute(
                "SELECT id, answered_at FROM host_message WHERE id=? AND chat_id=?", (prefer, chat_id)
            ).fetchone()
            if row is not None:
                if row["answered_at"] is None:
                    self.conn.execute("UPDATE host_message SET answered_at=? WHERE id=?", (now, prefer))
                return prefer
        oldest = self.oldest_unanswered(chat_id)
        if oldest is None:
            return None
        self.conn.execute("UPDATE host_message SET answered_at=? WHERE id=?", (now, oldest["id"]))
        return int(oldest["id"])

    # ---- notices -------------------------------------------------------------------------------------------------
    def waiting_chats(self) -> list[int]:
        """Chats whose text waits for the host."""
        rows = self.conn.execute(
            "SELECT DISTINCT m.chat_id FROM host_message m LEFT JOIN host_update u ON u.host_message_id = m.id "
            "WHERE u.update_id IS NULL AND m.kind='text' AND m.answered_at IS NULL ORDER BY m.chat_id"
        ).fetchall()
        return [int(row[0]) for row in rows]

    def claim_notice(self, chat_id: int, kind: str) -> bool:
        """Whether a fixed notice of this kind may go to the chat now, at most once per ten minutes (spec 4.6).
        One statement, so it composes with the caller's transaction."""
        cursor = self.conn.execute(
            "INSERT INTO host_notice(chat_id, kind, sent_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, kind) DO UPDATE SET sent_at=excluded.sent_at "
            "WHERE excluded.sent_at - host_notice.sent_at >= ?",
            (chat_id, kind, self.clock(), NOTICE_EVERY_SECONDS),
        )
        return cursor.rowcount == 1

    def send_hold_replies(self, outbox: Outbox, text: str) -> int:
        """While the host is on hold, a chat with waiting text gets the fixed reply (spec 4.6)."""
        sent = 0
        for chat_id in self.waiting_chats():
            with transaction(self.conn):
                if self.claim_notice(chat_id, "hold"):
                    outbox.enqueue_text(f"hold:{chat_id}:{self.clock():.3f}", chat_id, text)
                    sent += 1
        return sent
