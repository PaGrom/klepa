"""The adapter plugin's link to Core (spec 4.5, 4.6): signed messages over a Unix socket.

A message is one JSON object, POSTed to /v1/message. It is signed with HMAC-SHA256 over its exact bytes, with a
0600 key that never goes into snapshots, and it carries (boot_id, seq). Core answers with a signed body that
echoes the pair. Order guards against confusion, not attacks: the signature and the duplicate check are the
security (spec 4.5).

Messages: `heartbeat` every 5 s; `dispatch` from before_dispatch; `prompt_built` from before_prompt_build;
`turn_start` from before_agent_run. In stage 1 before_dispatch answers people itself and every turn is blocked;
the live probe is the one message before_dispatch lets through (spec 6.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from ..events import EventLog
from ..keys import ensure_private_dir
from ..locale import Locale
from .queue import HostQueue
from .supervisor import PROBE_BLOCK, Supervisor
from .turns import Turn, TurnRegistry

SIGNATURE_HEADER = "X-Klepa-Signature"
MAX_MESSAGE_BYTES = 64 * 1024
REJECTED_LOG_SECONDS = 60.0
SEQ_WINDOW = 256
_BOOT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
log = logging.getLogger("klepa_core")


def sign_body(key: bytes, body: bytes) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def probe_peer(key: bytes) -> int:
    """A Telegram-shaped user id no member has: the sender and chat of the live probe (spec 4.6). It lies in
    [2**51, 2**52), inside the 52 bits Telegram ids use and far above real ones, and it is the same on every start."""
    digest = hmac.new(key, b"klepa probe peer", hashlib.sha256).digest()
    return 2**51 + int.from_bytes(digest[:8], "big") % 2**51


def as_id(value: object) -> int | None:
    """A Telegram id as the host's hook context gives it: a number or a string of digits."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,20}", value):
        return int(value)
    return None


class BootSequence:
    """Within a boot each seq passes once, and one far behind the newest never does: messages sent at the same moment
    may arrive out of order, a repeat never passes. A boot that another replaced is never accepted again."""

    def __init__(self, keep: int = 16, window: int = SEQ_WINDOW) -> None:
        self.boot_id: str | None = None
        self.window = window
        self._top = -1
        self._seen: set[int] = set()
        self._retired: deque[str] = deque(maxlen=keep)

    def accept(self, boot_id: str, seq: int) -> bool:
        if boot_id != self.boot_id:
            if boot_id in self._retired:
                return False
            if self.boot_id is not None:
                self._retired.append(self.boot_id)
            self.boot_id, self._top, self._seen = boot_id, -1, set()
        if seq in self._seen or seq <= self._top - self.window:
            return False
        self._seen.add(seq)
        self._top = max(self._top, seq)
        if len(self._seen) > 2 * self.window:
            self._seen = {old for old in self._seen if old > self._top - self.window}
        return True


class AdapterServer:
    def __init__(
        self,
        path: Path,
        key: bytes,
        supervisor: Supervisor,
        turns: TurnRegistry,
        queue: HostQueue,
        events: EventLog,
        locale: Locale,
        *,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self.key = key
        self.supervisor = supervisor
        self.turns = turns
        self.queue = queue
        self.events = events
        self.locale = locale
        self.mono = mono
        self.boots = BootSequence()
        self._rejected_at: dict[str, float] = {}

    async def serve_forever(self, stop: asyncio.Event) -> None:
        ensure_private_dir(self.path.parent)
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISSOCK(self.path.lstat().st_mode):
                self.path.unlink()  # left by a Core that stopped; never anything but a socket
        app = web.Application(client_max_size=MAX_MESSAGE_BYTES)
        app.router.add_post("/v1/message", self._handle)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.0)
        await runner.setup()
        try:
            await web.UnixSite(runner, str(self.path)).start()
            os.chmod(self.path, 0o600)
            await stop.wait()
        finally:
            await runner.cleanup()

    def _rejected(self, reason: str) -> None:
        now = self.mono()
        if now - self._rejected_at.get(reason, float("-inf")) >= REJECTED_LOG_SECONDS:
            self._rejected_at[reason] = now
            self.events.log("adapter_rejected", {"reason": reason})
            log.warning("adapter message rejected: %s", reason)

    def _reply(self, status: int, body: dict[str, Any]) -> web.Response:
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
        return web.Response(
            body=raw,
            status=status,
            content_type="application/json",
            headers={SIGNATURE_HEADER: sign_body(self.key, raw)},
        )

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        signature = request.headers.get(SIGNATURE_HEADER, "").encode("utf-8", "replace")
        if not hmac.compare_digest(signature, sign_body(self.key, body).encode()):
            self._rejected("signature")  # before anything else: an unsigned message changes nothing
            return self._reply(401, {"ok": False, "error": "signature"})
        try:
            message = json.loads(body)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            self._rejected("malformed")
            return self._reply(400, {"ok": False, "error": "malformed"})
        kind, boot_id, seq = message.get("type"), message.get("boot_id"), message.get("seq")
        handler = {
            "heartbeat": self._heartbeat,
            "dispatch": self._dispatch,
            "prompt_built": self._prompt_built,
            "turn_start": self._turn_start,
        }.get(kind if isinstance(kind, str) else "")
        if (
            handler is None
            or not isinstance(boot_id, str)
            or not _BOOT_ID.match(boot_id)
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < 0
        ):
            self._rejected("malformed")
            return self._reply(400, {"ok": False, "error": "malformed"})
        if not self.boots.accept(boot_id, seq):
            self._rejected("sequence")
            return self._reply(409, {"ok": False, "error": "sequence", "boot_id": boot_id, "seq": seq})
        return self._reply(200, {**handler(boot_id, message), "boot_id": boot_id, "seq": seq})

    def _heartbeat(self, boot_id: str, message: dict[str, Any]) -> dict[str, Any]:
        problems = self.supervisor.on_heartbeat(boot_id, message)
        return {"ok": not problems, "problems": problems}

    def _dispatch(self, boot_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """Stage 1: Core's fixed answer for people; the live probe goes on to a turn (spec 6.1)."""
        if as_id(message.get("sender_id")) == self.queue.probe_peer:
            return {"handled": False}
        return {"handled": True, "text": self.locale.text("stage1")}

    def _prompt_built(self, boot_id: str, message: dict[str, Any]) -> dict[str, Any]:
        run_id = message.get("run_id")
        if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
            return {"ok": False}
        self.turns.prompt_built(run_id, boot_id, as_id(message.get("chat_id")), as_id(message.get("sender_id")))
        return {"ok": True}

    def _turn_start(self, boot_id: str, message: dict[str, Any]) -> dict[str, Any]:
        run_id = message.get("run_id")
        chat_id, sender_id = as_id(message.get("chat_id")), as_id(message.get("sender_id"))
        if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
            turn = Turn(False, "no_run_id")
        else:
            turn = self.turns.start(run_id, boot_id, chat_id, sender_id, message.get("session_key"))
        self.events.log(
            "turn_registered" if turn.registered else "turn_refused",
            {"reason": turn.reason, "chat_id": chat_id, "probe": turn.probe},
        )
        if turn.registered and turn.probe:
            self.supervisor.on_probe_turn(turn.host_message_id)
            text = PROBE_BLOCK
        elif self.supervisor.turns_blocked():
            text = self.locale.text("hold")
        else:
            text = self.locale.text("stage1")
        return {"outcome": "block", "message": text}  # stage 1: no turn reaches the model
