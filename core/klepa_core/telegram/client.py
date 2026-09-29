"""Minimal Telegram Bot API client for the gatekeeper (spec D33).

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


class BotApi:
    def __init__(self, session: aiohttp.ClientSession, api_root: str, token: str) -> None:
        self._session = session
        self._base = api_root.rstrip("/")
        self._token = token

    async def call(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        try:
            async with self._session.post(url, json=params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
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
        return await self.call("getUpdates", params, timeout=timeout + 15)

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return await self.call("getFile", {"file_id": file_id}, timeout=30)

    async def download(self, file_path: str, max_bytes: int) -> bytes:
        url = f"{self._base}/file/bot{self._token}/{file_path}"
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
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

    async def send_message(self, chat_id: int, text: str, reply_to_message_id: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if reply_to_message_id is not None:
            params["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
        return await self.call("sendMessage", params, timeout=30)
