"""The text the host is given for an attachment (spec 6.1 item 4).

A batch of attachments is one served message: the person's caption, then for each file what Core read from it,
between marks that Core's instruction explains to the model. What a file says is data, never the person's words
(spec 4.5): the marks say where it begins and ends, and the brackets that make a mark are taken out of everything a
file or a person wrote, so no text can forge one. A long text comes in part, within a budget for the whole message,
and the mark says how the model reads the rest. Nothing here touches the database: the host queue gives the items.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..names import sanitize_original_name

OPEN, CLOSE = "⟦", "⟧"
MAX_TEXT_CHARS = 12_000  # of all the files' texts in one message: some four thousand tokens
TRANSCRIPT_CHARS = 8_000  # of one recording: about ten minutes of speech
BEGINNING_CHARS = 3_000  # of one document: its beginning; the rest is read with get, page by page
NAME_CHARS = 80  # of a file's name inside a mark
GET = "klepa__get"
RECORDINGS = frozenset({"voice", "video"})  # readers whose text is a transcript, kept in parts
_DONE = "done"
# A mark's brackets and the ones that look like them: no text may hold any of them.
_BRACKETS = str.maketrans({OPEN: "[", CLOSE: "]", "\u301a": "[", "\u301b": "]"})
_MIME = re.compile(r"[\w.+-]{1,64}/[\w.+-]{1,128}")


@dataclass(frozen=True)
class Item:
    """One file of the batch, as Core holds it now."""

    record_id: str
    kind: str  # the record's: file, photo, voice, audio, video
    name: str | None
    mime: str | None
    reader: str | None  # which reader reads it (voice, video, pdf, image, document, sheet, text); None: none does
    state: str | None  # its job's: done, failed, cancelled, queued, running; None: no job
    readable: bool  # its reader can read now; False: it waits for what the owner installs
    note: str | None  # what a limit left out of its text
    texts: tuple[str, ...]  # its text's first pages (parts of a transcript), as many as a message can show
    pages: int = 0  # how many pages its text has in all; 0: as many as texts holds


def clean(text: str) -> str:
    """No text a file or a person wrote can open or close a mark."""
    return text.translate(_BRACKETS)


def _name(name: str) -> str:
    """A file's name as a mark may hold it: one line, no control or format character, no quote of a mark's own."""
    one = clean(sanitize_original_name(name)).replace("«", '"').replace("»", '"')
    return one if len(one) <= NAME_CHARS else one[: NAME_CHARS - 1] + "…"


def label(item: Item) -> str:
    """What the file is, in the model's words."""
    name = f" «{_name(item.name)}»" if item.name else ""
    if item.kind == "voice":
        return "voice message"
    if item.kind == "photo":
        return "photo"
    if item.kind in ("audio", "video"):
        return f"{item.kind}{name}"
    kinds = {"pdf": "PDF", "document": "document", "sheet": "spreadsheet", "text": "text file", "image": "image"}
    if item.reader in kinds:
        return f"{kinds[item.reader]}{name}"
    if item.reader in RECORDINGS:
        return f"recording{name}"
    return f"file{name}" + (f" ({item.mime})" if item.mime and _MIME.fullmatch(item.mime) else "")


def _state(item: Item) -> str | None:
    """Why the file brings no text, or None when it was read."""
    if item.reader is None:
        return "files of this kind are not read; the file is kept"
    if item.state == "failed":
        return "it could not be read; the file is kept"
    if item.state == "cancelled":
        return "its reading was cancelled; the file is kept"
    if item.state != _DONE:
        return "its text is not read yet: reading it needs what the owner installs; the file is kept"
    if not any(text.strip() for text in item.texts):
        what = "nothing was heard in it" if item.reader in RECORDINGS else "no text was found in it"
        return what + (f" ({clean(item.note)})" if item.note else "")
    return None


def _body(item: Item, budget: int) -> tuple[str, str]:
    """As much of the text as the budget allows, and what the mark says of it."""
    recording = item.reader in RECORDINGS
    limit = min(budget, TRANSCRIPT_CHARS if recording else BEGINNING_CHARS)
    count = max(item.pages, len(item.texts))
    parts: list[str] = []
    used = 0
    shown = 0
    for number, text in enumerate(item.texts, 1):
        piece = clean(text.strip())
        if item.reader == "pdf":
            piece = f"(page {number})\n{piece}"
        if used + len(piece) > limit:
            if not parts:  # one long first page: its beginning
                parts.append(piece[:limit].rstrip() + " …")
                used = limit
            break
        parts.append(piece)
        used += len(piece) + 1
        shown = number
    what = "transcript" if recording else "text"
    if shown == count:
        head = f"its {what}"
    else:
        start = shown + 1 if shown else 1
        unit = "part" if recording else "page"
        head = f"the beginning of its {what}; read the rest with {GET}, from {unit} {start} of {count}"
    if item.note:
        head += f" (partial: {clean(item.note)})"
    return "\n".join(parts), head


def attachment_text(captions: Sequence[str], items: Sequence[Item], *, forwarded_mark: str | None = None) -> str:
    """The served text of a batch: the caption, then each file between its marks."""
    lines: list[str] = []
    if forwarded_mark:
        lines.append(forwarded_mark)
    lines += [clean(caption) for caption in captions if caption.strip()]
    budget = MAX_TEXT_CHARS
    for item in items:
        prefix = f"{label(item)} · record {item.record_id}"
        why = _state(item)
        if why is not None:
            lines.append(f"{OPEN}{prefix} · {why}{CLOSE}")
            continue
        if budget <= 0:
            lines.append(f"{OPEN}{prefix} · its text is not shown here: read it with {GET}{CLOSE}")
            continue
        body, head = _body(item, budget)
        budget -= len(body)
        lines += [f"{OPEN}{prefix} · {head}:{CLOSE}", body, f"{OPEN}end of the {label(item)}{CLOSE}"]
    return "\n".join(lines)
