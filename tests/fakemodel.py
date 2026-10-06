"""A scripted model behind an OpenAI-compatible API, for the real gateway's tests. Never used in production.

The gateway reaches it as the provider `klepatest` through Core's egress proxy. A test sets `script`: it gets the
request as `Request` and returns `Say(text)` or `Call(tool, arguments)`. Every request is kept in `requests`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

PROVIDER = "klepatest"
MODEL = f"{PROVIDER}/scripted"
INTERNAL = "OPENCLAW_INTERNAL_CONTEXT"  # OpenClaw's own context messages are not the person's


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


@dataclass
class Request:
    messages: list[dict[str, Any]]
    tools: list[str]

    @property
    def system(self) -> str:
        return "\n".join(text_of(m.get("content")) for m in self.messages if m.get("role") in ("system", "developer"))

    @property
    def human_index(self) -> int:
        for index in range(len(self.messages) - 1, -1, -1):
            message = self.messages[index]
            if message.get("role") == "user" and INTERNAL not in text_of(message.get("content")):
                return index
        return -1

    @property
    def human(self) -> str:
        """The person's own words: OpenClaw wraps them in a conversation-info block and a runtime line."""
        index = self.human_index
        raw = text_of(self.messages[index].get("content")) if index >= 0 else ""
        body = raw.split("```", 2)[2] if raw.count("```") >= 2 else raw
        return body.split("\nRuntime:", 1)[0].strip()

    @property
    def tool_results(self) -> list[str]:
        """What tools answered since the person's last message, oldest first."""
        return [text_of(m.get("content")) for m in self.messages[self.human_index + 1 :] if m.get("role") == "tool"]


@dataclass
class Say:
    text: str
    delay: float = 0.0  # seconds the model "thinks" before it answers


@dataclass
class Call:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw: str | None = None  # the arguments exactly as the model writes them, when a test needs odd JSON


@dataclass
class Fail:
    status: int  # what the provider answers instead: 401 for a token that stopped working, 529 when it is busy


# The error type a provider gives with each status: OpenClaw's report, and so the failure's kind, depends on it.
ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    429: "rate_limit_error",
    500: "api_error",
    529: "overloaded_error",
}


def echo(request: Request) -> Say | Call | Fail:
    return Say(f"STUB: {request.human[:200]}")


def provider_table(url: str) -> dict[str, Any]:
    """The reference-table entries that make the gateway use this model."""
    return {
        "models.mode": "merge",
        f"models.providers.{PROVIDER}": {
            "baseUrl": f"{url}/v1",
            "apiKey": "not-a-secret",
            "api": "openai-completions",
            "models": [
                {
                    "id": "scripted",
                    "name": "Scripted",
                    "reasoning": False,
                    "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": 32000,
                    "maxTokens": 1000,
                }
            ],
        },
    }


class FakeModel:
    def __init__(self) -> None:
        self.script: Callable[[Request], Say | Call | Fail] = echo
        self.answer_probes = True  # the gate's model probe gets "OK" whatever the script says for people
        self.requests: list[Request] = []
        self.url = ""
        self.port = 0
        self._calls = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/chat/completions", self._complete)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _models(self, request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": "scripted", "object": "model", "owned_by": "t"}]})

    async def _complete(self, http: web.Request) -> web.StreamResponse:
        body = await http.json()
        tools = [t.get("function", {}).get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
        request = Request(list(body.get("messages") or []), [name for name in tools if isinstance(name, str)])
        self.requests.append(request)
        # The gate's model probe gets its answer whatever the test scripts for people.
        probe = self.answer_probes and "Klepa start check" in request.human
        decision = Say("OK") if probe else self.script(request)
        self._calls += 1
        if isinstance(decision, Say) and decision.delay:
            await asyncio.sleep(decision.delay)
        if isinstance(decision, Fail):
            kind = ERROR_TYPES.get(decision.status, "api_error")
            error = {"error": {"message": "refused by the test", "type": kind}}
            return web.json_response(error, status=decision.status)
        if isinstance(decision, Call):
            arguments = decision.raw if decision.raw is not None else json.dumps(decision.arguments)
            call = {"id": f"call_{self._calls}_{int(time.time() * 1000)}", "type": "function"}
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [call | {"function": {"name": decision.tool, "arguments": arguments}}],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": decision.text}
            finish = "stop"
        created, model = int(time.time()), body.get("model", "scripted")
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not body.get("stream"):
            choice = {"index": 0, "message": message, "finish_reason": finish}
            return web.json_response(
                {"id": "chatcmpl-t", "object": "chat.completion", "created": created, "model": model}
                | {"choices": [choice], "usage": usage}
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(http)

        async def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> None:
            choice = {"index": 0, "delta": delta, "finish_reason": finish_reason}
            data = {"id": "chatcmpl-t", "object": "chat.completion.chunk", "created": created, "model": model}
            await response.write(f"data: {json.dumps(data | {'choices': [choice]}, ensure_ascii=False)}\n\n".encode())

        if finish == "tool_calls":
            call = message["tool_calls"][0]
            head = {"index": 0, "id": call["id"], "type": "function"}
            await chunk(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [head | {"function": {"name": call["function"]["name"], "arguments": ""}}],
                }
            )
            await chunk({"tool_calls": [{"index": 0, "function": {"arguments": call["function"]["arguments"]}}]})
        else:
            await chunk({"role": "assistant", "content": ""})
            await chunk({"content": message["content"]})
        await chunk({}, finish)
        if (body.get("stream_options") or {}).get("include_usage"):
            data = {"id": "chatcmpl-t", "object": "chat.completion.chunk", "created": created, "model": model}
            await response.write(f"data: {json.dumps(data | {'choices': [], 'usage': usage})}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response
