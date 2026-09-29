"""Signed provenance cards next to each original (docs/architecture.md: Storage, D24)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .durable import write_new_atomically
from .signing import sign, verify

CARD_VERSION = 1
_FIELDS = (
    "space_id",
    "kind",
    "original_name",
    "disk_name",
    "mime",
    "size",
    "sha256",
    "received_at",
    "channel",
    "chat_id",
    "message_id",
    "authenticated_subject",
    "caption",
)


def build_card(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    card: dict[str, Any] = {"card_version": CARD_VERSION, "evidence_id": row["id"]}
    for key in _FIELDS:
        card[key] = row[key]
    card["tags"] = json.loads(row["tags"] or "[]")
    return card


def card_file_name(evidence_id: str) -> str:
    return f"{evidence_id}.card.json"


def write_card(directory: Path, card: dict[str, Any], key: bytes) -> Path:
    signed = dict(card, signature=sign(key, card))
    data = json.dumps(signed, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    return write_new_atomically(directory, card_file_name(card["evidence_id"]), data)


def read_card(path: Path, key: bytes) -> dict[str, Any] | None:
    try:
        signed = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(signed, dict):
        return None
    signature = signed.pop("signature", None)
    if not isinstance(signature, str) or not verify(key, signed, signature):
        return None
    return signed
