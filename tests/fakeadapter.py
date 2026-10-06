"""A stand-in for the adapter plugin (docs/architecture.md: Testing). Never used in production.

It talks to Core the way the plugin does: signed JSON over the Unix socket, with (boot_id, seq).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from pathlib import Path
from typing import Any

import aiohttp

from klepa_core.host.adapter import SIGNATURE_HEADER, sign_body

HOOKS = ["before_dispatch", "before_prompt_build", "before_agent_run", "before_tool_call", "reply_payload_sending"]


def session_key(peer: int) -> str:
    return f"agent:main:telegram:direct:{peer}"


class FakeAdapter:
    def __init__(self, socket_path: Path, key: bytes) -> None:
        self.socket_path = str(socket_path)
        self.key = key
        self.boot_id = secrets.token_hex(8)
        self.seq = 0
        self.beating = True
        self.heartbeat_fields: dict[str, Any] = {
            "registrations": list(HOOKS),
            "plugin_sha256": "0" * 64,
            "policy": {},
            "model": "anthropic/test-model",
            "runtime": "embedded",
        }

    def restart(self) -> None:
        """A new boot of the gateway."""
        self.boot_id = secrets.token_hex(8)
        self.seq = 0

    async def post(self, body: bytes, signature: str | None) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if signature is not None:
            headers[SIGNATURE_HEADER] = signature
        async with (
            aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=self.socket_path)) as session,
            session.post("http://adapter/v1/message", data=body, headers=headers) as resp,
        ):
            raw = await resp.read()
            assert resp.headers[SIGNATURE_HEADER] == sign_body(self.key, raw)  # every answer of Core is signed
            return resp.status, json.loads(raw)

    async def send(self, kind: str, **fields: Any) -> tuple[int, dict[str, Any]]:
        self.seq += 1
        body = json.dumps({"type": kind, "boot_id": self.boot_id, "seq": self.seq, **fields}).encode()
        return await self.post(body, sign_body(self.key, body))

    async def heartbeat(self, **overrides: Any) -> tuple[int, dict[str, Any]]:
        return await self.send("heartbeat", **(self.heartbeat_fields | overrides))

    async def dispatch(self, peer: int, message_id: int) -> tuple[int, dict[str, Any]]:
        # The host's hook context carries ids as strings (spike report, point 14).
        return await self.send(
            "dispatch", sender_id=str(peer), message_id=str(message_id), session_key=session_key(peer)
        )

    async def prompt_built(self, run_id: str, peer: int) -> tuple[int, dict[str, Any]]:
        return await self.send(
            "prompt_built", run_id=run_id, sender_id=str(peer), chat_id=str(peer), session_key=session_key(peer)
        )

    async def turn_start(self, run_id: str, peer: int) -> tuple[int, dict[str, Any]]:
        return await self.send(
            "turn_start", run_id=run_id, sender_id=str(peer), chat_id=str(peer), session_key=session_key(peer)
        )

    async def turn_reply(
        self, run_id: str, *, error: bool = False, error_kind: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        """How a turn that reached the model replied, as reply_payload_sending reports it."""
        return await self.send("turn_reply", run_id=run_id, error=error, error_kind=error_kind)

    async def beat(self, stop: asyncio.Event, every: float = 0.1) -> None:
        """The plugin's heartbeat service, every 5 s in production."""
        while not stop.is_set():
            if self.beating:
                with contextlib.suppress(OSError, aiohttp.ClientError):
                    await self.heartbeat()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), every)
