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
MAKE_PDF = "klepa__make_pdf"
RECORDINGS = frozenset({"voice", "video"})  # readers whose text is a transcript, kept in parts
_DONE = "done"
# A mark's brackets and the ones that look like them: no text may hold any of them.
_BRACKETS = str.maketrans({OPEN: "[", CLOSE: "]", "\u301a": "[", "\u301b": "]"})
# The delimiters of the runtime-context block that OpenClaw adds to a person's message. Core's instruction tells the
# model what the block is, so no text may hold them either; they become OpenClaw's own escaped forms (#60).
RUNTIME_CONTEXT = ("<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>", "<<<END_OPENCLAW_INTERNAL_CONTEXT>>>")
_RUNTIME_CONTEXT_ESCAPED = ("[[OPENCLAW_INTERNAL_CONTEXT_BEGIN]]", "[[OPENCLAW_INTERNAL_CONTEXT_END]]")
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


@dataclass(frozen=True)
class Draft:
    """The draft of a document that the batch's images made, or that its one photo is offered to (spec 6.1 item 4)."""

    id: int
    records: tuple[str, ...]  # its pages' records, in their order: the model's numbers for make_pdf
    revision: int  # the revision those numbers belong to
    made: bool = True  # the batch's images made it; False: the batch's one photo may be a page of it
    waits: bool = True  # the model has neither ordered it nor closed it since
    state: str = "open"
    previous: Draft | None = None  # the chat's document these images may continue (an album of a long one)


def clean(text: str) -> str:
    """No text a file or a person wrote can open or close a mark, nor the gateway's runtime-context block."""
    text = text.translate(_BRACKETS)
    for delimiter, escaped in zip(RUNTIME_CONTEXT, _RUNTIME_CONTEXT_ESCAPED, strict=True):
        text = text.replace(delimiter, escaped)
    return text


def _name(name: str) -> str:
    """A file's name as a mark may hold it: one line, no control or format character, no quote of a mark's own."""
    one = " ".join(clean(sanitize_original_name(name)).split())  # Unicode's line separators too
    one = one.replace("«", '"').replace("»", '"')
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


def attachment_text(
    captions: Sequence[str], items: Sequence[Item], *, forwarded_mark: str | None = None, draft: Draft | None = None
) -> str:
    """The served text of a batch: the caption, then each file between its marks, then the draft its images make."""
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
    if draft is not None:
        lines.append(f"{OPEN}document draft {draft.id} · {_draft(draft)}{CLOSE}")
    return "\n".join(lines)


NEVER = (
    "never name record or draft ids to the person; to name a photo, say which one it was in the order they were sent"
)


def _numbers(draft: Draft) -> str:
    return ", ".join(f"{number} record {record}" for number, record in enumerate(draft.records, 1))


def _draft(draft: Draft) -> str:
    """The owner's choice of 2026-10-27: no sheet to check; the model orders the pages, leaves out repeats, says what
    is missing, and Core sends the PDF after its answer."""
    if not draft.made:
        return (
            f"the chat's latest document, of {len(draft.records)} pages: {_numbers(draft)}; revision {draft.revision}. "
            "If the image above is a page of it, as the person's words or what it shows say (a page that was missing, "
            f"say), pass {MAKE_PDF} with the draft, revision {draft.revision}, add with the image's record, and order "
            f"with every page in the order their content shows, the image's number being {len(draft.records) + 1}; "
            "Core then sends the new PDF after your answer. Otherwise leave the draft as it is, and never pass "
            f"not_a_document for it; {NEVER}"
        )
    what = f"{len(draft.records)} pages made from the images above, in the order they came: {_numbers(draft)}"
    if draft.state == "declined":  # the review's M4: a turn retried after the model closed it
        return (
            f"{what}; it is closed as no document, and no PDF is made; {MAKE_PDF} with the draft and an order opens it"
        )
    if not draft.waits:
        if draft.state == "done":
            return f"{what}; you have ordered it, and Core sent the person its PDF; change it with {MAKE_PDF}; {NEVER}"
        if draft.state == "failed":
            return (
                f"{what}; its PDF could not be made, and the person was told; {MAKE_PDF} with the draft and an order "
                f"tries again; {NEVER}"
            )
        return f"{what}; you have ordered it already, and Core sends the PDF after your answer; {NEVER}"
    text = (
        f"{what}; revision {draft.revision}. Pass {MAKE_PDF} with the draft, revision {draft.revision} and order: the "
        "numbers of the pages in the order their content shows (how a text runs on, headings, signatures, page "
        "numbers), each page once, leaving a photo out only when its text is the same as another's (a second photo of "
        "one page); a page with little or no text is never a repeat, and when unsure, keep both. Core sends the person "
        "the PDF after your answer. In your answer say which photos you left out, if any, and if what the pages say "
        "shows a page is missing (a gap in the page numbers, a text that breaks off), say which, so the person can "
        "send it. If the images show people, places or things, and the person's words do not ask for a document, pass "
        f"not_a_document with revision {draft.revision} instead, and no PDF is made; little or no text is no sign of "
        "that, and when unsure, make the PDF"
    )
    if draft.previous is not None:
        previous = draft.previous
        text += (
            f". If instead these images continue the chat's latest document, draft {previous.id} of "
            f"{len(previous.records)} pages ({_numbers(previous)}; revision {previous.revision}), as their content or "
            f"the person's words say, pass {MAKE_PDF} with draft {previous.id}, revision {previous.revision}, add with "
            "these images' records, and order with every page, these numbered after its pages; this draft then makes "
            "nothing"
        )
    return f"{text}; {NEVER}"
