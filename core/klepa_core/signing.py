"""Canonical JSON and HMAC-SHA256 signatures for cards, manifests and journals (docs/architecture.md: D24)."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sign(key: bytes, obj: Any) -> str:
    return hmac.new(key, canonical_json(obj), hashlib.sha256).hexdigest()


def verify(key: bytes, obj: Any, signature: str) -> bool:
    return hmac.compare_digest(sign(key, obj), signature)
