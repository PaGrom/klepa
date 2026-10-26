"""Core's tools for the model (spec 6.2, 10), reached through the MCP shim of the adapter's package.

The shim has no key. Each call carries `_klepa`: the turn's run, the call's id and an HMAC that the adapter's
before_tool_call computed over exactly these parameters (spec 4.5). Core checks the signature, that the turn is
registered and live, and that the call id was never used, and only then runs the tool, for the person whose message
the turn answers. A signature copied from the model's history is useless: it fits only its own call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..db import transaction
from ..gatekeeper.drafts import DraftRefused
from ..gatekeeper.outbox import Outbox
from ..names import sanitize_original_name
from ..processing.jobs import KINDS
from ..search import find
from ..signing import canonical_json
from .served import clean

if TYPE_CHECKING:
    from ..gatekeeper.drafts import Drafts

TOOL_DOMAIN = b"klepa-tool-call-v1"
MAX_CALL_BYTES = 16 * 1024
SEARCH_LIMIT = 10
SEARCH_MAX = 50
GET_CHARS = 10_000  # what get returns of one page: a dense page fits, a longer one is cut and says so
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SIG = re.compile(r"[0-9a-f]{64}")
_MIME = re.compile(r"[\w.+-]{1,64}/[\w.+-]{1,128}")
_READABLE = ("stored", "processing", "processed", "failed", "too_large")  # what get reads and search lists


class ToolRefused(Exception):
    """A call Core does not run. Its text goes back to the model and never carries a record's content; its reason,
    which Core's event log keeps, never carries what the model wrote either."""

    def __init__(self, text: str, reason: str | None = None) -> None:
        super().__init__(text)
        self.reason = reason or text


def signature(key: bytes, run_id: str, call_id: str, tool: str, params: Mapping[str, Any]) -> str:
    signed = b"\n".join([TOOL_DOMAIN, run_id.encode(), call_id.encode(), tool.encode(), canonical_json(params)])
    return hmac.new(key, signed, hashlib.sha256).hexdigest()


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key appears twice")
    return dict(pairs)


def _no_float(text: str) -> Any:
    raise ValueError("numbers must be integers")


def parse_call(body: bytes) -> tuple[str, dict[str, Any]]:
    """The shim's call: {"name": ..., "arguments": {...}}. A key twice anywhere, or a fraction, refuses it."""
    if len(body) > MAX_CALL_BYTES:
        raise ToolRefused("the call is too large")
    try:
        call = json.loads(body, object_pairs_hook=_no_duplicates, parse_float=_no_float, parse_constant=_no_float)
    except ValueError as exc:
        raise ToolRefused(f"the call is not valid JSON: {exc}") from None
    except RecursionError:
        raise ToolRefused("the call is nested too deeply") from None
    if (
        not isinstance(call, dict)
        or not isinstance(call.get("name"), str)
        or not isinstance(call.get("arguments"), dict)
    ):
        raise ToolRefused("the call must name a tool and give its arguments")
    return call["name"], call["arguments"]


@dataclass(frozen=True)
class Caller:
    run_id: str
    person_id: str
    chat_id: int
    spaces: tuple[str, ...]
    message_id: int | None = None  # the person's message the turn answers


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    required: tuple[str, ...]
    run: Callable[[ToolBox, Caller, dict[str, Any]], Any]

    def spec(self) -> dict[str, Any]:
        schema = {
            "type": "object",
            "properties": self.properties,
            "required": list(self.required),
            "additionalProperties": False,
        }
        return {"name": self.name, "description": self.description, "inputSchema": schema}

    def check(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Exactly the declared parameters, of their types: unknown keys are refused, not dropped."""
        unknown = sorted(set(params) - set(self.properties))
        if unknown:
            raise ToolRefused(f"unknown parameters: {', '.join(unknown)}", reason="unknown parameters")
        missing = [name for name in self.required if name not in params]
        if missing:
            raise ToolRefused(f"missing parameters: {', '.join(missing)}")
        for name, value in params.items():
            schema = self.properties[name]
            if not _typed(value, schema):
                kind = schema["type"]
                what = f"a list of {schema['items']['type']}s" if kind == "array" else f"a {kind}"
                raise ToolRefused(f"{name} must be {what}")
        return dict(params)


def _typed(value: Any, schema: Mapping[str, Any]) -> bool:
    kind = schema["type"]
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list) and all(_typed(item, schema["items"]) for item in value)
    return False


class ToolBox:
    def __init__(
        self,
        conn: sqlite3.Connection,
        key: bytes,
        members: Mapping[int, str],
        names: Mapping[str, str],
        outbox: Outbox | None = None,
        incoming_dir: Path | None = None,
        *,
        readable: Callable[[], Collection[str]] = lambda: KINDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.outbox = outbox  # the family bot's: Core sends originals itself (spec 6.5)
        self.readable = readable  # the kinds of original Core reads now: a queued job of another kind waits
        self.incoming_dir = incoming_dir  # where the originals are; evidence keeps paths relative to it
        self.conn = conn
        self.key = key
        self.members = members  # telegram id -> person id
        self.names = names  # person id -> name
        self.clock = clock
        self.drafts: Drafts | None = None  # documents from photos (make_pdf): None where Core makes none
        self.tools = {tool.name: tool for tool in TOOLS}

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self.tools.values()]

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolRefused("no such tool")
        params = dict(arguments)
        klepa = params.pop("_klepa", None)
        caller = self._authorize(name, klepa, params)
        return tool.run(self, caller, tool.check(params))

    def _authorize(self, name: str, klepa: object, params: dict[str, Any]) -> Caller:
        if not isinstance(klepa, dict) or set(klepa) != {"run_id", "tool_call_id", "sig"}:
            raise ToolRefused("the call is not signed")
        run_id, call_id, sig = klepa["run_id"], klepa["tool_call_id"], klepa["sig"]
        if not all(isinstance(value, str) for value in (run_id, call_id, sig)):
            raise ToolRefused("the call is not signed")
        if not _ID.fullmatch(run_id) or not _ID.fullmatch(call_id) or call_id.startswith("http-"):
            raise ToolRefused("the call is not signed")  # /tools/invoke gives ids "http-…" and has no turn
        if not _SIG.fullmatch(sig):
            raise ToolRefused("the call is not signed")
        try:
            expected = signature(self.key, run_id, call_id, name, params)
        except (TypeError, ValueError, UnicodeError):
            raise ToolRefused("the call is not signed") from None
        if not hmac.compare_digest(expected, sig):
            raise ToolRefused("the call is not signed")
        now = self.clock()
        with transaction(self.conn):
            run = self.conn.execute(
                "SELECT r.chat_id, r.sender_id, m.message_id FROM host_run r LEFT JOIN host_message m "
                "ON m.id = r.host_message_id WHERE r.run_id=? AND r.host_message_id IS NOT NULL AND r.expires_at > ?",
                (run_id, now),
            ).fetchone()
            if run is None:
                raise ToolRefused("the turn is not registered")
            used = self.conn.execute(
                "INSERT INTO host_tool_call(run_id, tool_call_id, tool, used_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (run_id, call_id, name, now),
            )
            if used.rowcount != 1:
                raise ToolRefused("the call was made already")
        person = self.members.get(int(run["sender_id"]))
        if person is None:
            raise ToolRefused("the turn is not a member's")
        message_id = None if run["message_id"] is None else int(run["message_id"])
        return Caller(run_id, person, int(run["chat_id"]), ("shared", f"personal:{person}"), message_id)


def _stems(query: str) -> list[str]:
    """The query's words by their beginnings, so that another form of a word still matches ("insurances" finds
    "insurance"; Russian or Serbian endings likewise): a word keeps all but its last two letters, three at least, so
    that a short word loses its ending too ("cars" finds "car"; found in the live run of 2b-1). Words of one or two
    letters count only when there are no others."""
    words = re.findall(r"\w+", query.casefold())[:8]
    words = [word for word in words if len(word) >= 3] or words
    return sorted({word if len(word) <= 3 else word[: max(3, len(word) - 2)] for word in words})


def _search(box: ToolBox, caller: Caller, params: dict[str, Any]) -> Any:
    stems = _stems(params.get("query", ""))
    limit = min(max(params.get("limit", SEARCH_LIMIT), 1), SEARCH_MAX)
    marks = ",".join("?" * len(caller.spaces))
    states = ",".join("?" * len(_READABLE))
    if stems:
        hits = find(box.conn, caller.spaces, _READABLE, stems, limit)
        ids = [hit.evidence_id for hit in hits]
    else:  # without words: the latest records
        latest = box.conn.execute(
            f"SELECT id FROM evidence WHERE space_id IN ({marks}) AND state IN ({states}) "
            "ORDER BY received_at DESC LIMIT ?",
            (*caller.spaces, *_READABLE, limit),
        ).fetchall()
        hits = []
        ids = [row["id"] for row in latest]
    rows = {
        row["id"]: row
        for row in box.conn.execute(
            f"SELECT id, kind, original_name, caption, received_at, authenticated_subject FROM evidence "
            f"WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
    }
    pages = {hit.evidence_id: hit for hit in hits if hit.page > 0}
    found = []
    for record_id in ids:
        row = rows[record_id]
        result = {
            "id": row["id"],
            "kind": row["kind"],
            "name": _clean(row["original_name"]),
            "caption": _clean(row["caption"]),
            "date": row["received_at"][:10],
            "from": box.names.get(row["authenticated_subject"], row["authenticated_subject"]),
        }
        version = _version(box, record_id)
        if version is not None:  # a document Core made from photos: which version of it
            result["version"] = version
        if record_id in pages:  # found by its text: which page, and the words around
            result |= {"page": pages[record_id].page, "snippet": _clean(pages[record_id].snippet)}
        found.append(result)
    return {"results": found}


def _version(box: ToolBox, record_id: str) -> int | None:
    row = box.conn.execute("SELECT version FROM document_version WHERE evidence_id=?", (record_id,)).fetchone()
    return None if row is None else int(row[0])


def _clean(text: str | None) -> str | None:
    """What a file or a person wrote, without the brackets of Core's marks (served.clean)."""
    return None if text is None else clean(text)


_SENDABLE = ("stored", "processing", "processed", "failed")  # failed: its text, never its file


def _record(box: ToolBox, caller: Caller, record_id: str) -> sqlite3.Row:
    marks = ",".join("?" * len(caller.spaces))
    row: sqlite3.Row | None = box.conn.execute(
        f"SELECT * FROM evidence WHERE id=? AND space_id IN ({marks}) AND state IN ({','.join('?' * len(_READABLE))})",
        (record_id, *caller.spaces, *_READABLE),
    ).fetchone()
    if row is None:
        raise ToolRefused("no such record")  # whether it exists in a space the person may not see is not told
    return row


_TEXT_STATUS = {"done": "read", "queued": "reading", "running": "reading", "failed": "failed", "cancelled": "none"}


def _text_status(box: ToolBox, job: sqlite3.Row | None) -> str:
    if job is None:
        return "none"
    if job["state"] == "queued" and job["kind"] not in box.readable():
        return "waiting"  # for what reading it needs: a program, the speech model (finding I8)
    return _TEXT_STATUS[job["state"]]


def _get(box: ToolBox, caller: Caller, params: dict[str, Any]) -> Any:
    row = _record(box, caller, params["id"])
    record: dict[str, Any] = {
        "id": row["id"],
        "kind": row["kind"],
        "name": _clean(row["original_name"]),
        "caption": _clean(row["caption"]),
        "date": row["received_at"][:10],
        "from": box.names.get(row["authenticated_subject"], row["authenticated_subject"]),
        "mime": _clean(row["mime"]),  # as the sender's app gave it
        "size": row["size"],
        "state": row["state"],
        "space": "shared" if row["space_id"] == "shared" else "personal",
    }
    version = _version(box, row["id"])
    if version is not None:
        record["version"] = version
    job = box.conn.execute("SELECT kind, state, pages, note FROM job WHERE evidence_id=?", (row["id"],)).fetchone()
    record["text_status"] = _text_status(box, job)
    if record["text_status"] != "read":
        if "page" in params:
            raise ToolRefused("the record has no text to read")
        return record
    pages = int(job["pages"] or 0)
    record["pages"] = pages
    if job["note"]:
        record["partial"] = clean(job["note"])  # what a limit left out of the text (finding I6)
    page = params.get("page", 1)
    if pages == 0 and "page" not in params:
        return record  # read, and nothing was found in it: a silent recording, a blank scan
    if not 1 <= page <= pages:
        raise ToolRefused(f"no page {page}: the record has {pages}")
    text_row = box.conn.execute(
        "SELECT text FROM evidence_text WHERE evidence_id=? AND page=?", (row["id"], page)
    ).fetchone()
    text = "" if text_row is None else str(text_row["text"])  # a page without text has no row
    record |= {"page": page, "text": clean(text[:GET_CHARS])}
    if len(text) > GET_CHARS:
        record["cut"] = True
    return record


def _send_original(box: ToolBox, caller: Caller, params: dict[str, Any]) -> Any:
    row = _record(box, caller, params["id"])
    if row["state"] not in _SENDABLE or not row["incoming_path"] or box.outbox is None or box.incoming_dir is None:
        raise ToolRefused("this record has no file to send")
    # The name and the type came from the sender's device: a control character in either would stop the upload.
    mime = row["mime"] if row["mime"] and _MIME.fullmatch(row["mime"]) else "application/octet-stream"
    document = {
        "evidence_id": row["id"],
        "path": str(box.incoming_dir / row["incoming_path"]),
        "sha256": row["sha256"],
        "size": row["size"],
        "mime": mime,
        "name": sanitize_original_name(row["original_name"] or row["disk_name"]),
    }
    box.outbox.enqueue_document(f"original:{caller.run_id}:{row['id']}", caller.chat_id, document)
    return {"sent": True, "name": clean(document["name"])}  # the file keeps its name; the model reads it clean


def _make_pdf(box: ToolBox, caller: Caller, params: dict[str, Any]) -> Any:
    if box.drafts is None:
        raise ToolRefused("documents from photos are not made here")
    try:
        return box.drafts.change(
            caller.person_id,
            caller.chat_id,
            caller.spaces,
            draft_id=params.get("draft"),
            order=params.get("order"),
            add=params.get("add"),
            reply_to=caller.message_id,
            revision=params.get("revision"),
            not_a_document=bool(params.get("not_a_document", False)),
        )
    except DraftRefused as exc:
        raise ToolRefused(str(exc)) from None


TOOLS = (
    Tool(
        "search",
        "Find the family's stored records (files, photos, voice messages) by words in their names, captions and "
        "texts: what a document says, what was said in a voice message, what is written on a photo. A word matches "
        "by its beginning, so other forms of it are found; the best matches come first. A record found by its text "
        "names the page and quotes the words around. Without words: the latest records.",
        {
            "query": {"type": "string", "description": "A few key words, or nothing for the latest records."},
            "limit": {"type": "integer", "description": "How many records, 1 to 50; 10 when not given."},
        },
        (),
        _search,
    ),
    Tool(
        "get",
        "Read one stored record by its id: what it is, and its text page by page. text_status says whether the text "
        "is there: read (pages says how many; the first is returned unless you ask for another; partial, when given, "
        "says what part of the original the text leaves out), reading (it is being read: usually seconds, long "
        "recordings and scans take minutes), waiting (it is not read until the owner installs what reading it "
        "needs), failed (it could not be read; the original is kept and can be sent), none (a file of this kind is "
        "not read).",
        {
            "id": {"type": "string"},
            "page": {"type": "integer", "description": "Which page of the text, from 1; 1 when not given."},
        },
        ("id",),
        _get,
    ),
    Tool(
        "send_original",
        "Send the person the original file of a record, with the name it came with.",
        {"id": {"type": "string"}},
        ("id",),
        _send_original,
    ),
    Tool(
        "make_pdf",
        "Change a person's document draft, or start one from photos. Two or more photos sent at once make a draft: "
        "Core sends the person a sheet of the pages as numbered thumbnails with the buttons Done, Change the order "
        "and Not a document, and makes the PDF from the originals only when they press Done; a photo they send alone "
        "soon after joins it. When Core's mark names a draft its images made or joined, before you answer, put the "
        "pages in the order their content shows (how a text runs on, headings, signatures, page numbers), if it "
        "differs: the sheet goes out once you have answered; if they are no document, pass not_a_document instead, "
        "and no sheet goes out. When the person asks in words to reorder, drop or add "
        "pages, call this with the draft: order lists the pages to keep by their numbers as the draft is now, in the "
        "new order; add lists records of more photos, put after them. Without a draft, add starts a new one. Core "
        "then sends a new sheet to check. A PDF over 50 MB is not made: if the person says so, split the pages into "
        "two drafts. Never show the draft's id.",
        {
            "draft": {"type": "integer", "description": "The draft to change; none for a new one."},
            "order": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Page numbers as the draft is now (in Core's mark, or on the latest sheet), in the new "
                "order; pages left out are dropped.",
            },
            "revision": {
                "type": "integer",
                "description": "The revision your page numbers belong to, as Core's mark names it: a draft changed "
                "since is refused with its pages as they are.",
            },
            "add": {"type": "array", "items": {"type": "string"}, "description": "Record ids of photos to add."},
            "not_a_document": {
                "type": "boolean",
                "description": "True when the draft's images are no document (people, places, things; no text, or "
                "text only by chance): the draft closes, and a sheet not sent yet never goes out. Alone, with the "
                "draft and its revision.",
            },
        },
        (),
        _make_pdf,
    ),
)
