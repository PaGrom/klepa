"""Minimal Telegram Bot API client for the gatekeeper (docs/architecture.md: Intake, D33).

Error messages never include the request URL, because the URL carries the bot token.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: float | None = None) -> None:
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after


class Unauthorized(TelegramError):
    """401: the token was revoked or is wrong."""


class Conflict(TelegramError):
    """409: someone else polls with the same token."""


class TooManyRequests(TelegramError):
    """429: wait retry_after seconds."""


class BadRequest(TelegramError):
    """Another 4xx: the request itself is wrong; do not retry blindly."""


class NotSent(Exception):
    """The request never reached Telegram; retrying is safe."""


class Ambiguous(Exception):
    """The request may have reached Telegram; the outcome is unknown."""


class DownloadFailed(Exception):
    """A file download failed or arrived incomplete; retry later."""


def upload_seconds(size: int) -> float:
    """How long an upload may take: two minutes, and a second for every 100 KB, so that a 50 MB document has some ten
    minutes on a slow home line instead of ending UNKNOWN (the final review's M4)."""
    return 120.0 + size / 100_000


class BotApi:
    def __init__(self, session: aiohttp.ClientSession, api_root: str, token: str) -> None:
        self._session = session
        self._base = api_root.rstrip("/")
        self._token = token

    async def call(
        self, method: str, params: dict[str, Any], *, timeout: float, form: aiohttp.FormData | None = None
    ) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        body: dict[str, Any] = {"json": params} if form is None else {"data": form}
        try:
            async with self._session.post(url, **body, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                status = resp.status
                raw = await resp.read()
        except aiohttp.ClientConnectorError as exc:
            raise NotSent(f"{method}: cannot connect ({type(exc).__name__})") from None
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise Ambiguous(f"{method}: {type(exc).__name__}") from None
        try:
            body = json.loads(raw)
        except ValueError:
            raise Ambiguous(f"{method}: HTTP {status} with a non-JSON body") from None
        if body.get("ok") is True:
            return body.get("result")
        code = int(body.get("error_code") or status)
        description = str(body.get("description", ""))
        parameters = body.get("parameters") or {}
        if code == 401:
            raise Unauthorized(method, code, description)
        if code == 409:
            raise Conflict(method, code, description)
        if code == 429:
            raise TooManyRequests(method, code, description, float(parameters.get("retry_after", 1)))
        if 400 <= code < 500:
            raise BadRequest(method, code, description)
        raise Ambiguous(f"{method}: server error {code}")

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        result = await self.call("getUpdates", params, timeout=timeout + 15)
        if not isinstance(result, list):
            raise Ambiguous("getUpdates: unexpected result")
        return result

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return await self._call_for_object("getFile", {"file_id": file_id}, timeout=30)

    async def download(self, file_path: str, max_bytes: int, *, read_timeout: float = 60.0) -> bytes:
        """A silent connection fails after `read_timeout`, so one stalled download cannot hold intake for long."""
        url = f"{self._base}/file/bot{self._token}/{file_path}"
        chunks: list[bytes] = []
        total = 0
        timeout = aiohttp.ClientTimeout(total=600, sock_connect=30, sock_read=read_timeout)
        try:
            async with self._session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    raise DownloadFailed(f"download: HTTP {resp.status}")
                expected = resp.content_length
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise DownloadFailed("download: larger than the limit")
                    chunks.append(chunk)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise DownloadFailed(f"download: {type(exc).__name__}") from None
        if expected is not None and total != expected:
            raise DownloadFailed("download: truncated")
        return b"".join(chunks)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        *,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if parse_mode is not None:
            params["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            params["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return await self._call_for_object("sendMessage", params, timeout=30)

    async def send_document(
        self,
        chat_id: int,
        data: bytes,
        filename: str,
        mime: str,
        reply_to_message_id: int | None = None,
        *,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Upload these bytes as a document named `filename`: the person gets back the name the file came with."""
        form = aiohttp.FormData(quote_fields=False)  # the name as UTF-8: percent-encoding would reach the person
        form.add_field("chat_id", str(chat_id))
        if reply_to_message_id is not None:
            reply = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
            form.add_field("reply_parameters", json.dumps(reply))
        if caption is not None:
            form.add_field("caption", caption)
        form.add_field("document", data, filename=filename.replace('"', "'"), content_type=mime)
        result = await self.call("sendDocument", {}, timeout=upload_seconds(len(data)), form=form)
        if not isinstance(result, dict):
            raise Ambiguous("sendDocument: unexpected result")
        return result

    async def send_photo(
        self,
        chat_id: int,
        data: bytes,
        caption: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upload a picture Core drew (a draft's sheet), with its caption and buttons."""
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption)
        if reply_to_message_id is not None:
            reply = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
            form.add_field("reply_parameters", json.dumps(reply))
        if reply_markup is not None:
            form.add_field("reply_markup", json.dumps(reply_markup))
        form.add_field("photo", data, filename="sheet.jpg", content_type="image/jpeg")
        result = await self.call("sendPhoto", {}, timeout=upload_seconds(len(data)), form=form)
        if not isinstance(result, dict):
            raise Ambiguous("sendPhoto: unexpected result")
        return result

    async def edit_message_reply_markup(self, chat_id: int, message_id: int) -> None:
        """Take the buttons off one of Core's messages; its text or picture stays."""
        await self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id}, timeout=30)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        """Rewrite one of Core's own messages. Without reply_markup Telegram drops its buttons."""
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        await self.call("editMessageText", params, timeout=30)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        await self.call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10)

    async def get_me(self) -> dict[str, Any]:
        return await self._call_for_object("getMe", {}, timeout=30)

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, *, show_alert: bool = False
    ) -> None:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            params["text"] = text
        if show_alert:  # a dialog the person closes, not a toast that fades
            params["show_alert"] = True
        await self.call("answerCallbackQuery", params, timeout=30)

    async def _call_for_object(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        result = await self.call(method, params, timeout=timeout)
        if not isinstance(result, dict):
            raise Ambiguous(f"{method}: unexpected result")
        return result
