"""Fake Telegram Bot API for tests and the stand (spec §13.1). Never used in production."""
from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

from aiohttp import web

MAX_BOT_FILE = 20 * 1024 * 1024


class FakeTelegram:
    def __init__(self, token: str) -> None:
        self.token = token
        self.token_valid = True
        self.updates: list[dict[str, Any]] = []
        self.forced: list[dict[str, Any]] = []
        self.offsets_seen: list[int | None] = []
        self.files: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.failures: dict[str, list[dict[str, Any]]] = {}
        self.url = ""
        self._port = 0
        self._next_update_id = 1000
        self._next_message_id = 1
        self._next_sent_id = 900000
        self._wakeup = asyncio.Event()
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/bot{token}/{method}", self._api)
        app.router.add_get("/file/bot{token}/{path:.+}", self._file)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()
        self._port = self._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{self._port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---- building updates ------------------------------------------------------------------
    def message(self, from_id: int, *, chat_id: int | None = None, chat_type: str = "private",
                date: int | None = None, **fields: Any) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "message_id": self._next_message_id,
            "date": date or int(time.time()),
            "chat": {"id": from_id if chat_id is None else chat_id, "type": chat_type},
            "from": {"id": from_id, "is_bot": False, "first_name": "Test"},
        }
        self._next_message_id += 1
        msg.update(fields)
        return msg

    def push(self, fields: dict[str, Any]) -> dict[str, Any]:
        update = {"update_id": self._next_update_id, **fields}
        self._next_update_id += 1
        self.updates.append(update)
        self._wakeup.set()
        return update

    def add_text(self, from_id: int, text: str, **kw: Any) -> dict[str, Any]:
        return self.push({"message": self.message(from_id, text=text, **kw)})

    def _register_file(self, data: bytes, name: str | None, size: int | None) -> dict[str, Any]:
        n = len(self.files) + 1
        file_id = f"FILE{n}"
        real_size = len(data) if size is None else size
        self.files[file_id] = {"data": data, "path": f"documents/file_{n}", "size": real_size}
        meta: dict[str, Any] = {"file_id": file_id, "file_unique_id": f"U{n}", "file_size": real_size}
        if name is not None:
            meta["file_name"] = name
        return meta

    def _attachment(self, from_id: int, fields: dict[str, Any], caption: str | None,
                    media_group_id: str | None, kw: dict[str, Any]) -> dict[str, Any]:
        if caption is not None:
            fields["caption"] = caption
        if media_group_id is not None:
            fields["media_group_id"] = media_group_id
        return self.push({"message": self.message(from_id, **fields, **kw)})

    def add_document(self, from_id: int, name: str, data: bytes, *, mime: str = "application/pdf",
                     caption: str | None = None, media_group_id: str | None = None,
                     file_size: int | None = None, **kw: Any) -> dict[str, Any]:
        document = self._register_file(data, name, file_size) | {"mime_type": mime}
        return self._attachment(from_id, {"document": document}, caption, media_group_id, kw)

    def add_photo(self, from_id: int, data: bytes, *, caption: str | None = None,
                  media_group_id: str | None = None, **kw: Any) -> dict[str, Any]:
        big = self._register_file(data, None, None) | {"width": 1280, "height": 960}
        small = {"file_id": big["file_id"] + "-thumb", "file_unique_id": big["file_unique_id"] + "-thumb",
                 "file_size": 1, "width": 90, "height": 67}
        return self._attachment(from_id, {"photo": [small, big]}, caption, media_group_id, kw)

    def add_voice(self, from_id: int, data: bytes, *, duration: int = 5, **kw: Any) -> dict[str, Any]:
        voice = self._register_file(data, None, None) | {"duration": duration, "mime_type": "audio/ogg"}
        return self._attachment(from_id, {"voice": voice}, None, None, kw)

    def redeliver(self, update: dict[str, Any], *, new_update_id: bool = False) -> dict[str, Any]:
        """Deliver a copy again: the same update_id (ignoring offsets) or the same message under a new id."""
        if new_update_id:
            return self.push({k: copy.deepcopy(v) for k, v in update.items() if k != "update_id"})
        self.forced.append(copy.deepcopy(update))
        self._wakeup.set()
        return update

    def fail(self, method: str, *, status: int = 500, description: str = "Internal Server Error",
             retry_after: float | None = None, drop: bool = False, stall: bool = False, times: int = 1) -> None:
        for _ in range(times):
            self.failures.setdefault(method, []).append(
                {"status": status, "description": description, "retry_after": retry_after, "drop": drop,
                 "stall": stall})

    # ---- HTTP --------------------------------------------------------------------------------
    async def _api(self, request: web.Request) -> web.StreamResponse:
        method = request.match_info["method"]
        self.calls.append(method)
        if request.match_info["token"] != self.token or not self.token_valid:
            return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)
        params = await request.json() if request.body_exists else {}
        queue = self.failures.get(method)
        if queue:
            failure = queue.pop(0)
            if failure["drop"]:
                request.transport.close()
                return web.Response(status=500)
            body: dict[str, Any] = {"ok": False, "error_code": failure["status"], "description": failure["description"]}
            if failure["retry_after"] is not None:
                body["parameters"] = {"retry_after": failure["retry_after"]}
            return web.json_response(body, status=failure["status"])
        handler = {"getUpdates": self._get_updates, "getFile": self._get_file,
                   "sendMessage": self._send_message}.get(method)
        result = await handler(params) if handler else True
        if isinstance(result, web.StreamResponse):
            return result
        return web.json_response({"ok": True, "result": result})

    async def _get_updates(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        offset = params.get("offset")
        self.offsets_seen.append(offset)
        if isinstance(offset, int) and offset > 0:
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
        deadline = time.monotonic() + min(float(params.get("timeout", 0)), 1.0)
        while not self.updates and not self.forced:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), remaining)
            except TimeoutError:
                break
        batch = self.forced + self.updates[:100]
        self.forced = []
        return copy.deepcopy(batch)

    async def _get_file(self, params: dict[str, Any]) -> Any:
        entry = self.files.get(params.get("file_id"))
        if entry is None:
            return web.json_response({"ok": False, "error_code": 400, "description": "Bad Request: invalid file_id"}, status=400)
        if entry["size"] > MAX_BOT_FILE:
            return web.json_response({"ok": False, "error_code": 400, "description": "Bad Request: file is too big"}, status=400)
        return {"file_id": params["file_id"], "file_unique_id": "U" + params["file_id"],
                "file_size": entry["size"], "file_path": entry["path"]}

    async def _send_message(self, params: dict[str, Any]) -> dict[str, Any]:
        self._next_sent_id += 1
        message = {"message_id": self._next_sent_id, "date": int(time.time()),
                   "chat": {"id": params["chat_id"], "type": "private"}, "text": params.get("text", "")}
        self.sent.append({"params": params, "message": message})
        return message

    async def _file(self, request: web.Request) -> web.StreamResponse:
        if request.match_info["token"] != self.token or not self.token_valid:
            return web.Response(status=404)
        entry = next((e for e in self.files.values() if e["path"] == request.match_info["path"]), None)
        if entry is None:
            return web.Response(status=404)
        queue = self.failures.get("download")
        if queue:
            failure = queue.pop(0)
            response = web.StreamResponse(status=200)
            response.content_length = len(entry["data"])
            await response.prepare(request)
            await response.write(entry["data"][: len(entry["data"]) // 2])
            if failure["stall"]:
                await asyncio.sleep(2)  # the connection stays open and silent
            request.transport.close()
            return response
        return web.Response(body=entry["data"], content_type="application/octet-stream")
