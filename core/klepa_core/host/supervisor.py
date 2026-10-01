"""Supervision of the host gateway (spec 4.6, D26, D20).

States:
- STARTING: the gateway runs, but gets no messages until it passes the start gate (the spec's "start"
  paragraph): a full heartbeat within 30 s, the gateway ready, and the live probe;
- RUNNING: the host gets messages while the heartbeat is at most 15 s old, and its sends leave on the same
  condition. A short pause delays answers without an alarm;
- HOLD: after three minutes without a heartbeat, or the owner's Pause. No new messages for the host, its sends
  are held, turns are blocked, and people get a fixed reply;
- STOPPED: a failed check that HOLD does not cure. For people it looks like HOLD.

RUNNING belongs to one boot of the gateway: a new boot_id passes the gate again. `mono` stops while the Mac
sleeps, so a sleep never looks like a silent adapter.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from ..aio import sleep_or_stop, until_stopped
from ..alerts import Alerts
from ..events import EventLog
from ..signing import canonical_json
from .queue import HostQueue

REQUIRED_HOOKS = frozenset({"before_dispatch", "before_prompt_build", "before_agent_run"})
# What the adapter returns for the probe's turn, and what the host must then write to the probe's chat: the
# host's "Your message could not be sent: …" (spike report, point 15) proves the turn was blocked.
PROBE_BLOCK = "Klepa start probe: this turn is blocked on purpose."
FORBIDDEN_MODEL_PREFIXES = ("claude-cli/", "openai/", "codex")  # spec 7.1: the model stays under the proxy
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GATE_STEP_SECONDS = 0.05


class GatewayState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    HOLD = "HOLD"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class Expectations:
    """What a full heartbeat carries (spec 4.2). Plan 1c-2 adds the plugin hash, the reference policy, the model
    and the runtime; until then a heartbeat must still be well formed and name every required hook."""

    hooks: frozenset[str] = REQUIRED_HOOKS
    plugin_sha256: str | None = None
    policy: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    runtime: str | None = None


@dataclass(frozen=True)
class HostTiming:
    first_heartbeat: float = 30.0  # a full heartbeat within 30 s of the start (spec 4.6)
    probe: float = 30.0  # the live probe
    release_within: float = 15.0  # the host's sends leave only while the heartbeat is this fresh
    hold_after: float = 180.0  # HOLD after three minutes without a heartbeat
    drop_after: float = 600.0  # a held send is dropped with Core's apology after ten minutes
    not_polling: float = 300.0  # RUNNING, but the host has not polled for this long: an alert
    tick: float = 1.0


class GatewayControl(Protocol):
    """How Core starts and stops the gateway (plan 1c-2: launchd, /startupz, /readyz and the log check)."""

    async def ready(self) -> str | None:
        """None once the gateway reports ready, else the reason it is not."""

    async def stop(self) -> None: ...


class NoGatewayControl:
    """The gateway is started and stopped outside Core; the heartbeat and the live probe still gate it."""

    async def ready(self) -> str | None:
        return None

    async def stop(self) -> None:
        return None


def _same(a: object, b: object) -> bool:
    try:
        return canonical_json(a) == canonical_json(b)  # also tells true from 1
    except (TypeError, ValueError):
        return False


def _named(value: object, wanted: str | None) -> bool:
    return isinstance(value, str) and bool(value) and wanted in (None, value)


def check_heartbeat(payload: Mapping[str, Any], expect: Expectations) -> list[str]:
    """What is wrong with a heartbeat, as key names only."""
    problems: list[str] = []
    hooks = payload.get("registrations")
    if not isinstance(hooks, list) or not all(isinstance(hook, str) for hook in hooks):
        problems.append("registrations")
    else:
        problems += [f"hook:{name}" for name in sorted(expect.hooks - set(hooks))]
    digest = payload.get("plugin_sha256")
    well_formed = isinstance(digest, str) and _SHA256.match(digest) is not None
    if not well_formed or expect.plugin_sha256 not in (None, digest):
        problems.append("plugin_sha256")
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        problems.append("policy")
    else:
        problems += [
            f"policy:{key}"
            for key, value in sorted(expect.policy.items())
            if key not in policy or not _same(policy[key], value)
        ]
    model, runtime = payload.get("model"), payload.get("runtime")
    if not _named(model, expect.model) or str(model).lower().startswith(FORBIDDEN_MODEL_PREFIXES):
        problems.append("model")
    if not _named(runtime, expect.runtime):
        problems.append("runtime")
    return problems


class Supervisor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        queue: HostQueue,
        events: EventLog,
        *,
        alerts: Alerts | None = None,
        expect: Expectations | None = None,
        control: GatewayControl | None = None,
        timing: HostTiming | None = None,
        on_hold: Callable[[], object] | None = None,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.conn = conn
        self.queue = queue
        self.events = events
        self.alerts = alerts
        self.expect = expect or Expectations()
        self.control: GatewayControl = control or NoGatewayControl()
        self.timing = timing or HostTiming()
        self.on_hold = on_hold
        self.mono = mono
        row = conn.execute("SELECT value FROM host_setting WHERE key='paused'").fetchone()
        self.paused = row is not None and row[0] == "1"
        self.state = GatewayState.HOLD if self.paused else GatewayState.STARTING
        self.reason: str | None = "paused" if self.paused else None
        self.boot_id: str | None = None  # the boot of the latest valid heartbeat
        self.seen_boot: str | None = None  # the boot of the latest heartbeat, valid or not
        self.gated_boot: str | None = None  # the boot that passed the start gate
        self.failed_boot: str | None = None  # the boot that failed it
        self.last_heartbeat: float | None = None
        self.last_poll: float | None = None
        self.running_since: float | None = None
        self.probe_id: int | None = None
        self._probe_turn: int | None = None  # the probe whose turn the adapter reported
        self._probe_blocked: int | None = None  # the probe whose block the host then wrote to its chat
        self._problems: dict[str, list[str]] = {}
        self._stop_reason: str | None = None

    # ---- inputs --------------------------------------------------------------------------------------------------
    def on_heartbeat(self, boot_id: str, payload: Mapping[str, Any]) -> list[str]:
        self.seen_boot = boot_id
        problems = check_heartbeat(payload, self.expect)
        if problems != self._problems.get(boot_id):  # log a change, not every beat
            if len(self._problems) >= 16:
                self._problems.clear()
            self._problems[boot_id] = problems
            if problems:
                self.events.log("heartbeat_rejected", {"boot_id": boot_id, "problems": problems[:10]})
        if problems:
            if boot_id == self.gated_boot and self.state in (GatewayState.RUNNING, GatewayState.HOLD):
                self._stop_reason = f"heartbeat:{problems[0]}"
            return problems
        self.last_heartbeat = self.mono()
        self.boot_id = boot_id
        if self.paused:
            return []
        if self.state is GatewayState.RUNNING and boot_id != self.gated_boot:
            self._set(GatewayState.STARTING, "new_boot")
        elif self.state is GatewayState.HOLD:
            if boot_id == self.gated_boot:
                self._set(GatewayState.RUNNING, None)
            else:
                self._set(GatewayState.STARTING, "new_boot")
        elif self.state is GatewayState.STOPPED and boot_id != self.failed_boot:
            self._set(GatewayState.STARTING, "new_boot")
        return []

    def on_probe_turn(self, host_message_id: int | None) -> None:
        """The adapter reported before_prompt_build and before_agent_run for the probe, and the turn was blocked."""
        if host_message_id is not None and host_message_id == self.probe_id:
            self._probe_turn = host_message_id

    def on_probe_blocked(self) -> None:
        """The host wrote the probe's block to the probe's chat: the turn ended there, without the model."""
        if self.probe_id is not None and self._probe_turn == self.probe_id:
            self._probe_blocked = self.probe_id

    def on_poll(self) -> None:
        self.last_poll = self.mono()

    def pause(self) -> None:
        """The owner's Pause (spec 9.5): HOLD until Resume, across restarts too."""
        self._save("paused", "1")
        self.paused = True
        self._set(GatewayState.HOLD, "paused")

    def resume(self) -> None:
        """Resume always passes the start gate again, and gives a boot that failed it another try."""
        self._save("paused", "0")
        self.paused = False
        self.failed_boot = None
        self.gated_boot = None
        self._set(GatewayState.STARTING, "resumed")

    # ---- decisions -----------------------------------------------------------------------------------------------
    def heartbeat_fresh(self) -> bool:
        return self.last_heartbeat is not None and self.mono() - self.last_heartbeat <= self.timing.release_within

    def may_release(self) -> bool:
        """The host's sends leave only while the gateway is up and the heartbeat is fresh (spec 4.2)."""
        return self.state in (GatewayState.STARTING, GatewayState.RUNNING) and self.heartbeat_fresh()

    def may_serve(self, host_message_id: int, kind: str) -> bool:
        """Text only while RUNNING with a fresh heartbeat: a gateway that restarted is a new boot that has not
        passed the gate, and until its first heartbeat Core cannot tell. The probe only while STARTING."""
        if kind == "probe":
            return self.state is GatewayState.STARTING and host_message_id == self.probe_id
        return self.state is GatewayState.RUNNING and self.heartbeat_fresh()

    def turns_blocked(self) -> bool:
        return self.state in (GatewayState.HOLD, GatewayState.STOPPED)

    def describe(self) -> str:
        """The service text key of the state, for the status line."""
        if self.state is GatewayState.HOLD:
            return "host_paused" if self.paused else "host_hold"
        return f"host_{self.state.value.lower()}"

    # ---- the loop ------------------------------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self._stop_reason is not None:
                reason, self._stop_reason = self._stop_reason, None
                await self._fail(reason)
            elif self.state is GatewayState.STARTING:
                await until_stopped(stop, self._gate())
                continue
            else:
                self._watch()
            await sleep_or_stop(stop, self.timing.tick)

    def _watch(self) -> None:
        now = self.mono()
        if self.state is GatewayState.RUNNING:
            if self.last_heartbeat is None or now - self.last_heartbeat > self.timing.hold_after:
                self._set(GatewayState.HOLD, "silent")
                if self.alerts is not None:
                    self.alerts.raise_("host_silent")
            elif now - max(self.last_poll or 0.0, self.running_since or now) > self.timing.not_polling:
                if self.alerts is not None:
                    self.alerts.raise_("host_not_polling")
        if self.state in (GatewayState.HOLD, GatewayState.STOPPED) and self.on_hold is not None:
            self.on_hold()

    def _candidate(self) -> bool:
        return (
            self.boot_id is not None
            and self.boot_id != self.failed_boot
            and self.heartbeat_fresh()
            and not self._problems.get(self.boot_id)
        )

    async def _gate(self) -> None:
        """The start gate (spec 4.6). Anything that changes the state or the boot ends it; the loop decides again."""
        started = self.mono()
        while not self._candidate():
            if self.state is not GatewayState.STARTING or self._stop_reason is not None:
                return
            if self.mono() - started >= self.timing.first_heartbeat:
                problems = self._problems.get(self.seen_boot or "")
                await self._fail(f"heartbeat:{problems[0]}" if problems else "no_heartbeat")
                return
            await asyncio.sleep(_GATE_STEP_SECONDS)
        boot = self.boot_id
        problem = await self.control.ready()
        if self.state is not GatewayState.STARTING or self.boot_id != boot:
            return
        if problem is not None:
            await self._fail(problem)
            return
        self.probe_id = self.queue.add_probe()
        deadline = self.mono() + self.timing.probe
        try:
            while self._probe_blocked != self.probe_id:
                if self.state is not GatewayState.STARTING or self.boot_id != boot:
                    return
                if self.mono() >= deadline:
                    await self._fail("probe")
                    return
                await asyncio.sleep(_GATE_STEP_SECONDS)
        finally:
            self.queue.retire(self.probe_id)
            self.probe_id = None
        self.gated_boot = boot
        self._set(GatewayState.RUNNING, None)

    async def _fail(self, reason: str) -> None:
        self.failed_boot = self.boot_id
        self.gated_boot = None
        self._set(GatewayState.STOPPED, reason)
        if self.alerts is not None:
            self.alerts.raise_("host_failed", reason=reason)
        await self.control.stop()

    def _set(self, state: GatewayState, reason: str | None) -> None:
        if state is GatewayState.RUNNING and self.state is not GatewayState.RUNNING:
            self.running_since = self.mono()
        if (state, reason) != (self.state, self.reason):
            self.events.log("host_state", {"state": state.value, "reason": reason})
        self.state = state
        self.reason = reason
        self.queue.wake()

    def _save(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO host_setting(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
