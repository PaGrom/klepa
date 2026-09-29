"""klepa-core command line."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

import aiohttp

from . import db, macos
from .app import AlreadyRunning, acquire_lock, init_layout, run_service
from .config import Config, ConfigError, load_config, read_service_token
from .keys import KeyFileError, ensure_private_dir, key_from_paper, load_or_create_key, paper_copy, restore_key
from .logs import configure_logging
from .servicebot import bind_owner_chat, new_bind_code
from .telegram.client import BotApi

PAPER_NOTE = (
    "The snapshot signing key of this installation. Write the lines below on paper and keep the paper safe:\n"
    "the key proves that snapshots are yours. Never type it into a chat or a website.\n"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="klepa-core")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "run"):
        commands.add_parser(name).add_argument("--config", required=True, type=Path)
    service_bot = commands.add_parser("service-bot").add_subparsers(dest="action", required=True)
    service_bot.add_parser("bind").add_argument("--config", required=True, type=Path)
    service = commands.add_parser("service").add_subparsers(dest="action", required=True)
    install = service.add_parser("install")
    install.add_argument("--config", required=True, type=Path)
    install.add_argument("--python", type=Path, default=Path(sys.executable))
    for name in ("uninstall", "restart", "status"):
        service.add_parser(name)
    keys = commands.add_parser("keys").add_subparsers(dest="action", required=True)
    for name in ("paper-backup", "restore"):
        keys.add_parser(name).add_argument("--config", required=True, type=Path)
    return parser


async def _run_with_signals(cfg: Config) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run_service(cfg, stop)
    return 0


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


def _exclude_keys_from_backups(cfg: Config) -> None:
    problem = macos.exclude_from_time_machine(cfg.keys_dir)
    if problem is not None:
        print(f"klepa-core: warning: keys/ is not excluded from Time Machine: {problem}", file=sys.stderr)


def _service(action: str) -> int:
    """The launchd commands that need no config."""
    if action == "uninstall":
        macos.uninstall()
        print("klepa-core: the launchd agent is removed")
    elif action == "restart":
        macos.restart()
        print("klepa-core: Core is restarting")
    else:
        print(f"klepa-core: {macos.status()}")
    return 0


def _keys(cfg: Config, action: str) -> int:
    if action == "paper-backup":
        print(PAPER_NOTE)
        print("\n".join(paper_copy(load_or_create_key(cfg.signing_key_path))))
        return 0
    print("Type the lines of the paper copy, then press Ctrl-D:", file=sys.stderr)
    restore_key(cfg.signing_key_path, key_from_paper(sys.stdin.read()))
    print("klepa-core: the signing key is restored")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "service" and args.action != "install":
            return _service(args.action)
        cfg = load_config(args.config)
        if args.command == "init":
            init_layout(cfg)
            cfg.documents_dir.mkdir(parents=True, exist_ok=True)
            _exclude_keys_from_backups(cfg)
            print("klepa-core: initialized")
            return 0
        if args.command == "service-bot":
            return _bind_service_bot(cfg)
        if args.command == "keys":
            return _keys(cfg, args.action)
        if args.command == "service":
            path = macos.install(cfg, args.config, args.python)
            _exclude_keys_from_backups(cfg)
            print(f"klepa-core: installed {path}; Core now runs under launchd")
            print("If macOS asks whether Python may open the documents folder, allow it, then run: service restart")
            return 0
        configure_logging()  # launchd keeps stderr in a file: a token must never reach it
        return asyncio.run(_run_with_signals(cfg))
    except (ConfigError, KeyFileError) as exc:
        print(f"klepa-core: {exc}", file=sys.stderr)
        return 2
    except AlreadyRunning:
        print("klepa-core: another instance is running", file=sys.stderr)
        return 3
    except macos.ServiceError as exc:
        print(f"klepa-core: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
