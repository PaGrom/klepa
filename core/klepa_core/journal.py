"""Inbound journal (docs/architecture.md: Intake): every Telegram update is stored durably before it is acknowledged.

It lives in its own SQLite file, so restoring core.db from a snapshot never touches it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .db import connect, transaction


class InboundJournal:
    def __init__(self, path: Path) -> None:
        self.conn = connect(path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS inbound_update (
                update_id INTEGER PRIMARY KEY,
                received_at TEXT NOT NULL,
                raw TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'new' CHECK (state IN ('new','done','rejected','failed')),
                note TEXT)"""
        )

    def append_batch(self, updates: list[dict[str, Any]], received_at: str) -> list[int]:
        """Store a getUpdates batch in one durable transaction; return the ids that are new."""
        new_ids: list[int] = []
        with transaction(self.conn):
            for update in updates:
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO inbound_update(update_id, received_at, raw) VALUES (?,?,?)",
                    (int(update["update_id"]), received_at, json.dumps(update, ensure_ascii=False)),
                )
                if cursor.rowcount == 1:
                    new_ids.append(int(update["update_id"]))
        return new_ids

    def pending(self) -> list[tuple[int, dict[str, Any]]]:
        rows = self.conn.execute("SELECT update_id, raw FROM inbound_update WHERE state='new' ORDER BY update_id")
        return [(row["update_id"], json.loads(row["raw"])) for row in rows]

    def state(self, update_id: int) -> str | None:
        row = self.conn.execute("SELECT state FROM inbound_update WHERE update_id=?", (update_id,)).fetchone()
        return row["state"] if row else None

    def mark(self, update_id: int, state: str, note: str | None = None) -> None:
        self.conn.execute("UPDATE inbound_update SET state=?, note=? WHERE update_id=?", (state, note, update_id))

    def mark_many(self, update_ids: Iterable[int], state: str, note: str | None = None) -> None:
        """Mark several updates in one transaction: all of them or none."""
        with transaction(self.conn):
            self.conn.executemany(
                "UPDATE inbound_update SET state=?, note=? WHERE update_id=?",
                [(state, note, update_id) for update_id in update_ids],
            )

    def next_offset(self) -> int | None:
        row = self.conn.execute("SELECT MAX(update_id) AS last FROM inbound_update").fetchone()
        return None if row["last"] is None else int(row["last"]) + 1

    def close(self) -> None:
        self.conn.close()
