"""The background reader (stage 2c-2, spec 4.6 rev): photos and scanned pages read by the model, one at a time.

A reading is a message of its space's reader peer in that peer's own chat, which the gateway answers with a turn of
its own: Core's reader instruction, `view_page` and `save_text` only, for the record's owner (tools.py decides what
such a turn may do). The reading ends with its turn, never with its first saved part, so a long page saved in parts is
kept whole. Then Core resets the peer's session before the peer gets another reading: a reading never carries an
earlier one (proven live: test_core_can_reset_a_session_and_the_next_turn_carries_nothing_earlier). A peer whose
reset failed gets nothing until one succeeds, also after a restart.

People go first: nothing is issued while a person's turn is live, while the host is not RUNNING with a fresh heartbeat,
or while the provider has just said it is busy. Photos and first pages go before deeper pages, so a long scan never
holds back a later photo. An album item waits until its album is quiet: a late private caption may still move it, and
its reading must be its final space's. Attempts count when they reached the model; a busy provider backs a reading
off; one the gateway never starts counts apart, and after too many the reading is held and the owner told.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..alerts import Alerts
from ..db import transaction
from ..events import EventLog
from .queue import HostQueue

READ_SECONDS = 180.0  # a reading's turn, counted only while the host runs
SAVE_IDLE_SECONDS = 20.0  # saved, and its tools quiet this long: the turn is over even if no reply comes
COUNTED_ATTEMPTS = 3  # turns that reached the model and saved nothing
UNCOUNTED_ATTEMPTS = 5  # turns the gateway never started
BACKOFF_SECONDS = (60.0, 300.0, 1800.0)  # after the provider said it is busy
RESET_ALERT_AFTER = 5  # failed resets of one peer before the owner is told
PERSON_PAUSE_SECONDS = 60.0  # after a person's turn met a busy provider
TICK_SECONDS = 1.0
BACKFILL_SECONDS = 5.0  # how often the reader looks for what to read
BUSY = ("rate_limit", "provider", "timeout")


class Gate(Protocol):
    def may_release(self) -> bool: ...


Reset = Callable[[int], Awaitable[bool]]


@dataclass
class _Current:
    reading_id: int
    message_id: int
    peer: int
    running_seconds: float = 0.0
    reply: tuple[bool, str | None] | None = None


class Reader:
    def __init__(
        self,
        conn: sqlite3.Connection,
        queue: HostQueue,
        gate: Gate,
        reset: Reset,
        events: EventLog,
        *,
        alerts: Alerts | None = None,
        album_quiet: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.conn = conn
        self.queue = queue
        self.gate = gate
        self.reset = reset
        self.events = events
        self.alerts = alerts
        self.album_quiet = album_quiet
        self.clock = clock
        self.current: _Current | None = None
        self.paused_until = 0.0
        self._reset_after = 0.0  # a failed reset's back-off: not kept over a restart, which tries at once
        self._next_backfill = 0.0
        self.wake = asyncio.Event()

    # ---- the queue ---------------------------------------------------------------------------------------------------
    def queue_reading(self, evidence_id: str, page: int) -> None:
        row = self.conn.execute("SELECT space_id FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        if row is None:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO reading(evidence_id, page, space_id, state, queued_at) "
            "VALUES (?, ?, ?, 'queued', ?)",
            (evidence_id, page, row["space_id"], self.clock()),
        )
        self.wake.set()

    def backfill(self) -> int:
        """What to read, from what Core stored: a photo whose job drew its picture and has no text yet, and each
        scanned page of a PDF; also what came while no reader ran. Returns how many readings it queued."""
        readable = "('stored', 'processing', 'processed', 'failed')"
        kept = (  # a batch its sender cancelled is never read (the final review's I5)
            "AND NOT EXISTS (SELECT 1 FROM host_message_item i JOIN host_message m ON m.id = i.host_message_id "
            "WHERE i.evidence_id = e.id AND m.cancelled_at IS NOT NULL) "
        )
        rows = self.conn.execute(
            "SELECT j.evidence_id, 1 AS page, e.space_id FROM job j JOIN evidence e ON e.id = j.evidence_id "
            f"WHERE j.kind = 'image' AND j.state = 'done' AND e.state IN {readable} "
            "AND NOT EXISTS (SELECT 1 FROM evidence_text t WHERE t.evidence_id = j.evidence_id AND t.page = 1) "
            "AND NOT EXISTS (SELECT 1 FROM reading r WHERE r.evidence_id = j.evidence_id AND r.page = 1) "
            f"{kept}"
            "UNION ALL "
            "SELECT s.evidence_id, s.page, e.space_id FROM scan_page s JOIN evidence e ON e.id = s.evidence_id "
            f"WHERE e.state IN {readable} "
            "AND NOT EXISTS (SELECT 1 FROM reading r WHERE r.evidence_id = s.evidence_id AND r.page = s.page) "
            f"{kept}"
        ).fetchall()
        now = self.clock()
        self.conn.executemany(
            "INSERT OR IGNORE INTO reading(evidence_id, page, space_id, state, queued_at) "
            "VALUES (?, ?, ?, 'queued', ?)",
            [(row["evidence_id"], int(row["page"]), row["space_id"], now) for row in rows],
        )
        return len(rows)

    def recover(self) -> None:
        """At start: a reading a stopped Core left running is queued again, its message retired; a failed one gets
        another round."""
        for row in self.conn.execute("SELECT host_message_id FROM reading WHERE state = 'running'").fetchall():
            if row["host_message_id"] is not None:
                self.queue.retire(int(row["host_message_id"]))
        self.conn.execute(
            "UPDATE reading SET state = 'queued', host_message_id = NULL, attempts = 0, uncounted = 0, not_before = 0 "
            "WHERE state IN ('running', 'failed')"
        )

    # ---- what the adapter reports ------------------------------------------------------------------------------------
    def is_reading(self, host_message_id: int | None) -> bool:
        return self.current is not None and host_message_id == self.current.message_id

    def on_reply(self, host_message_id: int, ok: bool, kind: str | None) -> None:
        """The reading's turn replied: it reached the model, and is over."""
        if self.current is not None and host_message_id == self.current.message_id:
            self.current.reply = (ok, kind)
            self.wake.set()

    def on_person_error(self, kind: str | None) -> None:
        """A person's turn met a busy provider: readings wait, so they do not take the person's next turn's room."""
        if kind in BUSY:
            self.paused_until = max(self.paused_until, self.clock() + PERSON_PAUSE_SECONDS)

    def tick_running(self, seconds: float) -> None:
        """The reading's clock runs only while the host runs (a gateway restart or HOLD is no reading's fault)."""
        if self.current is not None and self.gate.may_release():
            self.current.running_seconds += seconds

    # ---- one step ----------------------------------------------------------------------------------------------------
    async def step(self) -> None:
        if self.clock() >= self._next_backfill:
            self.backfill()
            self._next_backfill = self.clock() + BACKFILL_SECONDS
        if self.current is not None:
            self._settle()
        if self.current is not None:
            return
        if not await self._resets():
            return
        if self._may_issue():
            self._issue()

    def _settle(self) -> None:
        current = self.current
        assert current is not None
        reading = self.conn.execute("SELECT * FROM reading WHERE id = ?", (current.reading_id,)).fetchone()
        if reading is None or reading["state"] != "running":
            self._close(current)
            return
        if current.reply is not None:
            ok, kind = current.reply
            if self._saved(current.reading_id):
                self._end(current, "done")
            elif not ok and kind in BUSY:
                attempts = int(reading["attempts"]) + 1
                backoff = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
                self._end(current, "queued", attempts=attempts, not_before=self.clock() + backoff)
            else:
                attempts = int(reading["attempts"]) + 1
                self._end(current, "failed" if attempts >= COUNTED_ATTEMPTS else "queued", attempts=attempts)
        elif self._saved(current.reading_id) and self._idle(current) >= SAVE_IDLE_SECONDS:
            # Saved, then quiet: a model may end with no words, and then no reply comes (the final review's I2).
            self._end(current, "done")
        elif current.running_seconds >= READ_SECONDS:
            if self._saved(current.reading_id):  # still saving past its time: an attempt; its text is kept at the end
                attempts = int(reading["attempts"]) + 1
                self._end(current, "done" if attempts >= COUNTED_ATTEMPTS else "queued", attempts=attempts)
                return
            uncounted = int(reading["uncounted"]) + 1
            if uncounted >= UNCOUNTED_ATTEMPTS:
                self._end(current, "failed", uncounted=uncounted)
                if self.alerts is not None:
                    self.alerts.raise_("reading_stuck")
            else:
                self._end(current, "queued", uncounted=uncounted)

    def _idle(self, current: _Current) -> float:
        """Seconds since the reading's turn last called a tool."""
        row = self.conn.execute(
            "SELECT MAX(t.used_at) FROM host_tool_call t JOIN host_run r ON r.run_id = t.run_id "
            "WHERE r.host_message_id = ?",
            (current.message_id,),
        ).fetchone()
        return 0.0 if row[0] is None else self.clock() - float(row[0])

    def _saved(self, reading_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM evidence_text WHERE saved_by_reading = ? LIMIT 1", (reading_id,))
        return row.fetchone() is not None

    def _end(
        self,
        current: _Current,
        state: str,
        *,
        attempts: int | None = None,
        uncounted: int | None = None,
        not_before: float = 0.0,
    ) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE reading SET state = ?, attempts = COALESCE(?, attempts), uncounted = COALESCE(?, uncounted), "
                "not_before = ?, ended_at = ?, host_message_id = CASE WHEN ? = 'done' THEN host_message_id END "
                "WHERE id = ?",
                (state, attempts, uncounted, not_before, self.clock(), state, current.reading_id),
            )
        self.events.log("reading_" + ("done" if state == "done" else "failed" if state == "failed" else "retried"), {})
        self._close(current)

    def _close(self, current: _Current) -> None:
        self.queue.retire(current.message_id)
        self.current = None

    async def _resets(self) -> bool:
        """Every peer marked for a reset is reset before anything else is issued; False while one fails. A reset is
        asked only of a running gateway, and one that failed waits its back-off (the final review's I3)."""
        if not self.gate.may_release() or self.clock() < self._reset_after:
            return False
        marked = self.conn.execute("SELECT peer_id, failures FROM reader_peer WHERE needs_reset = 1").fetchall()
        for row in marked:
            peer = int(row["peer_id"])
            if await self.reset(peer):
                self.conn.execute("UPDATE reader_peer SET needs_reset = 0, failures = 0 WHERE peer_id = ?", (peer,))
                continue
            failures = int(row["failures"]) + 1
            self.conn.execute("UPDATE reader_peer SET failures = ? WHERE peer_id = ?", (failures, peer))
            self._reset_after = self.clock() + BACKOFF_SECONDS[min(failures, len(BACKOFF_SECONDS)) - 1]
            if failures >= RESET_ALERT_AFTER and self.alerts is not None:
                self.alerts.raise_("reader_reset_failed")
            return False
        return True

    def _may_issue(self) -> bool:
        if not self.gate.may_release() or self.clock() < self.paused_until:
            return False
        live = self.conn.execute(  # a person's turn: registered or about to be
            "SELECT 1 FROM host_run WHERE expires_at > ? AND ended_at IS NULL "
            "AND sender_id IN (SELECT telegram_id FROM member) LIMIT 1",
            (self.clock(),),
        ).fetchone()
        return live is None

    def _next(self) -> sqlite3.Row | None:
        rows = self.conn.execute(
            "SELECT r.*, e.space_id AS now_space, e.chat_id, e.media_group_id, e.authenticated_subject "
            "FROM reading r JOIN evidence e ON e.id = r.evidence_id "
            "WHERE r.state = 'queued' AND r.not_before <= ? "
            "ORDER BY r.page > 1, r.page, e.received_at DESC, r.id",  # newest first: what a person asks about next
            (self.clock(),),
        ).fetchall()
        for row in rows:
            if not self._album_open(int(row["chat_id"]), row["media_group_id"]):
                found: sqlite3.Row = row
                return found
        return None

    def _album_open(self, chat_id: int, media_group_id: str | None) -> bool:
        if not media_group_id:
            return False
        row = self.conn.execute(
            "SELECT MAX(received_at) FROM evidence WHERE chat_id = ? AND media_group_id = ?", (chat_id, media_group_id)
        ).fetchone()
        last = None if row[0] is None else datetime.fromisoformat(row[0]).timestamp()
        return last is not None and self.clock() - last < self.album_quiet

    def _issue(self) -> None:
        row = self._next()
        if row is None:
            return
        space = str(row["now_space"])  # the record's space now, not when it was queued (review 5's M2)
        peers = self.queue.peers.reader_peers()
        peer = next((p for p, s in peers.items() if s == space), None)
        if peer is None:  # a space no reader serves (a member removed since): nothing reads it
            self.conn.execute(
                "UPDATE reading SET state = 'cancelled', ended_at = ? WHERE id = ?", (self.clock(), row["id"])
            )
            return
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO reader_peer(peer_id, needs_reset) VALUES (?, 1) "
                "ON CONFLICT(peer_id) DO UPDATE SET needs_reset = 1",
                (peer,),
            )
            self.conn.execute(
                "UPDATE reading SET state = 'running', space_id = ?, started_at = ? WHERE id = ?",
                (space, self.clock(), row["id"]),
            )
        message = self.queue.add_reading(int(row["id"]), peer)
        self.current = _Current(int(row["id"]), message, peer)
        self.events.log("reading_issued", {"page": int(row["page"])})

    # ---- the loop ----------------------------------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        self.recover()
        while not stop.is_set():
            await self.step()
            self.tick_running(TICK_SECONDS)
            self.wake.clear()
            stopped = asyncio.ensure_future(stop.wait())
            woken = asyncio.ensure_future(self.wake.wait())
            await asyncio.wait({stopped, woken}, timeout=TICK_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            for task in (stopped, woken):
                task.cancel()


def session_key(peer: int) -> str:
    """The gateway's session of a reader peer's chat (dmScope per-channel-peer; turns.session_of)."""
    return f"agent:main:telegram:direct:{peer}"


def reset_params(peer: int) -> str:
    return json.dumps({"key": session_key(peer), "reason": "reset"})
