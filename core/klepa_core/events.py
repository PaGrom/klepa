"""Append-only event log. Metadata only: no message text, no captions, no secrets, no URLs."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

_FORBIDDEN_KEYS = frozenset({"token", "text", "caption", "file_path", "url"})


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class EventLog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def log(self, kind: str, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        bad = _FORBIDDEN_KEYS.intersection(data)
        if bad:
            raise ValueError(f"event data must not carry {sorted(bad)}")
        self.conn.execute(
            "INSERT INTO event_log(at, kind, data) VALUES (?,?,?)",
            (utc_now_iso(), kind, json.dumps(data, ensure_ascii=False, sort_keys=True)))

    def kinds(self) -> list[str]:
        return [row["kind"] for row in self.conn.execute("SELECT kind FROM event_log ORDER BY id")]
