import asyncio
import json
import os
import socket
import stat

import pytest

from fakeadapter import FakeAdapter
from helpers import MEMBER, OWNER, wait_until
from klepa_core.events import EventLog
from klepa_core.host.adapter import AdapterServer, BootSequence, probe_peer, sign_body
from klepa_core.host.instruction import TOOLS, instruction
from klepa_core.host.queue import HostQueue
from klepa_core.host.supervisor import PROBE_BLOCK, HostTiming, Supervisor
from klepa_core.host.tools import ToolBox, signature
from klepa_core.host.turns import TurnRegistry

KEY = b"k" * 32


class FakeAlerts:
    def __init__(self):
        self.raised = []

    def raise_(self, name, **fields):
        self.raised.append((name, fields))
        return True


def server_for(path, cfg, conn, queue, turns, events, supervisor, alerts=None):
    persons = {member.telegram_id: member.person_id for member in cfg.members}
    names = {member.person_id: member.name for member in cfg.members}
    tools = ToolBox(conn, KEY, persons, names)
    return AdapterServer(
        path, KEY, supervisor, turns, queue, events, cfg.locale, instruction("en"), tools, alerts=alerts
    )


def everything(host_message_id, kind):
    return True


@pytest.fixture
async def server(core_db, short_dir):
    cfg, conn, journal = core_db
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), probe_peer(KEY))
    turns = TurnRegistry(conn, queue)
    events = EventLog(conn)
    supervisor = Supervisor(conn, queue, events, timing=HostTiming(first_heartbeat=60, probe=60, tick=0.01))
    path = short_dir / "run" / "adapter.sock"
    adapter_server = server_for(path, cfg, conn, queue, turns, events, supervisor, FakeAlerts())
    stop = asyncio.Event()
    task = asyncio.create_task(adapter_server.serve_forever(stop))
    await wait_until(path.exists)
    yield adapter_server, FakeAdapter(path, KEY), conn, journal
    stop.set()
    await asyncio.wait_for(task, 5)


def issue(adapter_server, journal, update_id, sender, message_id):
    message = {"message_id": message_id, "date": 1, "chat": {"id": sender}, "from": {"id": sender}, "text": "hi"}
    journal.append_batch([{"update_id": update_id, "message": message}], "t")
    adapter_server.queue.enqueue(update_id, sender, message_id, sender, 1)
    adapter_server.queue.serve(None, 100, everything)


async def test_an_unsigned_or_foreign_message_changes_nothing(server):
    adapter_server, adapter, conn, journal = server
    issue(adapter_server, journal, 1, OWNER, 10)
    body = json.dumps({"type": "prompt_built", "boot_id": "boot-aaaaaaaa", "seq": 1, "run_id": "r1"}).encode()
    assert (await adapter.post(body, None))[0] == 401
    assert (await adapter.post(body, sign_body(b"x" * 32, body)))[0] == 401
    assert conn.execute("SELECT COUNT(*) FROM host_run").fetchone()[0] == 0
    assert adapter_server.boots.boot_id is None  # the sequence did not move either
    assert "adapter_rejected" in EventLog(conn).kinds()


async def test_a_signature_header_that_is_not_ascii_is_refused_cleanly(server):
    _, adapter, _, _ = server
    body = json.dumps({"type": "heartbeat", "boot_id": "boot-aaaaaaaa", "seq": 1}).encode()
    assert (await adapter.post(body, "é" * 64))[0] == 401


async def test_answers_are_signed_and_carry_the_pair(server):
    _, adapter, _, _ = server
    status, body = await adapter.heartbeat()
    assert (status, body["ok"], body["boot_id"], body["seq"]) == (200, True, adapter.boot_id, 1)


async def test_a_repeat_is_refused_and_a_replaced_boot_never_comes_back(server):
    _, adapter, _, _ = server

    def signed(seq, boot_id=None):
        body = json.dumps({"type": "heartbeat", "boot_id": boot_id or adapter.boot_id, "seq": seq}).encode()
        return body, sign_body(KEY, body)

    assert (await adapter.post(*signed(5)))[0] == 200
    assert (await adapter.post(*signed(5)))[0] == 409  # a repeat
    assert (await adapter.post(*signed(4)))[0] == 200  # sent at the same moment, arrived later: fine
    old = adapter.boot_id
    adapter.restart()
    assert (await adapter.heartbeat())[0] == 200
    assert (await adapter.post(*signed(6, old)))[0] == 409


@pytest.mark.parametrize(
    "message",
    [
        {"type": "heartbeat", "boot_id": "short", "seq": 1},
        {"type": "heartbeat", "boot_id": "boot-aaaaaaaa"},
        {"type": "heartbeat", "boot_id": "boot-aaaaaaaa", "seq": True},
        {"type": "reboot", "boot_id": "boot-aaaaaaaa", "seq": 1},
        ["not", "an", "object"],
    ],
)
async def test_malformed_messages_are_refused(server, message):
    _, adapter, _, _ = server
    body = json.dumps(message).encode()
    assert (await adapter.post(body, sign_body(KEY, body)))[0] == 400


async def test_before_dispatch_lets_every_issued_message_go_on_to_a_turn(server):
    adapter_server, adapter, _, _ = server
    assert (await adapter.dispatch(OWNER, 10))[1]["handled"] is False
    assert (await adapter.dispatch(adapter_server.queue.probe_peer, 1))[1]["handled"] is False


async def test_a_built_prompt_gets_cores_instruction_its_tools_and_the_failure_text(server):
    adapter_server, adapter, _, _ = server
    _, answer = await adapter.prompt_built("run-1", MEMBER)
    assert answer["instruction"] == instruction("en")
    assert answer["tools_allow"] == list(TOOLS)
    assert answer["failure"].startswith("I couldn't answer just now")
    _, probe = await adapter.prompt_built("run-p", adapter_server.queue.probe_peer)
    assert probe["tools_allow"] == []  # the probes need no tool


async def test_a_registered_turn_reaches_the_model(server):
    adapter_server, adapter, conn, journal = server
    issue(adapter_server, journal, 1, MEMBER, 10)
    await adapter.prompt_built("run-1", MEMBER)
    status, turn = await adapter.turn_start("run-1", MEMBER)
    assert (status, turn["outcome"]) == (200, "pass")
    assert conn.execute("SELECT host_message_id FROM host_run WHERE run_id='run-1'").fetchone()[0] is not None
    assert "turn_registered" in EventLog(conn).kinds()


async def test_a_turn_on_hold_is_blocked_with_the_hold_text(server):
    adapter_server, adapter, _, journal = server
    issue(adapter_server, journal, 1, MEMBER, 10)
    adapter_server.supervisor.pause()
    await adapter.prompt_built("run-1", MEMBER)
    _, turn = await adapter.turn_start("run-1", MEMBER)
    assert (turn["outcome"], turn["message"]) == ("block", adapter_server.locale.text("hold"))


async def test_a_failed_turn_of_a_person_alerts_the_owner_and_a_blocked_one_does_not(server):
    adapter_server, adapter, conn, journal = server
    issue(adapter_server, journal, 1, MEMBER, 10)
    await adapter.prompt_built("run-1", MEMBER)
    await adapter.turn_start("run-1", MEMBER)
    await adapter.turn_reply("run-1", error=True, error_kind="auth")
    await adapter.turn_reply("run-unknown", error=True, error_kind="auth")  # a run Core never registered
    # A refused token also raises its own alert: an hourly host_turn_failed of another reason must not hide it.
    assert adapter_server.alerts.raised == [
        ("host_turn_failed", {"reason": "auth"}),
        ("host_model_auth", {"reason": "auth"}),
    ]
    assert conn.execute("SELECT succeeded FROM host_run WHERE run_id='run-1'").fetchone()[0] == 0


async def test_a_busy_provider_in_a_persons_turn_is_no_token_alert(server):
    adapter_server, adapter, _, journal = server
    issue(adapter_server, journal, 1, MEMBER, 10)
    await adapter.prompt_built("run-1", MEMBER)
    await adapter.turn_start("run-1", MEMBER)
    await adapter.turn_reply("run-1", error=True, error_kind="rate_limit")
    assert adapter_server.alerts.raised == [("host_turn_failed", {"reason": "rate_limit"})]


async def test_the_model_probe_hears_how_its_own_turn_replied(server):
    adapter_server, adapter, _, _ = server
    supervisor = adapter_server.supervisor
    peer = adapter_server.queue.probe_peer
    cases = (
        (False, None, (True, "other")),
        (True, "rate_limit", (False, "rate_limit")),
        (True, "Odd!", (False, "other")),
    )
    for n, (error, kind, expected) in enumerate(cases):
        supervisor.model_probe_id = adapter_server.queue.add_probe("model_probe")
        supervisor._model_reply = None
        adapter_server.queue.serve(None, 100, everything)
        await adapter.prompt_built(f"run-{n}", peer)
        assert (await adapter.turn_start(f"run-{n}", peer))[1]["outcome"] == "pass"
        await adapter.turn_reply(f"run-{n}", error=error, error_kind=kind)
        assert supervisor._model_reply == expected
        adapter_server.queue.retire(supervisor.model_probe_id)
    # the reply of an earlier probe's turn, delivered late, is not the current probe's
    supervisor.model_probe_id = adapter_server.queue.add_probe("model_probe")
    supervisor._model_reply = None
    await adapter.turn_reply("run-0", error=True, error_kind="auth")
    assert supervisor._model_reply is None
    assert adapter_server.alerts.raised == []  # the probe's failure is the gate's to report


async def test_a_tool_that_breaks_tells_the_model_and_core_goes_on(server, short_dir, monkeypatch):
    import aiohttp

    adapter_server, _, conn, _ = server

    def broken(name, arguments):
        raise KeyError("a bug")

    monkeypatch.setattr(adapter_server.tools, "call", broken)
    connector = aiohttp.UnixConnector(path=str(short_dir / "run" / "adapter.sock"))
    call = {"name": "search", "arguments": {}}
    async with (
        aiohttp.ClientSession(connector=connector) as session,
        session.post("http://core/v1/tool", json=call) as response,
    ):
        answer = (response.status, await response.json())
    assert answer == (200, {"isError": True, "content": [{"type": "text", "text": "Klepa's tool failed."}]})
    assert EventLog(conn).kinds().count("tool_failed") == 1


async def test_cores_tools_are_listed_and_a_signed_call_runs(server, short_dir):
    import aiohttp

    adapter_server, adapter, conn, journal = server
    issue(adapter_server, journal, 1, MEMBER, 10)
    await adapter.prompt_built("run-1", MEMBER)
    await adapter.turn_start("run-1", MEMBER)
    connector = aiohttp.UnixConnector(path=str(short_dir / "run" / "adapter.sock"))
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get("http://core/v1/tools") as response:
            listed = await response.json()
        params = {"query": "insurance"}
        sig = signature(KEY, "run-1", "call-1", "search", params)
        call = {
            "name": "search",
            "arguments": params | {"_klepa": {"run_id": "run-1", "tool_call_id": "call-1", "sig": sig}},
        }
        async with session.post("http://core/v1/tool", json=call) as response:
            ran = await response.json()
        async with session.post("http://core/v1/tool", json={"name": "search", "arguments": params}) as response:
            refused = await response.json()
    assert sorted(tool["name"] for tool in listed["tools"]) == ["get", "search", "send_original"]
    assert ran == {"isError": False, "content": [{"type": "text", "text": '{"results": []}'}]}
    assert refused["isError"] is True
    assert EventLog(conn).kinds().count("tool_refused") == 1


async def test_a_turn_without_an_issued_message_is_refused_and_logged(server):
    _, adapter, conn, _ = server
    await adapter.prompt_built("run-1", OWNER)
    _, turn = await adapter.turn_start("run-1", OWNER)
    assert turn["outcome"] == "block"
    row = conn.execute("SELECT data FROM event_log WHERE kind='turn_refused'").fetchone()
    assert json.loads(row[0])["reason"] == "no_issued_message"


async def test_the_probe_turn_passes_the_probe(server):
    adapter_server, adapter, _, _ = server
    supervisor = adapter_server.supervisor
    supervisor.probe_id = adapter_server.queue.add_probe()
    adapter_server.queue.serve(None, 100, everything)
    peer = adapter_server.queue.probe_peer
    await adapter.prompt_built("run-p", peer)
    _, turn = await adapter.turn_start("run-p", peer)
    assert turn == {"outcome": "block", "message": PROBE_BLOCK, "boot_id": adapter.boot_id, "seq": adapter.seq}
    assert supervisor._probe_turn == supervisor.probe_id


async def test_the_socket_is_private(server, short_dir):
    assert stat.S_IMODE(os.stat(short_dir / "run" / "adapter.sock").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(short_dir / "run").st_mode) == 0o700


async def test_a_file_in_the_sockets_place_is_never_removed(core_db, short_dir):
    cfg, conn, journal = core_db
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), probe_peer(KEY))
    events = EventLog(conn)
    supervisor = Supervisor(conn, queue, events)
    (short_dir / "run").mkdir(mode=0o700)
    path = short_dir / "run" / "adapter.sock"
    path.write_text("not a socket")
    adapter_server = server_for(path, cfg, conn, queue, TurnRegistry(conn, queue), events, supervisor)
    with pytest.raises(OSError):  # noqa: PT011 - the bind fails, whatever errno the platform picks
        await adapter_server.serve_forever(asyncio.Event())
    assert path.read_text() == "not a socket"


async def test_a_socket_left_by_a_stopped_core_is_replaced(core_db, short_dir):
    (short_dir / "run").mkdir(mode=0o700)
    path = short_dir / "run" / "adapter.sock"
    leftover = socket.socket(socket.AF_UNIX)
    leftover.bind(str(path))
    leftover.close()
    cfg, conn, journal = core_db
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), probe_peer(KEY))
    events = EventLog(conn)
    adapter_server = server_for(
        path, cfg, conn, queue, TurnRegistry(conn, queue), events, Supervisor(conn, queue, events)
    )
    stop = asyncio.Event()
    task = asyncio.create_task(adapter_server.serve_forever(stop))
    adapter = FakeAdapter(path, KEY)
    await wait_until(lambda: adapter_server.boots.boot_id is None and path.exists())
    for _ in range(50):
        try:
            assert (await adapter.heartbeat())[0] == 200
            break
        except OSError:
            await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 5)


def test_boot_sequence_takes_each_seq_once_within_a_window():
    boots = BootSequence(keep=2, window=4)
    assert boots.accept("a" * 8, 10)
    assert not boots.accept("a" * 8, 10)
    assert boots.accept("a" * 8, 8)
    assert not boots.accept("a" * 8, 6)  # too far behind the newest
    assert boots.accept("b" * 8, 0)
    assert not boots.accept("a" * 8, 11)  # a replaced boot


def test_probe_peer_is_a_stable_telegram_shaped_id():
    peer = probe_peer(KEY)
    assert peer == probe_peer(KEY)
    assert 2**51 <= peer < 2**52
    assert peer != probe_peer(b"z" * 32)


async def test_a_refusal_is_logged_without_what_the_model_wrote(server, short_dir):
    """Events keep reasons, never parameters: a parameter name the model made up may carry a person's data, and
    core.db goes into the snapshots. The model still learns what it got wrong."""
    import aiohttp

    adapter_server, adapter, conn, journal = server
    issue(adapter_server, journal, 1, MEMBER, 10)
    await adapter.prompt_built("run-1", MEMBER)
    await adapter.turn_start("run-1", MEMBER)
    params = {"query": "x", "passport 4509 123456": "1"}
    sig = signature(KEY, "run-1", "call-1", "search", params)
    call = {
        "name": "search",
        "arguments": params | {"_klepa": {"run_id": "run-1", "tool_call_id": "call-1", "sig": sig}},
    }
    connector = aiohttp.UnixConnector(path=str(short_dir / "run" / "adapter.sock"))
    async with (
        aiohttp.ClientSession(connector=connector) as session,
        session.post("http://core/v1/tool", json=call) as response,
    ):
        answer = await response.json()
    assert answer["isError"] is True
    assert "passport 4509 123456" in answer["content"][0]["text"]
    logged = [row[0] for row in conn.execute("SELECT data FROM event_log WHERE kind='tool_refused'")]
    assert logged == ['{"reason": "unknown parameters"}']
