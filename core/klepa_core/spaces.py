"""The space is decided at intake by the caption (spec D32).

Keywords match whole words only, so «отлично» never makes a file private.
"""
from __future__ import annotations

import re


def is_private_caption(caption: str | None, keywords: tuple[str, ...]) -> bool:
    text = (caption or "").casefold()
    for keyword in keywords:
        if re.search(r"(?<!\w)" + re.escape(keyword.casefold()) + r"(?!\w)", text):
            return True
    return False


def decide_space(caption: str | None, person_id: str, default_kind: str, keywords: tuple[str, ...]) -> str:
    if default_kind == "personal" or is_private_caption(caption, keywords):
        return f"personal:{person_id}"
    return "shared"
