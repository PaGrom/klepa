"""Core on macOS: a launchd agent in the owner's session, and keys kept out of Time Machine.

KeepAlive restarts Core after a crash but not after a clean exit, so `service uninstall` stops it for good.
The interpreter given at install time is the one macOS asks about when Core first opens a protected
documents folder; give Core its own, so the permission belongs to Core alone.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import Config, read_service_token, read_token
from .keys import ensure_private_dir

LABEL = "klepa.core"
BOOTSTRAP_ATTEMPTS = 5
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ServiceError(Exception):
    """launchd refused the agent, or the interpreter cannot run Core."""


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _runner(run: Runner | None) -> Runner:
    return run or _run  # looked up at call time, so the test suite can replace _run once


def _target(uid: int | None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}"


def _agents(agents_dir: Path | None) -> Path:
    return agents_dir or Path.home() / "Library" / "LaunchAgents"


def plist_for(config_path: Path, python: Path, logs_dir: Path) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "klepa_core", "run", "--config", str(config_path)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        # A config error exits with 2; launchd tries again once a minute until it is fixed.
        "ThrottleInterval": 60,
        "StandardOutPath": str(logs_dir / "core.out.log"),
        "StandardErrorPath": str(logs_dir / "core.err.log"),
    }


def install(
    cfg: Config,
    config_path: Path,
    python: Path,
    *,
    agents_dir: Path | None = None,
    run: Runner | None = None,
    uid: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Check what Core will need, write the agent and load it: fail here, in the terminal, rather than in a
    restart loop under launchd."""
    runner = _runner(run)
    read_token(cfg)  # ConfigError when missing, empty or not 0600
    if cfg.service_token_file is not None:
        read_service_token(cfg)
    if runner([str(python), "-c", "import klepa_core"]).returncode != 0:
        raise ServiceError(f"{python} cannot import klepa_core; install the engine into it first")
    logs_dir = cfg.data_dir / "logs"
    ensure_private_dir(logs_dir)
    for name in ("core.out.log", "core.err.log"):
        log = logs_dir / name
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
    agents = _agents(agents_dir)
    agents.mkdir(parents=True, exist_ok=True)
    plist_path = agents / f"{LABEL}.plist"
    plist_path.write_bytes(plistlib.dumps(plist_for(config_path.resolve(), python, logs_dir)))
    plist_path.chmod(0o644)
    target = _target(uid)
    runner(["launchctl", "bootout", f"{target}/{LABEL}"])  # replace an older version; harmless when absent
    error = ""
    for _ in range(BOOTSTRAP_ATTEMPTS):
        result = runner(["launchctl", "bootstrap", target, str(plist_path)])
        if result.returncode == 0:
            return plist_path
        error = result.stderr.strip()
        sleep(1.0)  # right after bootout, launchd may still be tearing the old agent down (error 5)
    raise ServiceError(f"launchctl bootstrap failed: {error}")


def uninstall(*, agents_dir: Path | None = None, run: Runner | None = None, uid: int | None = None) -> None:
    _runner(run)(["launchctl", "bootout", f"{_target(uid)}/{LABEL}"])
    (_agents(agents_dir) / f"{LABEL}.plist").unlink(missing_ok=True)


def restart(*, run: Runner | None = None, uid: int | None = None) -> None:
    """Restart Core, for example after it was allowed into the documents folder."""
    result = _runner(run)(["launchctl", "kickstart", "-k", f"{_target(uid)}/{LABEL}"])
    if result.returncode != 0:
        raise ServiceError(f"launchctl kickstart failed: {result.stderr.strip()}")


def status(*, run: Runner | None = None, uid: int | None = None) -> str:
    result = _runner(run)(["launchctl", "print", f"{_target(uid)}/{LABEL}"])
    if result.returncode != 0:
        return "not installed"
    state = pid = None
    for raw in result.stdout.splitlines():
        line = raw.strip()
        # The first occurrences belong to the service itself; nested sections come later.
        if state is None and line.startswith("state = "):
            state = line.split("=", 1)[1].strip()
        elif pid is None and line.startswith("pid = "):
            pid = line.split("=", 1)[1].strip()
    return (state or "unknown") + (f", pid {pid}" if pid else "")


def exclude_from_time_machine(path: Path, *, run: Runner | None = None) -> str | None:
    """Keep `path` out of Time Machine backups (spec 5.3). Returns what went wrong, or None."""
    try:
        result = _runner(run)(["tmutil", "addexclusion", str(path)])
    except OSError as exc:
        return f"tmutil cannot run ({type(exc).__name__})"
    if result.returncode != 0:
        return result.stderr.strip() or f"tmutil exited with {result.returncode}"
    return None
