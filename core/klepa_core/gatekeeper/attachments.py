"""While a file is read (spec 6.1 items 4-6): Core's interim answer, its edit when the reading ends, and cancel.

A batch whose reading takes over two seconds gets Core's "listening…" (recordings) or "reading…", in the
installation's language, as a reply to its first file, with a Cancel button. The two seconds count from when the
batch closed and its reading began: the window that gathers a batch is not reading (spec 6.1 item 4 and the batch's
definition), so the person sees it some four seconds after sending one file. When the reading ends, Core edits that
message: the button goes and the text says how it went (the owner's choice of 2026-10-18). A cancel, by the button or
by a message of only a cancel word, ends the batch's jobs at once, past the chat's queue: the batch never reaches the
host, and the person hears that the file is kept. While several batches of a chat are read, the word gets a question
with a button for each.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import sqlite3
import time
from collections.abc import Callable, Collection
from typing import Any

from ..db import transaction
from ..events import EventLog, utc_now_iso
from ..host.queue import HostQueue
from ..locale import Locale, available_locales, load_locale
from ..processing.jobs import Jobs
from ..telegram.client import Ambiguous, BotApi, NotSent, TelegramError
from .drafts import Answer, Drafts, is_draft_action
from .intake import Classified
from .outbox import Outbox

INTERIM_AFTER_SECONDS = 2.0  # a reading that ends sooner gets no interim answer (spec 6.1 item 4)
BUTTON_TTL_SECONDS = 24 * 3600.0  # a reading ends within its readers' limits, far within a day
TICK_SECONDS = 0.5
LABEL_CHARS = 40
_RECORDINGS = frozenset({"voice", "audio", "video"})  # records whose reading is listening
_CANCEL = "cancel:"


class Attachments:
    def __init__(
        self,
        conn: sqlite3.Connection,
        queue: HostQueue,
        outbox: Outbox,
        api: BotApi,
        locale: Locale,
        events: EventLog,
        *,
        jobs: Jobs | None = None,
        stop_reading: Callable[[Collection[int]], None] | None = None,
        clock: Callable[[], float] = time.time,
        interim_after: float = INTERIM_AFTER_SECONDS,
    ) -> None:
        self.conn = conn
        self.queue = queue
        self.outbox = outbox
        self.api = api
        self.locale = locale
        self.events = events
        self.jobs = jobs  # None: nothing is read here, and nothing is waited for
        self.stop_reading = stop_reading  # the worker's: kills the reader of a cancelled job
        self.clock = clock
        self.interim_after = interim_after
        # The family may write in any of the languages Klepa speaks: a cancel word of each of them counts.
        self.words = frozenset().union(*(load_locale(code).cancel_words for code in available_locales()))
        self.drafts: Drafts | None = None  # the buttons of a draft's sheet are Core's too (spec 6.1 item 4)
        self.wake = asyncio.Event()

    # ---- what is being read -----------------------------------------------------------------------------------------
    def pending(self, chat_id: int) -> list[sqlite3.Row]:
        """A chat's batches whose reading has not ended: neither given to the host nor cancelled."""
        rows = self.conn.execute(
            "SELECT m.* FROM host_message m LEFT JOIN host_update u ON u.host_message_id = m.id "
            "WHERE m.kind='attachment' AND m.chat_id=? AND u.update_id IS NULL AND m.answered_at IS NULL ORDER BY m.id",
            (chat_id,),
        ).fetchall()
        return [row for row in rows if not self.queue.ready(row)]

    def _records(self, host_message_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT e.id, e.kind, e.original_name, j.state FROM host_message_item i JOIN evidence e ON e.id = "
            "i.evidence_id LEFT JOIN job j ON j.evidence_id = e.id WHERE i.host_message_id=? ORDER BY i.position",
            (host_message_id,),
        ).fetchall()

    def _listening(self, host_message_id: int) -> bool:
        return all(record["kind"] in _RECORDINGS for record in self._records(host_message_id))

    # ---- cancel -----------------------------------------------------------------------------------------------------
    def cancel(self, host_message_id: int, *, word: tuple[int, int] | None = None) -> bool:
        """End a batch's reading: its jobs are cancelled and it never reaches the host. False when it reached the host
        already or was cancelled before. `word`: the chat and message of the cancel word that asked for it, kept with
        the cancel and Core's answer to it, so a word replayed after a crash is known as Core's."""
        now = self.clock()
        with transaction(self.conn):
            cancelled = self.conn.execute(
                "UPDATE host_message SET cancelled_at=?, answered_at=COALESCE(answered_at, ?) WHERE id=? "
                "AND kind='attachment' AND cancelled_at IS NULL "
                "AND id NOT IN (SELECT host_message_id FROM host_update)",
                (now, now, host_message_id),
            ).rowcount
            if cancelled == 1 and word is not None:
                chat_id, message_id = word
                self._keep_word(chat_id, message_id)
                interim = self.conn.execute("SELECT interim_key FROM host_message WHERE id=?", (host_message_id,))
                if interim.fetchone()[0] is None:  # no interim answer to edit: Core answers the word itself
                    text = self.locale.text("interim_cancelled")
                    self.outbox.enqueue_text(f"cancelled:{chat_id}:{message_id}", chat_id, text, reply_to=message_id)
        if cancelled != 1:
            return False
        records = [record["id"] for record in self._records(host_message_id)]
        running = self.jobs.cancel(records) if self.jobs is not None else []
        if running and self.stop_reading is not None:
            self.stop_reading(running)
        self.events.log("attachment_cancelled", {"host_message_id": host_message_id, "files": len(records)})
        self.queue.wake()
        self.wake.set()
        return True

    def cancel_word(self, chat_id: int, message_id: int, text: str) -> bool:
        """A message of only a cancel word while the chat's files are read is Core's: True when it took it, also when
        it took it before a crash and comes again."""
        word = text.strip().strip(".!?…").strip().casefold()
        if word not in self.words:
            return False
        if self._word_kept(chat_id, message_id):
            return True
        pending = self.pending(chat_id)
        if not pending:
            return False  # nothing is read: the word is the person's to the model
        if len(pending) > 1:
            self._ask(chat_id, message_id, pending)
            return True
        return self.cancel(int(pending[0]["id"]), word=(chat_id, message_id))

    def _keep_word(self, chat_id: int, message_id: int) -> None:
        """A cancel word Core took, as a message the host is never given; in the caller's transaction."""
        now = self.clock()
        self.conn.execute(
            "INSERT OR IGNORE INTO host_message(source_update_id, kind, chat_id, message_id, sender_id, date, "
            "created_at, answered_at) VALUES (NULL, 'word', ?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, chat_id, int(now), now, now),
        )

    def _word_kept(self, chat_id: int, message_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM host_message WHERE chat_id=? AND message_id=? AND kind='word'", (chat_id, message_id)
        ).fetchone()
        return row is not None

    def _label(self, row: sqlite3.Row) -> str:
        records = self._records(int(row["id"]))
        first = records[0]
        if first["kind"] in _RECORDINGS:
            name = first["original_name"] if first["kind"] == "audio" else None
            what = "🎧 " + (name or self.locale.text("label_video" if first["kind"] == "video" else "label_voice"))
        else:
            name = None if first["kind"] == "photo" else first["original_name"]
            what = "📄 " + (name or self.locale.text("label_photo" if first["kind"] == "photo" else "label_file"))
        if len(what) > LABEL_CHARS:
            what = what[: LABEL_CHARS - 1] + "…"
        return what + (f" (+{len(records) - 1})" if len(records) > 1 else "")

    def _ask(self, chat_id: int, message_id: int, pending: list[sqlite3.Row]) -> None:
        """Which of the batches being read? A button for each, and one for all."""
        key = f"cancel_which:{chat_id}:{message_id}"
        ids = [str(row["id"]) for row in pending]
        buttons = [(secrets.token_hex(16), _CANCEL + str(row["id"]), self._label(row)) for row in pending]
        buttons.append((secrets.token_hex(16), _CANCEL + ",".join(ids), self.locale.text("cancel_all")))
        markup = {"inline_keyboard": [[{"text": label, "callback_data": action_id}] for action_id, _, label in buttons]}
        with transaction(self.conn):
            self._send_with_buttons(key, chat_id, self.locale.text("cancel_which"), message_id, markup, buttons)
            self._keep_word(chat_id, message_id)

    def _send_with_buttons(
        self,
        key: str,
        chat_id: int,
        text: str,
        reply_to: int,
        markup: dict[str, Any],
        buttons: list[tuple[str, str, str]],
    ) -> bool:
        """The message and its buttons, in the caller's transaction."""
        if not self.outbox.enqueue_text(key, chat_id, text, reply_to=reply_to, reply_markup=markup):
            return False
        for action_id, action, _ in buttons:
            self.conn.execute(
                "INSERT INTO button_action(id, action, chat_id, outbound_key, expires_at) VALUES (?, ?, ?, ?, ?)",
                (action_id, action, chat_id, key, self.clock() + BUTTON_TTL_SECONDS),
            )
        return True

    async def press(self, c: Classified) -> None:
        """A press of one of Core's buttons in the family bot: one use, its own chat and message, before it expires
        (scenario 45). A cancel button cancels at once, a draft's button goes to the drafts; any press that is not
        good says the button no longer works."""
        row = self.conn.execute("SELECT * FROM button_action WHERE id=?", (c.data,)).fetchone()
        answer = Answer(self.locale.text("button_expired"))
        if (
            row is not None
            and row["used_at"] is None
            and float(row["expires_at"]) > self.clock()
            and row["chat_id"] == c.chat_id
            and self._on_its_message(str(row["outbound_key"]), c.message_id)
        ):
            action = str(row["action"])
            if action.startswith(_CANCEL) and self._cancel_pressed(row, c):
                answer = Answer(None)
            elif self.drafts is not None and is_draft_action(action):
                assert c.chat_id is not None
                answer = self.drafts.press(action, c.chat_id)
        try:
            assert c.callback_id is not None
            await self.api.answer_callback_query(c.callback_id, answer.text, show_alert=answer.alert)
        except (TelegramError, NotSent, Ambiguous) as exc:
            self.events.log("press_answer_failed", {"error": type(exc).__name__})

    def _cancel_pressed(self, row: sqlite3.Row, c: Classified) -> bool:
        """A cancel button: it and the others on its message are spent, and its batches cancelled."""
        claimed = self.conn.execute(
            "UPDATE button_action SET used_at=? WHERE outbound_key=? AND used_at IS NULL",
            (utc_now_iso(), row["outbound_key"]),
        ).rowcount
        if not claimed:
            return False
        targets = [int(part) for part in str(row["action"])[len(_CANCEL) :].split(",") if part.isdigit()]
        results = [self.cancel(target) for target in targets]  # every one of them, not the first only
        done = any(results)
        if done and str(row["outbound_key"]).startswith("cancel_which:"):
            assert c.chat_id is not None
            assert c.message_id is not None
            self.outbox.enqueue_edit(
                f"edit:{row['outbound_key']}", c.chat_id, c.message_id, self.locale.text("interim_cancelled")
            )
        return done

    def _on_its_message(self, key: str, message_id: int | None) -> bool:
        """The press came on the message that carries the button: Telegram's id for it, or, for a send whose outcome
        is unknown, any message of the chat (the button's 128-bit id is on that message alone)."""
        known = self.outbox.message_id(key)
        return known == message_id if known is not None else self.outbox.state(key) == "UNKNOWN"

    # ---- the interim answer -----------------------------------------------------------------------------------------
    def tick(self) -> None:
        """Interim answers that are due, and edits of those whose reading ended."""
        due = self.conn.execute(
            "SELECT m.* FROM host_message m LEFT JOIN host_update u ON u.host_message_id = m.id "
            "WHERE m.kind='attachment' AND m.interim_key IS NULL AND m.answered_at IS NULL AND u.update_id IS NULL "
            "AND m.created_at <= ? ORDER BY m.id",
            (self.clock() - self.interim_after,),  # from when the batch closed and its reading began
        ).fetchall()
        for row in due:
            if not self.queue.ready(row):  # one that ended sooner gets no interim answer
                self._show(row)
        shown = self.conn.execute("SELECT * FROM host_message WHERE interim_state='shown'").fetchall()
        for row in shown:
            final = self._final(row)
            if final is not None:
                self._edit(row, final)

    def _show(self, row: sqlite3.Row) -> None:
        key = f"interim:{row['id']}"
        action_id = secrets.token_hex(16)  # 128 bits
        text = self.locale.text("interim_listening" if self._listening(int(row["id"])) else "interim_reading")
        markup = {"inline_keyboard": [[{"text": self.locale.text("cancel_button"), "callback_data": action_id}]]}
        button = [(action_id, _CANCEL + str(row["id"]), "")]
        with transaction(self.conn):
            self._send_with_buttons(key, int(row["chat_id"]), text, int(row["message_id"]), markup, button)
            self.conn.execute(
                "UPDATE host_message SET interim_key=?, interim_state='shown' WHERE id=?", (key, row["id"])
            )

    def _final(self, row: sqlite3.Row) -> str | None:
        """What the interim answer says once the reading ended; None while it goes on."""
        if row["cancelled_at"] is not None:
            return "interim_cancelled"
        if not self.queue.ready(row):
            return None
        states = [record["state"] for record in self._records(int(row["id"]))]
        if any(state in (None, "failed", "cancelled") for state in states):  # None: a kind no reader reads
            return "interim_unread"
        if any(state in ("queued", "running") for state in states):  # ready, yet waiting for what the owner installs
            return "interim_later"
        return "interim_listened" if self._listening(int(row["id"])) else "interim_read"

    def _edit(self, row: sqlite3.Row, final: str) -> None:
        key = str(row["interim_key"])
        if self.outbox.state(key) == "SENDING":
            return  # on its way: Telegram's id for it comes in a moment
        message_id = self.outbox.message_id(key)
        chat_id = int(row["chat_id"])
        with transaction(self.conn):
            if message_id is not None:
                self.outbox.enqueue_edit(f"interim_edit:{row['id']}", chat_id, message_id, self.locale.text(final))
            else:
                self.outbox.withdraw(key)  # one that never left never will; one whose outcome is unknown stays so
                if final == "interim_cancelled":  # nothing Core can edit says the cancel was taken: a reply does
                    self.outbox.enqueue_text(
                        f"cancelled:{row['id']}", chat_id, self.locale.text(final), reply_to=int(row["message_id"])
                    )
            self.conn.execute(
                "UPDATE host_message SET interim_state=? WHERE id=?", (final.removeprefix("interim_"), row["id"])
            )
            self.conn.execute(
                "UPDATE button_action SET used_at=? WHERE outbound_key=? AND used_at IS NULL", (utc_now_iso(), key)
            )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.wake.clear()
            self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), TICK_SECONDS)
