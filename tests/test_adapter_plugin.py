"""The real adapter plugin, run by Node, against Core's real adapter server: the contract between the two languages.

Skipped where no Node of version 22.18 or later is on PATH: it runs TypeScript without a build step. CI has one.
"""

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import MEMBER, wait_until
from klepa_core.events import EventLog
from klepa_core.host.adapter import AdapterServer, probe_peer
from klepa_core.host.instruction import TOOLS, instruction
from klepa_core.host.queue import HostQueue
from klepa_core.host.supervisor import PROBE_BLOCK, Expectations, HostTiming, Supervisor
from klepa_core.host.tools import ToolBox, signature
from klepa_core.host.turns import TurnRegistry

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "core" / "klepa_core" / "host" / "openclaw" / "adapter" / "index.ts"
DRIVER = REPO / "tests" / "adapter" / "drive.ts"
KEY = b"k" * 32


def node() -> str | None:
    path = shutil.which("node")
    if path is None:
        return None
    version = subprocess.run([path, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    major, minor = (int(part) for part in version.lstrip("v").split(".")[:2])
    return path if (major, minor) >= (22, 18) else None


pytestmark = pytest.mark.skipif(node() is None, reason="needs Node 22.18 or later on PATH")


async def test_the_plugin_speaks_cores_language(core_db, short_dir):
    cfg, conn, journal = core_db
    probe = probe_peer(KEY)
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), probe)
    turns = TurnRegistry(conn, queue)
    events = EventLog(conn)
    expect = Expectations(
        plugin_sha256=hashlib.sha256(PLUGIN.read_bytes()).hexdigest(),
        policy={"tools.profile": "minimal"},
        model="anthropic/claude-sonnet-5",
        runtime="openclaw",
    )
    supervisor = Supervisor(conn, queue, events, expect=expect, timing=HostTiming(first_heartbeat=60, probe=60))
    socket_path = short_dir / "run" / "adapter.sock"
    persons = {member.telegram_id: member.person_id for member in cfg.members}
    names = {member.person_id: member.name for member in cfg.members}
    tools = ToolBox(conn, KEY, persons, names)
    server = AdapterServer(socket_path, KEY, supervisor, turns, queue, events, cfg.locale, instruction("en"), tools)
    message = {"message_id": 10, "date": 1, "chat": {"id": MEMBER}, "from": {"id": MEMBER}, "text": "hi"}
    journal.append_batch([{"update_id": 1, "message": message}], "t")
    queue.enqueue(1, MEMBER, 10, MEMBER, 1)
    key_file = short_dir / "adapter.key"
    key_file.write_bytes(KEY)
    key_file.chmod(0o600)
    supervisor.probe_id = queue.add_probe()
    queue.serve(None, 100, lambda host_message_id, kind: True)  # the gatekeeper gave the host the probe
    stop = asyncio.Event()
    task = asyncio.create_task(server.serve_forever(stop))
    try:
        await wait_until(socket_path.exists)
        process = await asyncio.create_subprocess_exec(
            str(node()),
            str(DRIVER),
            str(socket_path),
            str(key_file),
            str(MEMBER),
            str(probe),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, err.decode()
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    lines = [line for line in out.decode().split("\n") if line]  # not splitlines(): U+2028 is inside a value
    steps = {line["step"]: line["result"] for line in map(json.loads, lines)}
    assert steps["registered"] == [
        "before_dispatch",
        "before_prompt_build",
        "before_agent_run",
        "before_tool_call",
        "reply_payload_sending",
    ]
    assert steps["member_dispatch"] is None  # every issued message goes on to a turn
    assert steps["probe_dispatch"] is None
    assert steps["probe_prompt_built"] == {"appendSystemContext": instruction("en"), "toolsAllow": []}
    assert steps["probe_turn"] == {"outcome": "block", "reason": "klepa", "message": PROBE_BLOCK}
    assert steps["probe_reply"] == {"payload": {"text": "the block", "isError": True}}  # without OpenClaw's wrapper
    assert steps["member_prompt_built"] == {"appendSystemContext": instruction("en"), "toolsAllow": list(TOOLS)}
    assert steps["member_turn"] == {"outcome": "pass"}
    signed = steps["member_tool"]["params"]
    klepa = signed.pop("_klepa")
    assert signed == {"query": 'Διαβατήριο ✓ "x" \\ / \u2028 end', "limit": 3}
    assert (klepa["run_id"], klepa["tool_call_id"]) == ("run-member", "call-1")  # the model's own is replaced
    assert klepa["sig"] == signature(KEY, "run-member", "call-1", "search", signed)  # the same bytes in both languages
    assert steps["foreign_tool"]["block"] is True
    assert steps["direct_tool"]["block"] is True
    assert steps["member_failed"]["payload"]["text"] == cfg.locale.text("turn_failed")
    assert steps["member_media"] == {"payload": {"text": "here"}}
    assert events.kinds().count("turn_failed") == 1  # the plugin reported the failed reply
    assert supervisor.last_heartbeat is not None  # the heartbeat passed Core's check: hash, policy, model, runtime
    assert supervisor._probe_turn == supervisor.probe_id
    assert "adapter_rejected" not in events.kinds()
