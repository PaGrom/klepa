"""Alerts to the owner through the service bot (docs/architecture.md: Service bot).

Fixed templates without family data, at most one alert per class per period, every send logged. Without a
bound service bot an alert is only logged, and its class stays open for when the bot is bound. A problem with
the documents folder is reported only once it has lasted a grace period: at login the volume that holds the
folder may mount a minute after Core starts.
"""

from __future__ import annotations

import errno
import secrets
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .db import owner_service_chat, transaction
from .events import EventLog
from .gatekeeper.outbox import Outbox
from .locale import Locale

DEFAULT_LIMITS: dict[str, float] = {
    "documents_unavailable": 6 * 3600,
    "permission_denied": 6 * 3600,
    "channel_dead": 3600,
    "snapshot_failed": 12 * 3600,
    "restarted": 600,
    "album_private_after_copy": 0,
}
DOCUMENTS_GRACE_SECONDS = 300.0


class Alerts:
    def __init__(
        self,
        conn: sqlite3.Connection,
        outbox: Outbox | None,
        events: EventLog,
        locale: Locale,
        *,
        clock: Callable[[], float] = time.time,
        limits: dict[str, float] | None = None,
        documents_grace: float = DOCUMENTS_GRACE_SECONDS,
    ) -> None:
        self.conn = conn
        self.outbox = outbox
        self.events = events
        self.locale = locale
        self.clock = clock
        self.limits = DEFAULT_LIMITS if limits is None else limits
        self.documents_grace = documents_grace
        self._unsent_logged: dict[str, float] = {}
        self._documents_failing_since: float | None = None

    def raise_(self, name: str, **fields: object) -> bool:
        """Queue an alert for the owner, at most one per class per period. True when it was queued."""
        if name not in self.limits:
            raise ValueError(f"unknown alert: {name}")
        now = self.clock()
        period = self.limits[name]
        row = self.conn.execute("SELECT last_sent_at FROM alert_state WHERE class=?", (name,)).fetchone()
        if row is not None and now - float(row["last_sent_at"]) < period:
            return False
        chat_id = owner_service_chat(self.conn)
        if self.outbox is None or chat_id is None:
            # Nobody to tell yet. Log it once per period; the class stays open for when the bot is bound.
            if now - self._unsent_logged.get(name, float("-inf")) >= period:
                self._unsent_logged[name] = now
                self.events.log("alert_unsent", {"alert": name})
            return False
        text = self.locale.service_text(f"alert_{name}", **fields)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO alert_state(class, last_sent_at) VALUES (?, ?) "
                "ON CONFLICT(class) DO UPDATE SET last_sent_at=excluded.last_sent_at",
                (name, now),
            )
            self.outbox.enqueue_text(f"alert:{name}:{secrets.token_hex(8)}", chat_id, text)
        self.events.log("alert_queued", {"alert": name})
        return True

    def documents_failed(self, error: str, code: int | None) -> bool:
        """The documents folder failed. Alert once the problem has lasted the grace period; True from then on."""
        now = self.clock()
        if self._documents_failing_since is None:
            self._documents_failing_since = now
        if now - self._documents_failing_since < self.documents_grace:
            return False
        if code in (errno.EPERM, errno.EACCES):
            interpreter = str(Path(sys.executable).resolve())  # the binary macOS asks about
            self.raise_("permission_denied", error=error, interpreter=interpreter)
        else:
            self.raise_("documents_unavailable", error=error)
        return True

    def documents_ok(self) -> None:
        self._documents_failing_since = None
