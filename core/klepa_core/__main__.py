"""klepa-core command line: `init` and `run` (stage 1a)."""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from .app import AlreadyRunning, init_layout, run_service
from .config import Config, ConfigError, load_config
from .keys import KeyFileError


async def _run_with_signals(cfg: Config) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run_service(cfg, stop)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="klepa-core")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "run"):
        commands.add_parser(name).add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        cfg = load_config(args.config)
        if args.command == "init":
            init_layout(cfg)
            print("klepa-core: initialized")
            return 0
        return asyncio.run(_run_with_signals(cfg))
    except (ConfigError, KeyFileError) as exc:
        print(f"klepa-core: {exc}", file=sys.stderr)
        return 2
    except AlreadyRunning:
        print("klepa-core: another instance is running", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
