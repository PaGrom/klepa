"""The gateway as a launchd agent that Core loads, unloads and watches (spec 4.6, D26).

The agent's plist lives with the gateway's other files in the data folder, not in ~/Library/LaunchAgents: launchd
never starts the gateway by itself at login, only Core does. KeepAlive brings it back after a crash; a clean exit
leaves it down, and Core notices (the spike report, points 32 to 35). Stopping is only ever `launchctl bootout`.

Before every start Core writes the gateway's config from the reference table and the adapter it ships. After every
load it keeps a digest of what the gateway was started with: config, adapter and plist. A running gateway whose
digest matches the files is left running, so a restart of Core alone does not restart the gateway, and the start gate
proves it again. Files that changed since, whoever wrote them (`host install` too), are validated by OpenClaw and
reloaded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import plistlib
import re
import secrets
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from .. import macos
from ..keys import KeyFileError, ensure_private_dir
from . import runtime as rt
from .reference import PLUGIN_ID, HostLayout, render
from .supervisor import GatewayUnknown

LABEL = "klepa.gateway"
BOOTSTRAP_ATTEMPTS = 5
READY_SECONDS = 60.0  # after a long outage of Core the gateway may wait 30 s before it polls again
LOG_TAIL_BYTES = 16 * 1024 * 1024
NOT_LOADED = 113  # what `launchctl print` exits with for an agent it does not know
_STARTUP_MARK = "loading configuration"  # the first line the gateway logs on each start
_HOOK_PROBLEM = re.compile(r'typed hook "[^"]+" (?:blocked|ignored)|unknown typed hook')
log = logging.getLogger("klepa_core")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)


@dataclass(frozen=True)
class GatewayProcess:
    pid: int
    runs: int  # launchd counts starts: a new process even when the pid is reused


@dataclass(frozen=True)
class AgentState:
    loaded: bool
    process: GatewayProcess | None = None
    last_exit: int | None = None
    runs: int = 0
    crashed: bool = False  # it died of a signal or an error: launchd brings it back by itself
    state: str = ""


def parse_print(result: subprocess.CompletedProcess[str]) -> AgentState:
    """`launchctl print` of the agent: only the agent's own lines, which have one tab; nested sections have more.
    Raises GatewayUnknown for an answer that is neither "not loaded" nor readable: that is never a reason to act."""
    if result.returncode == NOT_LOADED:
        return AgentState(loaded=False)
    if result.returncode != 0:
        raise GatewayUnknown(f"launchctl print exited with {result.returncode}")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("\t") and not line.startswith("\t\t") and " = " in line:
            key, _, value = line.strip().partition(" = ")
            fields.setdefault(key, value)
    pid, runs_text = fields.get("pid", ""), fields.get("runs", "0")
    runs = int(runs_text) if runs_text.isdigit() else 0
    process = GatewayProcess(int(pid), runs) if pid.isdigit() else None
    code = fields.get("last exit code", "")
    last_exit = int(code) if code.lstrip("-").isdigit() else None
    crashed = "last terminating signal" in fields or last_exit not in (None, 0)
    state = fields.get("state", "")
    if state == "running" and process is None:
        raise GatewayUnknown("launchd says the agent runs but names no pid")
    return AgentState(loaded=True, process=process, last_exit=last_exit, runs=runs, crashed=crashed, state=state)


def plist_for(runtime: rt.Runtime, layout: HostLayout) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": rt.openclaw_argv(runtime, "gateway", "run"),
        "EnvironmentVariables": rt.gateway_env(runtime, layout),
        "WorkingDirectory": str(layout.host_dir),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # back after a crash; a clean exit is Core's to notice
        "ThrottleInterval": 10,
        "StandardOutPath": str(layout.logs_dir / "gateway.out.log"),
        "StandardErrorPath": str(layout.logs_dir / "gateway.err.log"),
    }


def prepare_host(
    runtime: rt.Runtime,
    layout: HostLayout,
    reference: dict[str, Any],
    *,
    exclude: Callable[[Path], str | None] = macos.exclude_from_time_machine,
    warn: Callable[[str], object] = log.warning,
) -> bool:
    """Write what the gateway runs from: its folders, its token, its config and the adapter. True when the config
    or the adapter changed. The folder holds the model's token and the host's sessions: out of Time Machine, every
    time, so a folder made again is excluded again."""
    for folder in (layout.host_dir, layout.home, layout.state_dir, layout.workspace, layout.logs_dir):
        ensure_private_dir(folder)
    problem = exclude(layout.host_dir)
    if problem is not None:
        warn(f"the gateway's folder is not excluded from Time Machine: {problem}")
    if not layout.gateway_token.exists():
        rt.write_private(layout.gateway_token, secrets.token_hex(32).encode())
    adapter_changed = rt.install_adapter(runtime)
    config = (json.dumps(render(reference), indent=2, ensure_ascii=False) + "\n").encode()
    config_changed = not layout.config_path.exists() or layout.config_path.read_bytes() != config
    if config_changed:
        rt.write_private(layout.config_path, config)
    for name in ("gateway.out.log", "gateway.err.log"):
        log = layout.logs_dir / name
        log.touch(mode=0o600, exist_ok=True)
    return adapter_changed or config_changed


def hook_problem(log: Path, tail_bytes: int = LOG_TAIL_BYTES) -> str | None:
    """A registration OpenClaw refused since the gateway last started (spec 4.6), from the gateway's own log."""
    try:
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - tail_bytes))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except FileNotFoundError:
        return None
    texts = [_message(line) for line in lines]
    starts = [i for i, text in enumerate(texts) if _STARTUP_MARK in text]
    if not starts:
        return None  # the start is older than the tail; the live probe still proves the hooks
    for text in texts[starts[-1] :]:
        found = _HOOK_PROBLEM.search(text)
        if found and f"plugin={PLUGIN_ID}" in text:  # another plugin's problem is not this gate's
            return found.group(0)
    return None


def _message(line: str) -> str:
    """The text of one JSON log record: its numbered fields."""
    try:
        record = json.loads(line)
    except ValueError:
        return line
    if not isinstance(record, dict):
        return line
    return " ".join(value for key, value in sorted(record.items()) if key.isdigit() and isinstance(value, str))


class LaunchdGateway:
    """Core's GatewayControl for its own gateway."""

    managed = True

    def __init__(
        self,
        runtime: rt.Runtime,
        layout: HostLayout,
        reference: dict[str, Any],
        port: int,
        *,
        run: Runner | None = None,
        uid: int | None = None,
        ready_seconds: float = READY_SECONDS,
        validate: Callable[[], None] | None = None,
        model_access: Callable[[], bool] | None = None,
    ) -> None:
        self.runtime = runtime
        self.layout = layout
        self.reference = reference
        self.port = port
        self.run = run or _run  # looked up now, so the test suite can replace _run once
        self.target = f"gui/{os.getuid() if uid is None else uid}"
        self.ready_seconds = ready_seconds
        self.retry_seconds = 1.0  # right after a bootout launchd may still be tearing the old agent down
        self.validate = validate or (lambda: rt.validate_config(runtime, layout))
        self.model_access = model_access or (lambda: rt.model_token_stored(runtime, layout))

    @property
    def plist_path(self) -> Path:
        return self.layout.host_dir / f"{LABEL}.plist"

    @property
    def applied_path(self) -> Path:
        """The digest of the files the loaded gateway was started with."""
        return self.layout.host_dir / "applied.sha256"

    def _digest(self, plist: bytes) -> str:
        parts = [self.layout.config_path.read_bytes(), plist]
        parts += [(self.runtime.adapter_dir / name).read_bytes() for name in rt.ADAPTER_FILES]
        digest = hashlib.sha256()
        for part in parts:
            digest.update(hashlib.sha256(part).digest())
        return digest.hexdigest()

    async def _launchctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        """launchctl's answer. One that hangs or cannot run is an answer that cannot be read, like any other failure."""
        argv = ["launchctl", *args]
        try:
            return await asyncio.to_thread(self.run, argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(argv, -1, "", f"launchctl did not answer: {type(exc).__name__}")

    async def state(self) -> AgentState:
        return parse_print(await self._launchctl("print", f"{self.target}/{LABEL}"))

    async def process(self) -> GatewayProcess | None:
        """The running process; a crashed one launchd is bringing back counts as a new process (pid 0), and only
        a clean exit or an unloaded agent is no process at all."""
        state = await self.state()
        if state.process is None and state.loaded and state.crashed:
            return GatewayProcess(0, state.runs)
        return state.process

    async def start(self) -> str | None:
        """Write the gateway's files and load the agent unless it already runs exactly them. None, or why not."""
        if not self.runtime.openclaw_entry.exists():
            return "OpenClaw is not installed: run klepa-core host install"
        try:
            await asyncio.to_thread(prepare_host, self.runtime, self.layout, self.reference)
            plist = plistlib.dumps(plist_for(self.runtime, self.layout))
            if not self.plist_path.exists() or self.plist_path.read_bytes() != plist:
                await asyncio.to_thread(rt.write_private, self.plist_path, plist)
            current = await asyncio.to_thread(self._digest, plist)
            applied = self.applied_path.read_text().strip() if self.applied_path.exists() else ""
            if current != applied:
                await asyncio.to_thread(self.validate)
        except (OSError, KeyFileError, rt.HostRuntimeError) as exc:
            return f"config: {exc}"[:200]
        try:
            state: AgentState | None = await self.state()
        except GatewayUnknown:
            state = None
        if state is not None and state.loaded and state.process is not None and current == applied:
            return None
        if state is None or state.loaded:
            await self.stop()
        error = ""
        for _ in range(BOOTSTRAP_ATTEMPTS):
            result = await self._launchctl("bootstrap", self.target, str(self.plist_path))
            if result.returncode == 0:
                try:
                    await asyncio.to_thread(rt.write_private, self.applied_path, current.encode())
                except OSError as exc:  # the gateway runs the current files; the next start loads them once more
                    log.warning("the gateway's digest was not written: %s", type(exc).__name__)
                return None
            error = result.stderr.strip()
            await asyncio.sleep(self.retry_seconds)
        return f"launchctl bootstrap: {error}"[:200]

    async def stop(self) -> None:
        await self._launchctl("bootout", f"{self.target}/{LABEL}")
        for _ in range(50):
            try:
                if not (await self.state()).loaded:
                    return
            except GatewayUnknown:
                pass
            await asyncio.sleep(0.2)

    async def ready(self) -> str | None:
        """The gateway started and its channels are ready (spec 4.6), and OpenClaw refused none of the hooks."""
        deadline = asyncio.get_running_loop().time() + self.ready_seconds
        problem: str | None = "startupz"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
            while asyncio.get_running_loop().time() < deadline:
                problem = await self._probe(session)
                if problem is None:
                    break
                await asyncio.sleep(0.5)
        if problem is not None:
            return problem
        problem = await asyncio.to_thread(hook_problem, self.layout.gateway_log)
        if problem is not None:
            return problem
        if not await asyncio.to_thread(self.model_access):
            return "no model token: run klepa-core host login"  # OpenClaw refuses every turn before the hooks
        return None

    async def _probe(self, session: aiohttp.ClientSession) -> str | None:
        base = f"http://127.0.0.1:{self.port}"
        for path, want in (("/startupz", "started"), ("/readyz", None)):
            try:
                async with session.get(base + path) as response:
                    body = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                return path.strip("/")
            if response.status != 200 or (
                want is not None and (not isinstance(body, dict) or body.get("status") != want)
            ):
                return path.strip("/")
        return None
