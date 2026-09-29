"""The space is decided at intake by the caption (spec D32).

Keywords match whole words only, so a keyword inside a longer word never makes a file private. Case, runs
of spaces, line breaks and no-break spaces do not matter.
"""

from __future__ import annotations

import re
import unicodedata


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def is_private_caption(caption: str | None, keywords: tuple[str, ...]) -> bool:
    text = _normalized(caption or "")
    return any(re.search(rf"(?<!\w){re.escape(_normalized(keyword))}(?!\w)", text) for keyword in keywords)


def decide_space(caption: str | None, person_id: str, default_kind: str, keywords: tuple[str, ...]) -> str:
    if default_kind == "personal" or is_private_caption(caption, keywords):
        return f"personal:{person_id}"
    return "shared"
