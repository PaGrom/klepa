"""The owner's commands for the gateway (spec 7.1): install its runtime, give it the model, show its state, remove it.

Core itself starts, checks and watches the gateway; these commands only prepare it. A secret is never an argument
and never printed: the model's token is read without echo and handed to OpenClaw on stdin.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from .. import macos
from ..app import AlreadyRunning, acquire_lock
from ..config import Config, bot_id, read_token
from ..keys import ensure_host_token, ensure_private_dir, load_or_create_key
from . import gateway, reference
from . import runtime as rt
from .supervisor import GatewayUnknown

LOGIN_SECONDS = 60.0  # how long host login waits for OpenClaw's state to be free, in all
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
    new = rt.state_is_new(layout)  # before the claim below, which writes OpenClaw's first state
    rt.ensure_node(runtime, fetch=fetch, run=run, say=say)
    rt.ensure_openclaw(runtime, run=run, say=say)
    table = reference.table(reference.settings_of(cfg, key), layout)
    gateway.prepare_host(runtime, layout, table, exclude=exclude, warn=lambda line: say(f"warning: {line}"))
    rt.validate_config(runtime, layout, run=run)
    rt.write_wrapper(runtime, layout)
    rt.claim_ownership(runtime, layout, run=run)
    if new:
        rt.mark_state_ready(layout)
    elif rt.state_version(layout) != rt.OPENCLAW_VERSION:
        _migrate(cfg, runtime, layout, run=run, say=say)
    if MODEL_API not in cfg.host.egress_allow:
        say('warning: host.egress_allow lacks "api.anthropic.com:443"; the gateway cannot reach the model')
    return runtime


def _migrate(
    cfg: Config, runtime: rt.Runtime, layout: reference.HostLayout, *, run: rt.Runner, say: Callable[[str], None]
) -> None:
    """A state an older OpenClaw left: Core must not run (it would load the gateway again), and the gateway goes
    down, so that Doctor may own the state; Core starts it again."""
    try:
        lock = acquire_lock(cfg.data_dir / "core.lock")
    except AlreadyRunning:
        raise rt.HostRuntimeError(
            f"OpenClaw {rt.OPENCLAW_VERSION} must migrate the gateway's state while Core is stopped: "
            "run klepa-core service uninstall, then host install again, then service install"
        ) from None
    try:
        run(["launchctl", "bootout", _agent()])  # an agent launchd does not have is already down
        rt.migrate_state(runtime, layout, run=run, say=say)
    finally:
        os.close(lock)


def allowed(cfg: Config) -> list[tuple[int, str]]:
    """The ids the gateway admits (`channels.telegram.allowFrom`), with whose each is: the members' stand-ins in their
    config order, then the live probe's peer (spec 4.6). Core writes them into its own gateway's config at each start;
    a gateway somebody else runs (gateway = "external") needs them by hand, and again after the adapter key changes,
    which changes every stand-in."""
    if cfg.host is None:
        raise rt.HostRuntimeError("the config needs a [host] section")
    ensure_private_dir(cfg.data_dir)
    key = load_or_create_key(cfg.adapter_key_path)
    whose = [*(member.person_id for member in cfg.members), "the live probe"]
    return list(zip(reference.settings_of(cfg, key).allowed, whose, strict=True))


_FRAME = re.compile(r"[\s\u2500-\u257f]")  # whitespace, and the box a terminal display may draw around the token
_TOKEN = re.compile(r"[A-Za-z0-9_-]*")


def setup_token(paste: str) -> str:
    """The setup-token in a paste, without the line breaks and the frame a terminal or a display put into it. A paste
    that holds anything else is refused: a token with a stray character in it would be stored, and fail at the start."""
    token = _FRAME.sub("", paste)
    if not _TOKEN.fullmatch(token):
        raise rt.HostRuntimeError("the paste holds more than a setup-token: paste only the token; nothing was stored")
    if not token.startswith(SETUP_TOKEN_PREFIX) or len(token) < SETUP_TOKEN_MIN_LENGTH:
        raise rt.HostRuntimeError(
            "that is not a setup-token (sk-ant-oat01-…, from claude setup-token); nothing was stored"
        )
    return token


def login(cfg: Config, token: str, *, run: rt.Runner = rt._run) -> str:
    """Store the model's setup-token in the gateway's own auth store (spec 7.1, the owner's action). "now" when a
    running gateway took it at once; "restarted" when login stopped Core's gateway for it and Core starts it again;
    "later" when no gateway runs, and it reads the token when Core starts it.

    OpenClaw stores the token first and then tries to record the profile in its config, which Core keeps read-only.
    That refusal is expected, and the profile list proves the token is stored. Since 2026.10 OpenClaw stores a token
    only while no gateway owns its state: login stops Core's gateway, holding the login lock (gateway.login_lock), so
    that Core starts nothing meanwhile, and leaves a mark that lets Core start it again without an alert."""
    runtime, layout = _parts(cfg)
    token = setup_token(token)
    if not layout.config_path.exists():
        raise rt.HostRuntimeError("the gateway is not installed: run klepa-core host install first")
    argv = rt.openclaw_argv(
        runtime, "models", "auth", "paste-token", "--provider", "anthropic", "--profile-id", PROFILE_ID
    )
    with gateway.login_lock(layout) as mark:
        result = run(argv, env=rt.gateway_env(runtime, layout), input=token + "\n", timeout=120)
        if result.returncode != 0 and rt.OFFLINE_OWNERSHIP in result.stdout + result.stderr:
            stop = run(["launchctl", "bootout", _agent()], timeout=60)
            if stop.returncode != 0:  # 3 or 113: launchd has no such agent, so not Core's gateway owns the state
                raise rt.HostRuntimeError(
                    "another OpenClaw owns the gateway's state, not Core's gateway: stop it, then log in again"
                )
            mark.stopped()
            result = _until_free(lambda: run(argv, env=rt.gateway_env(runtime, layout), input=token + "\n", timeout=60))
        elif result.returncode != 0 and _busy(result):
            result = _until_free(lambda: run(argv, env=rt.gateway_env(runtime, layout), input=token + "\n", timeout=60))
        if result.returncode != 0 and "externally managed" not in result.stderr:
            detail = (result.stderr.strip() or result.stdout.strip())[-300:]
            raise rt.HostRuntimeError(f"OpenClaw did not store the token: {detail}")
        stored = has_model_access(cfg, run=run)
        for _ in range(int(LOGIN_SECONDS)):
            if stored is not None:
                break
            time.sleep(1.0)
            stored = has_model_access(cfg, run=run)
        if stored is False:
            raise rt.HostRuntimeError(f"OpenClaw does not list {PROFILE_ID} after storing it")
        if mark.stopped_gateway:
            return "restarted"
    params = json.dumps({"refresh": True, "agentId": "main"})
    try:
        rt.openclaw_json(runtime, layout, "gateway", "call", "models.authStatus", "--params", params, run=run)
    except rt.HostRuntimeError:
        return "later"  # no gateway runs now
    return "now"


def _busy(result: subprocess.CompletedProcess[str]) -> bool:
    """OpenClaw refused because the state is not free yet: a gateway releasing it, or another offline command."""
    text = result.stdout + result.stderr
    return rt.OFFLINE_OWNERSHIP in text or "No local mutation was attempted" in text or " is busy" in text


def _until_free(attempt: Callable[[], subprocess.CompletedProcess[str]]) -> subprocess.CompletedProcess[str]:
    """Try again while OpenClaw's state is not free, LOGIN_SECONDS at most in all."""
    deadline = time.monotonic() + LOGIN_SECONDS
    result = attempt()
    while result.returncode != 0 and _busy(result) and time.monotonic() < deadline:
        time.sleep(1.0)
        result = attempt()
    return result


def has_model_access(cfg: Config, *, run: rt.Runner = rt._run) -> bool | None:
    runtime, layout = _parts(cfg)
    return rt.model_token_stored(runtime, layout, run=run)


def status(cfg: Config, *, run: rt.Runner = rt._run, launchctl: gateway.Runner | None = None) -> list[str]:
    """How the gateway is, in a few lines, for the owner's terminal."""
    runtime, layout = _parts(cfg)
    node, openclaw = rt._node_version(runtime, run), rt._openclaw_version(runtime, run)
    lines = [f"runtime: Node {node or 'missing'}, OpenClaw {openclaw or 'missing'}"]
    lines.append(f"gateway: {_agent_line(launchctl or gateway._run)}")
    if openclaw is not None and layout.config_path.exists():
        stored = has_model_access(cfg, run=run)
        access = {True: "stored", None: "not readable while the gateway runs"}.get(
            stored, "missing: run klepa-core host login"
        )
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
    """Whether Core runs, and the supervisor's latest state from Core's event log, which keeps it after Core stopped."""
    if not cfg.core_db_path.exists():
        return "no database yet"
    try:
        with closing(sqlite3.connect(f"file:{cfg.core_db_path}?mode=ro", uri=True)) as conn:
            row = conn.execute(
                "SELECT at, data FROM event_log WHERE kind='host_state' ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error as exc:
        return f"unreadable ({type(exc).__name__})"
    runs = _core_runs(cfg)
    if row is None:
        return "has not supervised a gateway yet" if runs else "not running; has not supervised a gateway yet"
    data = json.loads(row[1])
    reason = f" ({data['reason']})" if data.get("reason") else ""
    seen = f"{data.get('state')}{reason} since {row[0]}"
    return seen if runs else f"not running; its last state was {seen}"


def _core_runs(cfg: Config) -> bool:
    """Core holds its lock while it runs: taking it for a moment tells."""
    try:
        lock = acquire_lock(cfg.data_dir / "core.lock")
    except AlreadyRunning:
        return True
    os.close(lock)
    return False


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
