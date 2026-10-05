"""The owner's commands for the gateway (spec 7.1): install its runtime, give it the model, show its state, remove it.

Core itself starts, checks and watches the gateway; these commands only prepare it. A secret is never an argument
and never printed: the model's token is read without echo and handed to OpenClaw on stdin.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from .. import macos
from ..app import AlreadyRunning, acquire_lock
from ..config import Config, bot_id, read_token
from ..keys import ensure_host_token, ensure_private_dir, load_or_create_key
from . import gateway, reference
from . import runtime as rt
from .adapter import probe_peer
from .supervisor import GatewayUnknown

PROFILE_ID = reference.MODEL_PROFILE
SETUP_TOKEN_PREFIX = "sk-ant-oat01-"  # OpenClaw takes only Anthropic's setup-token here, from `claude setup-token`
SETUP_TOKEN_MIN_LENGTH = 80
MODEL_API = ("api.anthropic.com", 443)


def _parts(cfg: Config) -> tuple[rt.Runtime, reference.HostLayout]:
    if cfg.host is None or cfg.host.gateway != "launchd":
        raise rt.HostRuntimeError('the config needs a [host] section with gateway = "launchd"')
    return rt.Runtime(cfg.host.runtime_dir), reference.layout_of(cfg)


def install(
    cfg: Config,
    *,
    say: Callable[[str], None] = print,
    run: rt.Runner = rt._run,
    fetch: rt.Fetch = rt._fetch,
    exclude: Callable[[Path], str | None] = macos.exclude_from_time_machine,
) -> rt.Runtime:
    """Everything the gateway needs before Core can start it; safe to run again, it redoes only what is missing."""
    runtime, layout = _parts(cfg)
    assert cfg.host is not None
    ensure_private_dir(cfg.data_dir)
    ensure_host_token(cfg.host_token_path, bot_id(read_token(cfg)))
    key = load_or_create_key(cfg.adapter_key_path)
    runtime.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    rt.ensure_node(runtime, fetch=fetch, run=run, say=say)
    rt.ensure_openclaw(runtime, run=run, say=say)
    table = reference.table(reference.settings_of(cfg, probe_peer(key)), layout)
    gateway.prepare_host(runtime, layout, table, exclude=exclude, warn=lambda line: say(f"warning: {line}"))
    rt.validate_config(runtime, layout, run=run)
    rt.write_wrapper(runtime, layout)
    rt.claim_ownership(runtime, layout, run=run)
    if MODEL_API not in cfg.host.egress_allow:
        say('warning: host.egress_allow lacks "api.anthropic.com:443"; the gateway cannot reach the model')
    return runtime


def login(cfg: Config, token: str, *, run: rt.Runner = rt._run) -> bool:
    """Store the model's setup-token in the gateway's own auth store (spec 7.1, the owner's action). True when a
    running gateway took it at once; a gateway that is not running reads it when Core starts it.

    OpenClaw stores the token first and then tries to record the profile in its config, which Core keeps read-only.
    That refusal is expected, and the profile list proves the token is stored. A running gateway keeps the credentials
    it read until it is asked to refresh them (OpenClaw's own command would ask only after the config write)."""
    runtime, layout = _parts(cfg)
    token = "".join(token.split())  # a terminal may wrap a long paste; OpenClaw drops whitespace inside it too
    if not token.startswith(SETUP_TOKEN_PREFIX) or len(token) < SETUP_TOKEN_MIN_LENGTH:
        raise rt.HostRuntimeError(
            "that is not a setup-token (sk-ant-oat01-…, from claude setup-token); nothing was stored"
        )
    if not layout.config_path.exists():
        raise rt.HostRuntimeError("the gateway is not installed: run klepa-core host install first")
    argv = rt.openclaw_argv(
        runtime, "models", "auth", "paste-token", "--provider", "anthropic", "--profile-id", PROFILE_ID
    )
    result = run(argv, env=rt.gateway_env(runtime, layout), input=token + "\n", timeout=120)
    if result.returncode != 0 and "externally managed" not in result.stderr:
        raise rt.HostRuntimeError(f"OpenClaw did not store the token: {result.stderr.strip()[-300:]}")
    if not has_model_access(cfg, run=run):
        raise rt.HostRuntimeError(f"OpenClaw does not list {PROFILE_ID} after storing it")
    params = json.dumps({"refresh": True, "agentId": "main"})
    try:
        rt.openclaw_json(runtime, layout, "gateway", "call", "models.authStatus", "--params", params, run=run)
    except rt.HostRuntimeError:
        return False  # no gateway runs now
    return True


def has_model_access(cfg: Config, *, run: rt.Runner = rt._run) -> bool:
    runtime, layout = _parts(cfg)
    return rt.model_token_stored(runtime, layout, run=run)


def status(cfg: Config, *, run: rt.Runner = rt._run, launchctl: gateway.Runner | None = None) -> list[str]:
    """How the gateway is, in a few lines, for the owner's terminal."""
    runtime, layout = _parts(cfg)
    node, openclaw = rt._node_version(runtime, run), rt._openclaw_version(runtime, run)
    lines = [f"runtime: Node {node or 'missing'}, OpenClaw {openclaw or 'missing'}"]
    lines.append(f"gateway: {_agent_line(launchctl or gateway._run)}")
    if openclaw is not None and layout.config_path.exists():
        access = "stored" if has_model_access(cfg, run=run) else "missing: run klepa-core host login"
        lines.append(f"model token ({PROFILE_ID}): {access}")
    lines.append(f"Core: {_core_view(cfg)}")
    return lines


def _agent_line(launchctl: gateway.Runner) -> str:
    try:
        agent = gateway.parse_print(launchctl(["launchctl", "print", _agent()]))
    except GatewayUnknown as exc:
        return f"launchd's answer is unreadable ({exc})"
    if not agent.loaded:
        return "not loaded (Core loads it when it runs)"
    if agent.process is None:
        return f"loaded, not running (last exit code {agent.last_exit})"
    return f"running, pid {agent.process.pid}, started {agent.process.runs} times"


def _core_view(cfg: Config) -> str:
    """The supervisor's latest state from Core's event log."""
    if not cfg.core_db_path.exists():
        return "no database yet"
    try:
        with closing(sqlite3.connect(f"file:{cfg.core_db_path}?mode=ro", uri=True)) as conn:
            row = conn.execute(
                "SELECT at, data FROM event_log WHERE kind='host_state' ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error as exc:
        return f"unreadable ({type(exc).__name__})"
    if row is None:
        return "has not supervised a gateway yet"
    data = json.loads(row[1])
    reason = f" ({data['reason']})" if data.get("reason") else ""
    return f"{data.get('state')}{reason} since {row[0]}"


def _agent() -> str:
    return f"gui/{os.getuid()}/{gateway.LABEL}"


def uninstall(cfg: Config, *, launchctl: gateway.Runner | None = None) -> None:
    """Unload the gateway for good; its runtime and folder stay. Core must not run: it would load the gateway again."""
    _, layout = _parts(cfg)
    try:
        lock = acquire_lock(cfg.data_dir / "core.lock")
    except AlreadyRunning:
        raise rt.HostRuntimeError("Core is running and would load the gateway again: stop Core first") from None
    try:
        (launchctl or gateway._run)(["launchctl", "bootout", _agent()])
        for name in (f"{gateway.LABEL}.plist", "applied.sha256"):
            (layout.host_dir / name).unlink(missing_ok=True)
    finally:
        os.close(lock)
