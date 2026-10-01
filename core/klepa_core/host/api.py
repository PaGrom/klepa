"""The gatekeeper's Telegram-shaped API for the host (spec 4.2, 4.6; scenario 44).

The host holds a fake token and reaches the family bot only through here. The interface is an exact allow-list:
- getUpdates and getMe;
- deleteWebhook, deleteMyCommands and setMyCommands, answered here and never passed on;
- sendChatAction;
- sendMessage with chat_id, text, parse_mode and a forced link_preview_options only; everything else is dropped.

Everything else, editing, deleting, pinning, reactions, copies, forwards, files, is refused. The port listens on
loopback, checks the Host header exactly and refuses browser requests (Origin, Sec-Fetch-*), so no web page can
reach it through DNS rebinding. It takes a limited number of connections, so a local process holding idle ones
can starve the host but never Core. It has no control paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import re
import time
from collections.abc import Callable, Collection
from html import escape
from typing import Any

from aiohttp import web

from ..alerts import Alerts
from ..events import EventLog
from ..telegram.client import Ambiguous, BotApi, NotSent, TelegramError
from .outbox import MAX_WAITING_PER_CHAT, HostOutbox
from .queue import HostQueue
from .sanitize import MAX_TEXT_UNITS, sanitize_html
from .supervisor import PROBE_BLOCK, Supervisor

ALLOWED_METHODS = frozenset(
    {"getupdates", "getme", "deletewebhook", "deletemycommands", "setmycommands", "sendchataction", "sendmessage"}
)
LOCAL_METHODS = frozenset({"deletewebhook", "deletemycommands", "setmycommands"})
MAX_BODY_BYTES = 64 * 1024
MAX_SOURCE_CHARS = 4 * MAX_TEXT_UNITS  # markup and entities included; checked before the text is parsed
MAX_POLL_SECONDS = 50
MAX_CONNECTIONS = 32  # the host keeps a few
REFUSED_LOG_SECONDS = 60.0
TYPING_EVERY_SECONDS = 4.0  # Telegram shows "typing" for about five seconds
_PATH = re.compile(r"^/bot(?P<token>[^/]+)/(?P<method>[^/]+)$")
_METHOD_NAME = re.compile(r"^[A-Za-z]{1,64}$")
_NUMBER = re.compile(r"-?\d{1,20}")
_PROBE_MESSAGE_ID = 1  # the probe's own chat never reaches Telegram


class BadParams(Exception):
    """A request refused with 400. Its text is Telegram-shaped and never carries content."""


def _error(code: int, description: str) -> web.Response:
    return web.json_response({"ok": False, "error_code": code, "description": description}, status=code)


def _ok(result: Any) -> web.Response:
    return web.json_response({"ok": True, "result": result})


class _Refuse(asyncio.Protocol):
    """A connection beyond the limit: closed at once."""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        transport.close()


def _int(value: object, name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
        return int(value)
    raise BadParams(f"{name} must be a number")


def _gone(request: web.Request) -> bool:
    return request.transport is None or request.transport.is_closing()


async def _params(request: web.Request) -> dict[str, Any]:
    """Telegram takes parameters in the query, as JSON or as a form; files are refused."""
    params: dict[str, Any] = dict(request.query)
    if not request.can_read_body:
        return params
    if request.content_type == "application/json":
        try:
            body = await request.json()
        except ValueError:
            raise BadParams("the body is not JSON") from None
        if not isinstance(body, dict):
            raise BadParams("the body must be a JSON object")
        params.update(body)
    elif request.content_type == "application/x-www-form-urlencoded":
        params.update({key: value for key, value in (await request.post()).items() if isinstance(value, str)})
    else:
        raise BadParams("files are not allowed")
    return params


class HostApi:
    def __init__(
        self,
        token: str,
        port: int,
        members: Collection[int],
        queue: HostQueue,
        supervisor: Supervisor,
        outbox: HostOutbox,
        api: BotApi,
        events: EventLog,
        *,
        alerts: Alerts | None = None,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.token = token
        self.port = port
        self.members = frozenset(members)
        self.queue = queue
        self.supervisor = supervisor
        self.outbox = outbox
        self.api = api
        self.events = events
        self.alerts = alerts
        self.clock = clock
        self.mono = mono
        self._runner: web.AppRunner | None = None
        self._listener: asyncio.Server | None = None
        self._closing = False
        self._poll: web.Request | None = None
        self._me: dict[str, Any] | None = None
        self._refused_at: dict[str, float] = {}
        self._typing_at: dict[int, float] = {}

    async def start(self) -> None:
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        # No access log: it would write the token. A short shutdown: a long poll must not hold Core's stop.
        runner = web.AppRunner(app, access_log=None, keepalive_timeout=75.0, shutdown_timeout=1.0)
        await runner.setup()
        server = runner.server
        assert server is not None

        def connection() -> asyncio.BaseProtocol:
            return _Refuse() if len(server.connections) >= MAX_CONNECTIONS else server()

        try:
            self._listener = await asyncio.get_running_loop().create_server(connection, "127.0.0.1", self.port)
        except OSError:
            await runner.cleanup()
            raise
        self._runner = runner
        self._closing = False
        self.port = int(self._listener.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        self._closing = True
        self.queue.wake()  # a waiting long poll returns at once
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if listener is not None:
            listener.close_clients()
            await listener.wait_closed()

    async def serve_forever(self, stop: asyncio.Event) -> None:
        await self.start()
        try:
            await stop.wait()
        finally:
            await self.stop()

    # ---- the door ------------------------------------------------------------------------------------------------
    def _refusal(self, request: web.Request) -> str | None:
        if request.headers.get("Host") != f"127.0.0.1:{self.port}":
            return "host"
        if "Origin" in request.headers:
            return "origin"
        if any(name.lower().startswith("sec-fetch-") for name in request.headers):
            return "browser"
        return None

    def _refused(self, reason: str) -> None:
        now = self.mono()
        if now - self._refused_at.get(reason, float("-inf")) >= REFUSED_LOG_SECONDS:
            self._refused_at[reason] = now
            self.events.log("host_request_refused", {"reason": reason})

    def _log_call(self, method: str, status: int) -> None:
        """Every call is an event; a refusal at most once a minute per method and status, so no caller floods
        the log."""
        name = method if _METHOD_NAME.match(method) else "invalid"
        if status >= 400:
            key, now = f"{name}:{status}", self.mono()
            if now - self._refused_at.get(key, float("-inf")) < REFUSED_LOG_SECONDS:
                return
            if len(self._refused_at) > 1000:
                self._refused_at.clear()
            self._refused_at[key] = now
        self.events.log("host_call", {"method": name, "status": status})

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        refusal = self._refusal(request)
        if refusal is not None:
            self._refused(refusal)
            return _error(403, "Forbidden")
        match = _PATH.match(request.path)
        if match is None:
            return _error(404, "Not Found")
        if not hmac.compare_digest(match["token"].encode(), self.token.encode()):
            self._refused("token")
            return _error(401, "Unauthorized")
        method = match["method"]
        name = method.lower()
        if name not in ALLOWED_METHODS:
            self._log_call(method, 400)
            return _error(400, "Bad Request: the gatekeeper does not allow this method")
        try:
            params = await _params(request)
            if name == "getupdates":
                return await self._get_updates(request, params)
            if name in LOCAL_METHODS:
                response = _ok(True)
            elif name == "getme":
                response = await self._get_me()
            elif name == "sendchataction":
                response = await self._send_chat_action(params)
            else:
                response = self._send_message(params)
        except BadParams as exc:
            response = _error(400, f"Bad Request: {exc}")
        except ValueError:  # an unreadable form or an undecodable body
            response = _error(400, "Bad Request: the parameters cannot be read")
        self._log_call(method, response.status)
        return response

    # ---- methods -------------------------------------------------------------------------------------------------
    async def _get_me(self) -> web.Response:
        if self._me is None:
            try:
                self._me = await self.api.get_me()
            except (TelegramError, NotSent, Ambiguous):
                return _error(502, "Bad Gateway: Telegram does not answer")
        return _ok(self._me)

    async def _get_updates(self, request: web.Request, params: dict[str, Any]) -> web.StreamResponse:
        if self._poll is not None and not _gone(self._poll):
            if self.alerts is not None:
                self.alerts.raise_("host_conflict")
            self._log_call("getUpdates", 409)
            return _error(409, "Conflict: terminated by other getUpdates request")
        self._poll = request
        try:
            offset = _int(params["offset"], "offset") if "offset" in params else None
            limit = _int(params.get("limit", 100), "limit")
            timeout = min(max(_int(params.get("timeout", 0), "timeout"), 0), MAX_POLL_SECONDS)
            self.supervisor.on_poll()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                self.queue.changed.clear()
                updates = self.queue.serve(offset, limit, self.supervisor.may_serve)
                remaining = deadline - loop.time()
                if updates or remaining <= 0 or self._closing or _gone(request):
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.queue.changed.wait(), timeout=min(remaining, 0.5))
            if updates:
                self._log_call("getUpdates", 200)
            return _ok(updates)
        except BadParams as exc:
            self._log_call("getUpdates", 400)
            return _error(400, f"Bad Request: {exc}")
        finally:
            if self._poll is request:
                self._poll = None

    def _chat(self, params: dict[str, Any]) -> int:
        chat_id = _int(params.get("chat_id"), "chat_id")
        if chat_id not in self.members and chat_id != self.queue.probe_peer:
            raise BadParams("chat not found")  # only a member's private chat, whose id is the member's id
        return chat_id

    async def _send_chat_action(self, params: dict[str, Any]) -> web.Response:
        chat_id = self._chat(params)
        if not isinstance(params.get("action"), str):
            raise BadParams("action is required")
        now = self.mono()
        if (
            params["action"] == "typing"
            and chat_id != self.queue.probe_peer
            and now - self._typing_at.get(chat_id, float("-inf")) >= TYPING_EVERY_SECONDS
            and self.queue.conversation_open(chat_id)
            and self.supervisor.may_release()
        ):
            self._typing_at[chat_id] = now  # Core's own sends share the bot's rate limit
            with contextlib.suppress(TelegramError, NotSent, Ambiguous):
                await self.api.send_chat_action(chat_id, "typing")
        return _ok(True)

    def _reply_to(self, chat_id: int, params: dict[str, Any]) -> int | None:
        """reply_parameters.message_id may name only an issued message or the host's own, in the same chat
        (spec 4.2). A quote and the rest of reply_parameters are dropped."""
        reply = params.get("reply_parameters")
        if isinstance(reply, str):
            try:
                reply = json.loads(reply)
            except ValueError:
                raise BadParams("reply_parameters must be JSON") from None
        message_id: object = params.get("reply_to_message_id")
        if isinstance(reply, dict):
            if "chat_id" in reply and _int(reply["chat_id"], "reply_parameters.chat_id") != chat_id:
                raise BadParams("the replied message must be in the same chat")
            message_id = reply.get("message_id", message_id)
        elif reply is not None:
            raise BadParams("reply_parameters must be an object")
        if message_id is None:
            return None
        target = _int(message_id, "reply_parameters.message_id")
        if self.queue.issued_in_chat(chat_id, target) or self.outbox.owns(chat_id, target):
            return target
        raise BadParams("message to be replied not found")

    def _send_message(self, params: dict[str, Any]) -> web.Response:
        chat_id = self._chat(params)
        text = params.get("text")
        if not isinstance(text, str):
            raise BadParams("message text is empty")
        if len(text) > MAX_SOURCE_CHARS:
            raise BadParams("message is too long")
        mode = params.get("parse_mode")
        if mode not in (None, "") and str(mode).lower() != "html":
            raise BadParams("only parse_mode HTML is allowed")
        clean = sanitize_html(text if mode else escape(text, quote=False))
        if not clean.plain.strip():
            raise BadParams("message text is empty")
        if clean.units > MAX_TEXT_UNITS:
            raise BadParams("message is too long")
        reply_to = self._reply_to(chat_id, params)
        if chat_id == self.queue.probe_peer:
            # The host's "Your message could not be sent: …" for the probe: nobody reads it, and it proves the
            # turn ended at the block (spec 4.6).
            self.queue.mark_answered(chat_id)
            if PROBE_BLOCK in clean.plain:
                self.supervisor.on_probe_blocked()
            message_id = _PROBE_MESSAGE_ID
        elif not self.queue.conversation_open(chat_id):
            raise BadParams("no open conversation in this chat")
        elif self.outbox.waiting(chat_id) >= MAX_WAITING_PER_CHAT:
            raise BadParams("too many messages wait in this chat")
        else:
            message_id = self.outbox.accept(chat_id, clean.html, reply_to)
        message = {"message_id": message_id, "date": int(self.clock()), "chat": {"id": chat_id, "type": "private"}}
        return _ok(message | {"text": clean.plain})
