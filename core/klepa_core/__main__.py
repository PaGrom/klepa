"""klepa-core command line: `init` and `run` (stage 1a)."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

import aiohttp

from . import db
from .app import AlreadyRunning, acquire_lock, init_layout, run_service
from .config import Config, ConfigError, load_config, read_service_token
from .keys import KeyFileError, ensure_private_dir
from .servicebot import bind_owner_chat, new_bind_code
from .telegram.client import BotApi


async def _run_with_signals(cfg: Config) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run_service(cfg, stop)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="klepa-core")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "run"):
        commands.add_parser(name).add_argument("--config", required=True, type=Path)
    service_bot = commands.add_parser("service-bot").add_subparsers(dest="action", required=True)
    service_bot.add_parser("bind").add_argument("--config", required=True, type=Path)
    return parser


async def _bind(cfg: Config, token: str) -> int:
    code = new_bind_code()
    conn = db.connect(cfg.core_db_path)
    try:
        async with aiohttp.ClientSession() as session:
            api = BotApi(session, cfg.service_api_root, token)
            me = await api.get_me()
            print(f"From the owner's Telegram account open https://t.me/{me.get('username')}?start={code}")
            print(f"or send the service bot: /start {code}")
            chat = await bind_owner_chat(cfg, api, conn, code, lambda q: input(q).strip().lower() in {"y", "yes"})
    finally:
        conn.close()
    if chat is None:
        print("klepa-core: the service bot was not bound", file=sys.stderr)
        return 4
    print("klepa-core: the service bot is bound to the owner's chat")
    return 0


def _bind_service_bot(cfg: Config) -> int:
    ensure_private_dir(cfg.data_dir)
    lock_fd = acquire_lock(cfg.data_dir / "core.lock")  # Core must not poll the service bot at the same time
    try:
        init_layout(cfg)
        return asyncio.run(_bind(cfg, read_service_token(cfg)))
    finally:
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    os.umask(0o077)
    try:
        cfg = load_config(args.config)
        if args.command == "init":
            init_layout(cfg)
            cfg.documents_dir.mkdir(parents=True, exist_ok=True)
            print("klepa-core: initialized")
            return 0
        if args.command == "service-bot":
            return _bind_service_bot(cfg)
        return asyncio.run(_run_with_signals(cfg))
    except (ConfigError, KeyFileError) as exc:
        print(f"klepa-core: {exc}", file=sys.stderr)
        return 2
    except AlreadyRunning:
        print("klepa-core: another instance is running", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
