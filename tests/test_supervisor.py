import asyncio
import contextlib

import pytest

from helpers import Clock, wait_until
from klepa_core.events import EventLog
from klepa_core.host import supervisor as module
from klepa_core.host.queue import HostQueue
from klepa_core.host.supervisor import Expectations, GatewayState, HostTiming, Supervisor, check_heartbeat

PEER = 2**51 + 7
FULL = {
    "registrations": ["before_dispatch", "before_prompt_build", "before_agent_run", "service"],
    "plugin_sha256": "a" * 64,
    "policy": {"tools.profile": "minimal", "gateway.reload.mode": "off"},
    "model": "anthropic/claude-test",
    "runtime": "embedded",
}


class FakeAlerts:
    def __init__(self):
        self.raised = []

    def raise_(self, name, **fields):
        self.raised.append((name, fields))
        return True


class FakeControl:
    """Core's control of the gateway. Managed: Core starts it and watches its process, as with launchd."""

    def __init__(self, ready=None, *, managed=False, start=None):
        self.answer = ready
        self.managed = managed
        self.start_answer = start
        self.stops = 0
        self.starts = 0
        self.running = None

    async def start(self):
        self.starts += 1
        if self.start_answer is None and self.running is None:
            self.running = f"process-{self.starts}"
        return self.start_answer

    async def ready(self):
        return self.answer

    async def stop(self):
        self.stops += 1
        self.running = None

    async def process(self):
        if self.running == "unknown":
            raise module.GatewayUnknown("launchctl print failed")
        return self.running


@pytest.fixture
def make(core_db):
    cfg, conn, journal = core_db

    def build(*, mono=None, control=None, timing=None, on_hold=None):
        queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), PEER)
        alerts = FakeAlerts()
        supervisor = Supervisor(
            conn,
            queue,
            EventLog(conn),
            alerts=alerts,
            control=control or FakeControl(),
            timing=timing or HostTiming(first_heartbeat=1.0, probe=1.0, tick=0.01),
            on_hold=on_hold,
            **({"mono": mono} if mono is not None else {}),
        )
        return supervisor, alerts, queue

    return build


def managed(**changes):
    """Timings for Core's own gateway: its process is checked at every step."""
    return HostTiming(**({"first_heartbeat": 1.0, "probe": 1.0, "process_check": 0.01, "tick": 0.01} | changes))


@contextlib.asynccontextmanager
async def running(supervisor):
    stop = asyncio.Event()
    task = asyncio.create_task(supervisor.run(stop))
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def gate(supervisor, boot="boot-aaaaaaaa"):
    """Pass the start gate the way the host and the adapter would: a full heartbeat, the probe's turn reported by
    the adapter, then the host writing the block to the probe's chat."""
    supervisor.on_heartbeat(boot, FULL)
    await wait_until(lambda: supervisor.probe_id is not None)
    supervisor.on_probe_turn(supervisor.probe_id)
    supervisor.on_probe_blocked()
    await wait_until(lambda: supervisor.state is GatewayState.RUNNING)


def test_a_full_heartbeat_has_no_problems():
    assert check_heartbeat(FULL, Expectations(plugin_sha256="a" * 64, policy={"tools.profile": "minimal"})) == []


@pytest.mark.parametrize(
    ("change", "expect", "problem"),
    [
        ({"registrations": ["before_dispatch"]}, Expectations(), "hook:before_agent_run"),
        ({"registrations": "all"}, Expectations(), "registrations"),
        ({"plugin_sha256": "xyz"}, Expectations(), "plugin_sha256"),
        ({}, Expectations(plugin_sha256="b" * 64), "plugin_sha256"),
        (
            {"policy": {"tools.profile": "full"}},
            Expectations(policy={"tools.profile": "minimal"}),
            "policy:tools.profile",
        ),
        ({"policy": {"x": 1}}, Expectations(policy={"x": True}), "policy:x"),
        ({"policy": {}}, Expectations(policy={"x": None}), "policy:x"),
        ({"policy": []}, Expectations(), "policy"),
        ({"model": "claude-cli/opus"}, Expectations(), "model"),
        ({"model": "openai/gpt"}, Expectations(), "model"),
        ({"model": ""}, Expectations(), "model"),
        ({"runtime": None}, Expectations(), "runtime"),
        ({"runtime": "acp"}, Expectations(runtime="embedded"), "runtime"),
    ],
)
def test_heartbeat_problems_are_named(change, expect, problem):
    assert problem in check_heartbeat(FULL | change, expect)


def test_another_openclaw_release_is_a_problem():
    expect = Expectations(version="2026.9.4")
    assert check_heartbeat(FULL | {"version": "2026.9.4"}, expect) == []
    assert check_heartbeat(FULL | {"version": "2027.1.0"}, expect) == ["version"]
    assert check_heartbeat(FULL, expect) == ["version"]


async def test_the_start_gate_runs_the_live_probe(make):
    supervisor, alerts, queue = make()
    assert supervisor.state is GatewayState.STARTING
    async with running(supervisor):
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL)
        await wait_until(lambda: supervisor.probe_id is not None)
        probe = supervisor.probe_id
        assert supervisor.may_serve(probe, "probe")
        assert not supervisor.may_serve(1, "text")
        supervisor.on_probe_turn(probe)
        supervisor.on_probe_blocked()
        await wait_until(lambda: supervisor.state is GatewayState.RUNNING)
    assert supervisor.gated_boot == "boot-aaaaaaaa"
    assert alerts.raised == []
    assert queue.serve(None, 100, lambda host_message_id, kind: True) == []  # the probe is retired


async def test_no_heartbeat_stops_with_an_alert(make):
    control = FakeControl()
    supervisor, alerts, _ = make(control=control, timing=HostTiming(first_heartbeat=0.2, tick=0.01))
    async with running(supervisor):
        await wait_until(lambda: supervisor.state is GatewayState.STOPPED)
    assert supervisor.reason == "no_heartbeat"
    assert alerts.raised == [("host_failed", {"reason": "no_heartbeat"})]
    assert control.stops == 1


async def test_a_heartbeat_with_problems_fails_the_gate_by_name(make):
    supervisor, _, _ = make(timing=HostTiming(first_heartbeat=0.2, tick=0.01))
    async with running(supervisor):
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL | {"registrations": ["before_dispatch", "before_agent_run"]})
        await wait_until(lambda: supervisor.state is GatewayState.STOPPED)
    assert supervisor.reason == "heartbeat:hook:before_prompt_build"


async def test_a_failed_probe_stops_that_boot_and_a_new_boot_tries_again(make):
    control = FakeControl()
    supervisor, _, _ = make(control=control, timing=HostTiming(first_heartbeat=1.0, probe=0.2, tick=0.01))
    async with running(supervisor):
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL)
        await wait_until(lambda: supervisor.state is GatewayState.STOPPED)
        assert supervisor.reason == "probe"
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL)
        assert supervisor.state is GatewayState.STOPPED
        supervisor.on_heartbeat("boot-bbbbbbbb", FULL)
        assert supervisor.state is GatewayState.STARTING
    assert control.stops >= 1


async def test_the_probe_needs_the_turn_and_then_the_hosts_block_message(make):
    supervisor, _, _ = make(timing=HostTiming(first_heartbeat=1.0, probe=0.3, tick=0.01))
    async with running(supervisor):
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL)
        await wait_until(lambda: supervisor.probe_id is not None)
        supervisor.on_probe_blocked()  # a block message before the turn proves nothing
        supervisor.on_probe_turn(supervisor.probe_id)  # the turn, but the host never wrote the block
        await wait_until(lambda: supervisor.state is GatewayState.STOPPED)
    assert supervisor.reason == "probe"


async def test_a_gateway_that_is_not_ready_fails_the_gate(make):
    supervisor, _, _ = make(control=FakeControl(ready="readyz"))
    async with running(supervisor):
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL)
        await wait_until(lambda: supervisor.state is GatewayState.STOPPED)
    assert supervisor.reason == "readyz"


async def test_silence_holds_and_the_same_boot_brings_running_back(make):
    mono = Clock(1000.0)
    supervisor, alerts, _ = make(mono=mono)
    async with running(supervisor):
        await gate(supervisor)
        assert supervisor.may_serve(1, "text")
        mono.advance(16)
        assert not supervisor.may_release()  # sends wait at once (spec 4.2)
        assert not supervisor.may_serve(1, "text")  # and so do new messages
        assert supervisor.state is GatewayState.RUNNING  # a short pause is no alarm
        mono.advance(165)
        await wait_until(lambda: supervisor.state is GatewayState.HOLD)
        assert (supervisor.reason, alerts.raised) == ("silent", [("host_silent", {})])
        assert supervisor.turns_blocked()
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL)
        assert supervisor.state is GatewayState.RUNNING
        assert supervisor.may_release()


async def test_a_new_boot_passes_the_gate_again_without_alerts(make):
    supervisor, alerts, _ = make()
    async with running(supervisor):
        await gate(supervisor)
        supervisor.on_heartbeat("boot-bbbbbbbb", FULL)  # the gateway restarted (scenario 41)
        assert supervisor.state is GatewayState.STARTING
        assert not supervisor.may_release()  # a boot that has not passed the gate sends nothing yet
        await wait_until(lambda: supervisor.probe_id is not None)
        supervisor.on_probe_turn(supervisor.probe_id)
        supervisor.on_probe_blocked()
        await wait_until(lambda: supervisor.state is GatewayState.RUNNING)
    assert supervisor.gated_boot == "boot-bbbbbbbb"
    assert supervisor.may_release()
    assert alerts.raised == []


async def test_a_bad_heartbeat_while_running_stops_the_gateway(make):
    control = FakeControl()
    supervisor, alerts, _ = make(control=control)
    async with running(supervisor):
        await gate(supervisor)
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL | {"model": "claude-cli/opus"})
        await wait_until(lambda: supervisor.state is GatewayState.STOPPED)
    assert supervisor.reason == "heartbeat:model"
    assert ("host_failed", {"reason": "heartbeat:model"}) in alerts.raised
    assert control.stops == 1


async def test_pause_holds_until_resume_even_across_restarts(make):
    supervisor, _, _ = make()
    async with running(supervisor):
        await gate(supervisor)
        supervisor.pause()
        assert (supervisor.state, supervisor.reason) == (GatewayState.HOLD, "paused")
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL)
        assert supervisor.state is GatewayState.HOLD
    again, _, _ = make()
    assert (again.state, again.paused, again.describe()) == (GatewayState.HOLD, True, "host_paused")
    again.resume()
    assert (again.state, again.paused) == (GatewayState.STARTING, False)


async def test_a_host_that_stops_polling_raises_an_alert(make):
    mono = Clock(1000.0)
    supervisor, alerts, _ = make(mono=mono, timing=HostTiming(tick=0.01, not_polling=300))
    async with running(supervisor):
        await gate(supervisor)
        supervisor.on_poll()
        mono.advance(200)
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL)
        await asyncio.sleep(0.05)
        assert alerts.raised == []
        mono.advance(101)
        supervisor.on_heartbeat("boot-aaaaaaaa", FULL)
        await wait_until(lambda: ("host_not_polling", {}) in alerts.raised)


async def test_people_get_the_hold_reply_while_the_host_is_on_hold(make):
    calls = []
    supervisor, _, _ = make(on_hold=lambda: calls.append(1), timing=HostTiming(first_heartbeat=0.1, tick=0.01))
    async with running(supervisor):
        await wait_until(lambda: supervisor.state is GatewayState.STOPPED)
        await wait_until(lambda: len(calls) >= 2)


def test_describe_names_a_service_text(make):
    supervisor, _, _ = make()
    assert supervisor.describe() == "host_starting"
    supervisor.state = GatewayState.HOLD
    assert supervisor.describe() == "host_hold"


async def test_core_starts_its_own_gateway_and_a_failed_start_stops_it(make):
    control = FakeControl(managed=True)
    supervisor, alerts, _ = make(control=control, timing=managed())
    async with running(supervisor):
        await wait_until(lambda: control.starts == 1)
        await gate(supervisor)
    assert supervisor.gated_process == "process-1"
    broken = FakeControl(managed=True, start="config: not installed")
    supervisor, alerts, _ = make(control=broken, timing=managed())
    async with running(supervisor):
        await wait_until(lambda: supervisor.state is GatewayState.STOPPED)
    assert alerts.raised == [("host_failed", {"reason": "start: config: not installed"})]


async def test_a_gateway_that_exits_is_started_again_and_gated_again(make):
    control = FakeControl(managed=True)
    supervisor, alerts, _ = make(control=control, timing=managed())
    async with running(supervisor):
        await gate(supervisor)
        control.running = None  # a clean exit: launchd leaves it down
        await wait_until(lambda: control.starts == 2)
        assert supervisor.state is GatewayState.STARTING
        assert not supervisor.may_serve(1, "text")
        await gate(supervisor, boot="boot-bbbbbbbb")
    assert ("host_exited", {}) in alerts.raised
    assert supervisor.gated_process == "process-2"


async def test_a_new_gateway_process_is_gated_before_its_first_heartbeat(make):
    control = FakeControl(managed=True)
    supervisor, alerts, _ = make(control=control, timing=managed())
    async with running(supervisor):
        await gate(supervisor)
        control.running = "process-crashed-and-restarted"  # launchd brought it back within a second
        await wait_until(lambda: supervisor.state is GatewayState.STARTING)
        assert supervisor.reason == "new_process"
        assert not supervisor.may_serve(1, "text")
    assert alerts.raised == []


async def test_a_gateway_core_stopped_stays_down_until_resume(make):
    control = FakeControl(managed=True)
    supervisor, alerts, _ = make(control=control, timing=managed(first_heartbeat=0.2))
    async with running(supervisor):
        await wait_until(lambda: supervisor.state is GatewayState.STOPPED)
        await asyncio.sleep(0.1)
        assert (control.starts, control.stops) == (1, 1)  # stopped for good: no restart, no host_exited
        supervisor.resume()
        await wait_until(lambda: control.starts == 2)
        await gate(supervisor)
    assert [name for name, _ in alerts.raised] == ["host_failed"]


async def test_a_gateway_that_exits_while_paused_comes_back_paused(make):
    control = FakeControl(managed=True)
    supervisor, _, _ = make(control=control, timing=managed())
    async with running(supervisor):
        await gate(supervisor)
        supervisor.pause()
        control.running = None
        await wait_until(lambda: control.starts == 2)
        await asyncio.sleep(0.05)
        assert supervisor.state is GatewayState.HOLD
    supervisor.resume()
    assert supervisor.state is GatewayState.STARTING


async def test_an_unreadable_launchd_answer_never_restarts_the_gateway(make):
    control = FakeControl(managed=True)
    supervisor, alerts, _ = make(control=control, timing=managed())
    async with running(supervisor):
        await gate(supervisor)
        control.running = "unknown"
        await asyncio.sleep(0.1)
        assert supervisor.state is GatewayState.RUNNING
    assert (control.starts, alerts.raised) == (1, [])


async def test_a_gateway_that_keeps_exiting_is_stopped(make):
    """Each time it passes the gate and then exits by itself; the third time in ten minutes Core stops it."""
    control = FakeControl(managed=True)
    supervisor, alerts, _ = make(control=control, timing=managed())
    async with running(supervisor):
        for boot in ("boot-aaaaaaaa", "boot-bbbbbbbb", "boot-cccccccc"):
            await gate(supervisor, boot=boot)
            control.running = None  # a clean exit: launchd leaves it down
            await wait_until(lambda: control.running is not None or supervisor.state is GatewayState.STOPPED)
    assert supervisor.state is GatewayState.STOPPED
    assert supervisor.reason == "exited_repeatedly"
    assert [name for name, _ in alerts.raised] == ["host_exited", "host_exited", "host_failed"]
    assert (control.starts, control.stops) == (3, 1)
