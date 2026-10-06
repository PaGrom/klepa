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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import transaction
from ..gatekeeper.outbox import Outbox
from ..names import sanitize_original_name
from ..signing import canonical_json

TOOL_DOMAIN = b"klepa-tool-call-v1"
MAX_CALL_BYTES = 16 * 1024
SEARCH_LIMIT = 10
SEARCH_MAX = 50
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SIG = re.compile(r"[0-9a-f]{64}")
_MIME = re.compile(r"[\w.+-]{1,64}/[\w.+-]{1,128}")
_READABLE = ("stored", "processing", "processed", "failed", "too_large")  # what get reads and search lists


class ToolRefused(Exception):
    """A call Core does not run. Its text goes back to the model and never carries content."""


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
            raise ToolRefused(f"unknown parameters: {', '.join(unknown)}")
        missing = [name for name in self.required if name not in params]
        if missing:
            raise ToolRefused(f"missing parameters: {', '.join(missing)}")
        for name, value in params.items():
            kind = self.properties[name]["type"]
            ok = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool)}
            if not ok.get(kind, False):
                raise ToolRefused(f"{name} must be a {kind}")
        return dict(params)


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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.outbox = outbox  # the family bot's: Core sends originals itself (spec 6.5)
        self.incoming_dir = incoming_dir  # where the originals are; evidence keeps paths relative to it
        self.conn = conn
        self.key = key
        self.members = members  # telegram id -> person id
        self.names = names  # person id -> name
        self.clock = clock
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
                "SELECT chat_id, sender_id FROM host_run WHERE run_id=? AND host_message_id IS NOT NULL "
                "AND expires_at > ?",
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
        return Caller(run_id, person, int(run["chat_id"]), ("shared", f"personal:{person}"))


def _stems(query: str) -> list[str]:
    """The query's words by their beginnings, so that another form of a word still matches ("insurances" finds
    "insurance"; Russian or Serbian endings likewise). Words of one or two letters count only when there are no
    others."""
    words = re.findall(r"\w+", query.casefold())[:8]
    words = [word for word in words if len(word) >= 3] or words
    return sorted({word if len(word) <= 4 else word[: max(4, len(word) - 2)] for word in words})


def _search(box: ToolBox, caller: Caller, params: dict[str, Any]) -> Any:
    stems = _stems(params.get("query", ""))
    limit = min(max(params.get("limit", SEARCH_LIMIT), 1), SEARCH_MAX)
    marks = ",".join("?" * len(caller.spaces))
    rows = box.conn.execute(
        f"SELECT id, kind, original_name, caption, received_at, authenticated_subject FROM evidence "
        f"WHERE space_id IN ({marks}) AND state IN ({','.join('?' * len(_READABLE))}) ORDER BY received_at DESC",
        (*caller.spaces, *_READABLE),
    ).fetchall()
    scored = []
    for row in rows:
        text = " ".join(filter(None, (row["original_name"], row["caption"]))).casefold()
        score = sum(len(stem) for stem in stems if stem in text)  # a longer word that matches weighs more
        if score or not stems:  # without words: the latest records
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)  # stable: equal scores keep the newest first
    found = [
        {
            "id": row["id"],
            "kind": row["kind"],
            "name": row["original_name"],
            "caption": row["caption"],
            "date": row["received_at"][:10],
            "from": box.names.get(row["authenticated_subject"], row["authenticated_subject"]),
        }
        for _, row in scored[:limit]
    ]
    return {"results": found}


_SENDABLE = ("stored", "processing", "processed")


def _record(box: ToolBox, caller: Caller, record_id: str) -> sqlite3.Row:
    marks = ",".join("?" * len(caller.spaces))
    row: sqlite3.Row | None = box.conn.execute(
        f"SELECT * FROM evidence WHERE id=? AND space_id IN ({marks}) AND state IN ({','.join('?' * len(_READABLE))})",
        (record_id, *caller.spaces, *_READABLE),
    ).fetchone()
    if row is None:
        raise ToolRefused("no such record")  # whether it exists in a space the person may not see is not told
    return row


def _get(box: ToolBox, caller: Caller, params: dict[str, Any]) -> Any:
    row = _record(box, caller, params["id"])
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["original_name"],
        "caption": row["caption"],
        "date": row["received_at"][:10],
        "from": box.names.get(row["authenticated_subject"], row["authenticated_subject"]),
        "mime": row["mime"],
        "size": row["size"],
        "state": row["state"],
        "space": "shared" if row["space_id"] == "shared" else "personal",
    }


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
    return {"sent": True, "name": document["name"]}


TOOLS = (
    Tool(
        "search",
        "Find the family's stored records (files, photos, voice messages) by words in their names and captions. "
        "A word matches by its beginning, so other forms of it are found; records that match more words come first. "
        "Without words: the latest records.",
        {
            "query": {"type": "string", "description": "A few key words, or nothing for the latest records."},
            "limit": {"type": "integer", "description": "How many records, 1 to 50; 10 when not given."},
        },
        (),
        _search,
    ),
    Tool("get", "Read one stored record by its id.", {"id": {"type": "string"}}, ("id",), _get),
    Tool(
        "send_original",
        "Send the person the original file of a record, with the name it came with.",
        {"id": {"type": "string"}},
        ("id",),
        _send_original,
    ),
)
