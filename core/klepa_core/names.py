"""Disk names for originals (docs/architecture.md: Storage, D27).

A file is stored as `<evidence_id>-<sanitized name>`: NFC, no path separators, no control or
bidirectional characters, at most 255 bytes. The original name lives only in the database and
the card; it is never used as a path on its own.
"""

from __future__ import annotations

import unicodedata

MAX_NAME_BYTES = 255
_SEPARATORS = frozenset("/\\:")
_MAX_EXTENSION_BYTES = 16


def sanitize_original_name(name: str | None) -> str:
    if not name:
        return "file"
    name = unicodedata.normalize("NFC", name)
    kept: list[str] = []
    for ch in name:
        if unicodedata.category(ch).startswith("C"):
            continue  # control, format (bidi overrides included), surrogate, private use, unassigned
        kept.append("_" if ch in _SEPARATORS else ch)
    cleaned = "".join(kept).strip().lstrip(".").strip()
    return cleaned or "file"


def _cut_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def disk_name(evidence_id: str, original_name: str | None) -> str:
    prefix = f"{evidence_id}-"
    base = sanitize_original_name(original_name)
    stem, dot, ext = base.rpartition(".")
    extension = f".{ext}" if dot and stem else ""
    if len(extension.encode("utf-8")) > _MAX_EXTENSION_BYTES:
        extension = ""
    if not extension:
        stem = base
    budget = MAX_NAME_BYTES - len(prefix.encode("utf-8")) - len(extension.encode("utf-8"))
    stem = _cut_utf8(stem, budget).rstrip() or "file"
    return prefix + stem + extension
