"""What the host receives (docs/architecture.md: Host interface; spec 4.2, 4.6, 6.1).

Members' messages wait here until the host may have them. The host takes them with getUpdates, under Core's own
update numbering, each chat strictly in order. A served message is built from a fixed list of fields when the host
polls: a text from the inbound journal (message text never lands in core.db, which snapshots copy), an attachment from
what Core read of its files (spec 6.1 item 4). An attachment waits for its reading, and its chat's later messages wait
behind it (spec 6.1 item 5): a voice note asking for something, then "cancel", must not reach the host the other way
round.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Collection, Sequence
from typing import Any

from ..config import Member
from ..db import transaction
from ..gatekeeper.outbox import Outbox
from ..journal import InboundJournal
from ..processing.jobs import KINDS, job_kind
from .peers import Peers
from .served import BEGINNING_CHARS, RECORDINGS, TRANSCRIPT_CHARS, Draft, Item, attachment_text, clean

MAX_UPDATES = 100
ANSWER_WINDOW_SECONDS = 600.0  # the host may go on writing to a chat this long after an answer (spec 4.2)
NOTICE_EVERY_SECONDS = 600.0  # a fixed notice goes to a chat at most once per ten minutes (spec 4.6)
PROBE_NAME = "Klepa probe"
PROBE_TEXT = "Klepa start probe"
MODEL_PROBE_TEXT = "Klepa start check: answer with one word."
PROBE_KINDS = ("probe", "model_probe")
FORWARDED_MARK = "[forwarded message]"

MayServe = Callable[[int, str], bool]
FRAGMENT_CHARS = 4000  # a text this long is joined with the next message (OpenClaw 2026.9.4, its Telegram ingress)
KEEP_DIGESTS = 1000


def text_digest(text: str) -> tuple[str, int]:
    """What Core keeps of a served text to know the turn that is its answer: its SHA-256, and its length as the
    gateway's JavaScript counts it, in UTF-16 code units."""
    return hashlib.sha256(text.encode()).hexdigest(), len(text.encode("utf-16-le")) // 2


class HostQueue:
    def __init__(
        self,
        conn: sqlite3.Connection,
        journal: InboundJournal,
        members: dict[int, Member],
        peers: Peers,
        *,
        readable: Callable[[], Collection[str]] = lambda: KINDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.conn = conn
        self.journal = journal
        self.members = members
        self.peers = peers  # the host knows people by stand-ins, never by their Telegram ids
        self.probe_peer = peers.probe
        self.readable = readable  # the kinds Core reads now: a file of another kind waits, and its chat must not
        self.clock = clock
        self.drafts = False  # True once Core makes drafts of documents: a batch waits until it was looked at for one
        self.changed = asyncio.Event()
        # The digests of served texts, by message, in memory only: core.db never holds what people wrote, nor a
        # digest a short text could be guessed from. After a restart a turn takes its chat's oldest message.
        self.digests: dict[int, tuple[str, int]] = {}

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

    def enqueue_attachment(self, chat_id: int, items: Sequence[tuple[int, int, str]]) -> int | None:
        """A batch of a member's attachments for the host, as (update id, message id, record) in the order they were
        sent: one message, issued once their reading ends. An original already in another message is left out (a
        batch redone after a crash), and the message takes the ids of the first one left; None when none is left."""
        records = [evidence_id for _, _, evidence_id in items]
        marks = ",".join("?" * len(records))
        with transaction(self.conn):
            given = {
                row[0]
                for row in self.conn.execute(
                    f"SELECT evidence_id FROM host_message_item WHERE evidence_id IN ({marks})", records
                )
            }
            fresh = [item for item in items if item[2] not in given]
            if not fresh:
                return None
            update_id, message_id, _ = fresh[0]
            kept = [evidence_id for _, _, evidence_id in fresh]
            rows = self.conn.execute(
                f"SELECT message_date, tags FROM evidence WHERE id IN ({','.join('?' * len(kept))})", kept
            ).fetchall()
            date = min((int(row["message_date"]) for row in rows if row["message_date"]), default=int(self.clock()))
            forwarded = any("forwarded" in json.loads(row["tags"] or "[]") for row in rows)
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO host_message(source_update_id, kind, chat_id, message_id, sender_id, date, "
                "forwarded, created_at) VALUES (?, 'attachment', ?, ?, ?, ?, ?, ?)",
                (update_id, chat_id, message_id, chat_id, date, int(forwarded), self.clock()),
            )
            if cursor.rowcount != 1:
                return None
            host_message_id = cursor.lastrowid
            self.conn.executemany(
                "INSERT INTO host_message_item(host_message_id, position, evidence_id) VALUES (?, ?, ?)",
                [(host_message_id, position, evidence_id) for position, evidence_id in enumerate(kept)],
            )
        self.wake()
        return host_message_id

    def add_probe(self, kind: str = "probe") -> int:
        """The synthetic message of the live probe (spec 4.6), in the probe peer's own chat. Every earlier probe is
        retired first.

        Its message id grows with the clock as well, so the host, which remembers chat_id:message_id pairs on
        disk, never takes a new probe for a repeat, even after core.db was replaced.
        """
        now = self.clock()
        with transaction(self.conn):
            # A probe that a stopped Core never retired must not take this probe's turn.
            self.conn.execute(
                "UPDATE host_message SET answered_at=? WHERE kind IN ('probe', 'model_probe') AND answered_at IS NULL",
                (now,),
            )
            last = self.conn.execute(
                "SELECT COALESCE(MAX(message_id), 0) FROM host_message WHERE chat_id=?", (self.probe_peer,)
            ).fetchone()[0]
            cursor = self.conn.execute(
                "INSERT INTO host_message(source_update_id, kind, chat_id, message_id, sender_id, date, created_at) "
                "VALUES (NULL, ?, ?, ?, ?, ?, ?)",
                (kind, self.probe_peer, max(int(last) + 1, int(now)), self.probe_peer, int(now), now),
            )
        self.wake()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def retire(self, host_message_id: int) -> None:
        """A probe that is over is never served later."""
        self.conn.execute(
            "UPDATE host_message SET answered_at=COALESCE(answered_at, ?) "
            "WHERE id=? AND kind IN ('probe', 'model_probe')",
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
                    self._keep_digest(int(row["id"]), message)
                    updates.append({"update_id": int(row["update_id"]), "message": message})
            behind: set[int] = set()  # chats whose oldest waiting message is not served now: the rest wait behind it
            for row in self._waiting():
                if len(updates) >= limit:
                    break
                chat_id = int(row["chat_id"])
                if chat_id in behind or not self.ready(row) or not may_serve(int(row["id"]), str(row["kind"])):
                    behind.add(chat_id)
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
                self._keep_digest(int(row["id"]), message)
                updates.append({"update_id": update_id, "message": message})
        return updates

    def _keep_digest(self, host_message_id: int, message: dict[str, Any]) -> None:
        self.digests[host_message_id] = text_digest(str(message.get("text", "")))
        while len(self.digests) > KEEP_DIGESTS:
            del self.digests[next(iter(self.digests))]

    def _waiting(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT m.* FROM host_message m LEFT JOIN host_update u ON u.host_message_id = m.id "
            "WHERE u.update_id IS NULL AND m.answered_at IS NULL ORDER BY m.id"
        ).fetchall()

    def ready(self, row: sqlite3.Row) -> bool:
        """An attachment is ready once its reading ended and, while Core makes drafts of documents, it was looked at
        for one: the model is told of the draft its photos make (spec 6.1 item 4)."""
        if row["kind"] != "attachment":
            return True
        return self.read_ended(row) and (not self.drafts or bool(row["draft_checked"]))

    def read_ended(self, row: sqlite3.Row) -> bool:
        """Each of its files is read, failed, cancelled, has no reader, or waits for what the owner installs: a kind
        Core cannot read now must not hold the chat (spec 6.1 item 5)."""
        readable = self.readable()
        jobs = self.conn.execute(
            "SELECT j.kind, j.state FROM host_message_item i JOIN job j ON j.evidence_id = i.evidence_id "
            "WHERE i.host_message_id=?",
            (row["id"],),
        ).fetchall()
        return all(job["state"] in ("done", "failed", "cancelled") or job["kind"] not in readable for job in jobs)

    def _items(self, host_message_id: int) -> list[Item]:
        """The batch's files as the served text needs them: of a text, only the first pages a message can show, and
        how many there are up to the last with text (a blank back is not more to read); a 2,000-page PDF is not loaded
        for a beginning of 3,000 characters."""
        readable = self.readable()
        items = []
        for row in self.conn.execute(
            "SELECT e.id, e.kind, e.original_name, e.mime, j.kind AS reader, j.state, j.note, "
            "(SELECT MAX(page) FROM evidence_text t WHERE t.evidence_id = e.id) AS last_page "
            "FROM host_message_item i JOIN evidence e ON e.id = i.evidence_id LEFT JOIN job j ON j.evidence_id = e.id "
            "WHERE i.host_message_id=? ORDER BY i.position",
            (host_message_id,),
        ).fetchall():
            reader = row["reader"] or job_kind(row["kind"], row["mime"], row["original_name"])
            texts: list[str] = []
            if row["state"] == "done":
                limit = TRANSCRIPT_CHARS if reader in RECORDINGS else BEGINNING_CHARS
                used = 0
                for page in self.conn.execute(
                    "SELECT page, text FROM evidence_text WHERE evidence_id=? ORDER BY page", (row["id"],)
                ):
                    texts += [""] * (int(page["page"]) - 1 - len(texts))  # a page without text has no row
                    texts.append(str(page["text"]))
                    used += len(texts[-1])
                    if used > limit:
                        break
            items.append(
                Item(
                    row["id"],
                    row["kind"],
                    row["original_name"],
                    row["mime"],
                    reader,
                    row["state"],
                    reader in readable,
                    row["note"],
                    tuple(texts),
                    int(row["last_page"] or 0),
                )
            )
        return items

    def _attachment_text(self, row: sqlite3.Row) -> str:
        captions = [
            str(caption[0])
            for caption in self.conn.execute(
                "SELECT e.caption FROM host_message_item i JOIN evidence e ON e.id = i.evidence_id "
                "WHERE i.host_message_id=? AND e.caption IS NOT NULL ORDER BY i.position",
                (row["id"],),
            )
        ]
        mark = FORWARDED_MARK if row["forwarded"] else None
        return attachment_text(captions, self._items(int(row["id"])), forwarded_mark=mark, draft=self._draft(row))

    def _draft(self, row: sqlite3.Row) -> Draft | None:
        """The draft the batch's images made or joined, with the revision they made: their pages then."""
        if row["draft_id"] is None:
            return None
        draft_id, revision = int(row["draft_id"]), row["draft_revision"]
        if revision is None:  # its pages already: the draft as it is
            revision = self.conn.execute("SELECT revision FROM draft WHERE id=?", (draft_id,)).fetchone()[0]
            return Draft(draft_id, self._draft_pages(draft_id, int(revision)), 0)
        pages = self._draft_pages(draft_id, int(revision))
        records = tuple(
            str(row[0])
            for row in self.conn.execute(
                "SELECT evidence_id FROM draft_page WHERE draft_id=? AND revision=? ORDER BY position",
                (draft_id, int(revision)),
            )
        )
        sent = self.conn.execute(  # the review's I4: the host took the batch only after its sheet went out
            "SELECT 1 FROM draft_sheet WHERE draft_id=? AND revision>=? AND outbound_key IS NOT NULL LIMIT 1",
            (draft_id, int(revision)),
        ).fetchone()
        added = pages - self._draft_pages(draft_id, int(revision) - 1)
        state = self.conn.execute("SELECT state FROM draft WHERE id=?", (draft_id,)).fetchone()[0]
        return Draft(draft_id, pages, added, records, int(revision), sent is None, state == "declined")

    def _draft_pages(self, draft_id: int, revision: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM draft_page WHERE draft_id=? AND revision=?", (draft_id, revision)
        ).fetchone()
        return int(row[0])

    def _next_update_id(self, now: float) -> int:
        last = self.conn.execute("SELECT COALESCE(MAX(update_id), 0) FROM host_update").fetchone()[0]
        return max(int(last) + 1, int(now))

    def _message(self, row: sqlite3.Row) -> dict[str, Any] | None:
        """The served message: message_id, from, chat, date and text, nothing else (spec 4.5), from and to the
        sender's stand-in."""
        sender_id = int(row["sender_id"])
        host_id = self.peers.host_id(sender_id)
        if host_id is None:
            return None
        if row["kind"] in PROBE_KINDS:
            name, text = PROBE_NAME, PROBE_TEXT if row["kind"] == "probe" else MODEL_PROBE_TEXT
        elif row["kind"] == "attachment":
            member = self.members.get(sender_id)
            if member is None:
                return None
            name, text = member.name, self._attachment_text(row)
        else:
            update = self.journal.get(int(row["source_update_id"])) or {}
            original = (update.get("message") or {}).get("text")
            member = self.members.get(sender_id)
            if not isinstance(original, str) or member is None:
                return None
            name = member.name
            text = clean(original)  # no text a person writes or forwards can hold a mark of Core's
            text = f"{FORWARDED_MARK}\n{text}" if row["forwarded"] else text
        person = {"id": host_id, "is_bot": False, "first_name": name}
        return {
            "message_id": int(row["message_id"]),
            "from": person,
            "chat": {"id": host_id, "type": "private", "first_name": name},  # a private chat's id is its person's
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

    def unanswered(
        self, chat_id: int, sender_id: int | None = None, exclude: Collection[int] = ()
    ) -> list[sqlite3.Row]:
        """The issued messages of a chat that have no answer yet, oldest first."""
        rows: list[sqlite3.Row] = self.conn.execute(
            "SELECT m.* FROM host_message m JOIN host_update u ON u.host_message_id = m.id "
            "WHERE m.chat_id=? AND m.answered_at IS NULL ORDER BY m.id",
            (chat_id,),
        ).fetchall()
        return [
            row for row in rows if (sender_id is None or row["sender_id"] == sender_id) and row["id"] not in exclude
        ]

    def oldest_unanswered(
        self, chat_id: int, sender_id: int | None = None, exclude: Collection[int] = ()
    ) -> sqlite3.Row | None:
        """The oldest issued message of a chat that has no answer yet."""
        rows = self.unanswered(chat_id, sender_id, exclude)
        return rows[0] if rows else None

    def mark_answered(self, chat_id: int, prefer: int | None = None) -> int | None:
        """Record that the host wrote to a chat: the answer belongs to the message of the turn that wrote it, and
        without a turn (stage 1 answers in before_dispatch) to the oldest message still waiting for one.

        The turn's answer closes its chat's older issued messages too, but for one a live turn of its own answers:
        the gateway runs a chat's messages in order, so an older one without a turn was joined to this turn, or never
        gets one (the live run of 2026-10-26)."""
        now = self.clock()
        if prefer is not None:
            row = self.conn.execute(
                "SELECT id, answered_at FROM host_message WHERE id=? AND chat_id=?", (prefer, chat_id)
            ).fetchone()
            if row is not None:
                if row["answered_at"] is None:
                    self.conn.execute("UPDATE host_message SET answered_at=? WHERE id=?", (now, prefer))
                self.conn.execute(
                    "UPDATE host_message SET answered_at=? WHERE chat_id=? AND id<? AND answered_at IS NULL "
                    "AND id IN (SELECT host_message_id FROM host_update) AND NOT EXISTS (SELECT 1 FROM host_run r "
                    "WHERE r.host_message_id = host_message.id AND r.expires_at > ? AND r.ended_at IS NULL)",
                    (now, chat_id, prefer, now),
                )
                return prefer
        oldest = self.oldest_unanswered(chat_id)
        if oldest is None:
            return None
        self.conn.execute("UPDATE host_message SET answered_at=? WHERE id=?", (now, oldest["id"]))
        return int(oldest["id"])

    # ---- notices -------------------------------------------------------------------------------------------------
    def waiting_chats(self) -> list[int]:
        """Chats whose messages wait for the host."""
        rows = self.conn.execute(
            "SELECT DISTINCT m.chat_id FROM host_message m LEFT JOIN host_update u ON u.host_message_id = m.id "
            "WHERE u.update_id IS NULL AND m.kind IN ('text', 'attachment') AND m.answered_at IS NULL "
            "ORDER BY m.chat_id"
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
