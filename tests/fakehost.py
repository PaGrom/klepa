"""A stand-in for OpenClaw and its adapter plugin (docs/architecture.md: Testing). Never used in production.

It polls the gatekeeper with the fake token and drives the fake adapter the way the host drives the plugin's
hooks: before_dispatch first; a message that it does not handle goes on to before_prompt_build and
before_agent_run. A blocked turn makes the host write Core's text to the chat, as the plugin leaves it; a turn that
passes gets the answer of `model` and the plugin's report of it (turn_reply). Every call carries the headers of
undici, the fetch of Node that OpenClaw uses.
`conversation_hooks=False` is a plugin without allowConversationAccess: those two hooks never run (spike report,
point 20).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import aiohttp

from fakeadapter import FakeAdapter

# What undici's fetch adds to every request; Sec-Fetch-Mode is the one a gatekeeper could mistake for a browser.
NODE_FETCH_HEADERS = {"sec-fetch-mode": "cors", "accept": "*/*", "accept-language": "*", "user-agent": "node"}


class FakeHost:
    def __init__(
        self,
        url: str,
        token: str,
        adapter: FakeAdapter,
        *,
        proxy: str | None = None,
        conversation_hooks: bool = True,
        poll_timeout: int = 1,
    ) -> None:
        self.url = url
        self.token = token
        self.adapter = adapter
        self.proxy = proxy
        self.conversation_hooks = conversation_hooks
        self.poll_timeout = poll_timeout
        self.answering = True  # False: the host takes messages but never gets to answer them
        self.model = lambda text: f"STUB: {text}"  # the scripted model; the gate's model probe gets "OK"
        self.updates: list[dict[str, Any]] = []
        self.offset: int | None = None

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.url}/bot{self.token}/{method}",
                json=params or {},
                headers=NODE_FETCH_HEADERS | (headers or {}),
                proxy=self.proxy,
            ) as resp,
        ):
            return resp.status, await resp.json()

    async def send(self, chat_id: int, text: str, **params: Any) -> tuple[int, dict[str, Any]]:
        return await self.call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", **params})

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            params: dict[str, Any] = {"timeout": self.poll_timeout}
            if self.offset is not None:
                params["offset"] = self.offset
            try:
                status, body = await self.call("getUpdates", params)
            except (aiohttp.ClientError, OSError):
                status, body = 0, {}
            if status != 200:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 0.1)
                continue
            for update in body["result"]:
                self.offset = update["update_id"] + 1
                self.updates.append(update)
                if self.answering:
                    await self.handle(update["message"])

    async def handle(self, message: dict[str, Any]) -> None:
        peer = message["chat"]["id"]
        _, answer = await self.adapter.dispatch(peer, message["message_id"])
        if answer.get("handled"):
            await self.send(peer, answer["text"])
            return
        if not self.conversation_hooks:
            return  # the turn would run without Core ever hearing of it
        run_id = str(uuid.uuid4())
        await self.adapter.prompt_built(run_id, peer)
        _, turn = await self.adapter.turn_start(run_id, peer)
        if turn.get("outcome") == "block":
            await self.send(peer, turn["message"])
            return
        text = message.get("text", "")
        answer = "OK" if text.startswith("Klepa start check") else self.model(text)
        await self.send(peer, answer)
        await self.adapter.turn_reply(run_id)

    def texts(self) -> list[str]:
        return [update["message"]["text"] for update in self.updates]
