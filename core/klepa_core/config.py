"""Installation config (TOML). Installation data never goes into the repository."""

from __future__ import annotations

import re
import stat
import tomllib
from dataclasses import dataclass
from datetime import time as time_of_day
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .locale import Locale, LocaleError, available_locales, load_locale

_PERSON_ID = re.compile(r"^[a-z0-9_-]{1,32}$")
_FORBIDDEN_DATA_ROOTS = ("Library/CloudStorage", "Library/Mobile Documents")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# iCloud's "Desktop & Documents Folders" option syncs these; Core cannot tell whether it is on.
_SYNCABLE_HOME_FOLDERS = ("Desktop", "Documents")


class ConfigError(Exception):
    """The installation config is invalid. Messages never include secrets."""


@dataclass(frozen=True)
class Member:
    person_id: str
    telegram_id: int
    name: str
    role: str


@dataclass(frozen=True)
class Config:
    data_dir: Path
    documents_dir: Path
    token_file: Path
    api_root: str
    service_token_file: Path | None
    service_api_root: str
    timezone: str
    locale: Locale
    default_space: str
    shared_folder: str
    members: tuple[Member, ...]
    max_file_bytes: int
    batch_window_seconds: float
    album_quiet_seconds: float
    snapshot_at: time_of_day | None
    daily_line_at: time_of_day | None
    poll_timeout_seconds: int
    private_keywords: tuple[str, ...]

    @property
    def keys_dir(self) -> Path:
        return self.data_dir / "keys"

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "incoming"

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "inbound-journal" / "inbound.db"

    @property
    def core_db_path(self) -> Path:
        return self.data_dir / "core.db"

    @property
    def signing_key_path(self) -> Path:
        return self.keys_dir / "snapshot-signing.key"

    def members_by_telegram_id(self) -> dict[int, Member]:
        return {m.telegram_id: m for m in self.members}


def _abs_path(value: object, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{key} must be an absolute path")
    return path


def _inside(path: Path, folder: Path) -> bool:
    """Whether `path` is `folder` or lies below it, ignoring case like the default macOS volume format."""
    parts = [part.casefold() for part in path.parts]
    prefix = [part.casefold() for part in folder.parts]
    return parts[: len(prefix)] == prefix


def _check_data_dir(data_dir: Path) -> None:
    """Keys and databases must never leave the machine through iCloud or a sync client (#9)."""
    resolved = data_dir.resolve()  # follows symlinks in the part of the path that exists
    if any(root.casefold() in str(resolved).casefold() for root in _FORBIDDEN_DATA_ROOTS):
        raise ConfigError("paths.data_dir must not be inside iCloud or CloudStorage")
    home = Path.home().resolve()
    for folder in _SYNCABLE_HOME_FOLDERS:
        if _inside(resolved, home / folder):
            raise ConfigError(f"paths.data_dir must not be inside ~/{folder}: iCloud may sync it")


def _members(items: object) -> tuple[Member, ...]:
    members: list[Member] = []
    for i, item in enumerate(items if isinstance(items, list) else []):
        try:
            member = Member(
                str(item["person_id"]), int(item["telegram_id"]), str(item["name"]), str(item.get("role", "member"))
            )
        except (KeyError, TypeError, ValueError):
            raise ConfigError(f"members[{i}] needs person_id, telegram_id and name") from None
        if not _PERSON_ID.match(member.person_id):
            raise ConfigError(f"members[{i}].person_id must match {_PERSON_ID.pattern}")
        if member.role not in ("owner", "member"):
            raise ConfigError(f"members[{i}].role must be 'owner' or 'member'")
        if not member.name.strip():
            raise ConfigError(f"members[{i}].name must not be empty")
        members.append(member)
    if not members:
        raise ConfigError("at least one member is required")
    if len({m.telegram_id for m in members}) != len(members) or len({m.person_id for m in members}) != len(members):
        raise ConfigError("members must have unique telegram_id and person_id")
    if sum(m.role == "owner" for m in members) != 1:
        raise ConfigError("exactly one member must have role 'owner'")
    return tuple(members)


def _safe_api_root(url: str) -> bool:
    """https anywhere; plain http only to a loopback host, because the URL carries the bot token."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


def parse_time_of_day(text: str) -> time_of_day | None:
    """'HH:MM' in local time, or 'off' to disable a daily job."""
    if text == "off":
        return None
    try:
        hours, minutes = text.split(":")
        return time_of_day(int(hours), int(minutes))
    except ValueError:
        raise ValueError(f"expected HH:MM or 'off', got {text!r}") from None


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config: {type(exc).__name__}") from None
    paths = raw.get("paths", {})
    telegram = raw.get("telegram", {})
    spaces = raw.get("spaces", {})
    intake = raw.get("intake", {})

    data_dir = _abs_path(paths.get("data_dir"), "paths.data_dir")
    _check_data_dir(data_dir)
    api_root = str(telegram.get("api_root", "https://api.telegram.org")).rstrip("/")
    if not _safe_api_root(api_root):
        raise ConfigError("telegram.api_root must be https (or plain http to a loopback address, for tests)")
    token_file = _abs_path(telegram.get("token_file"), "telegram.token_file")
    service = raw.get("service_bot")
    service_token_file: Path | None = None
    service_api_root = api_root
    if service is not None:
        service_token_file = _abs_path(service.get("token_file"), "service_bot.token_file")
        if service_token_file == token_file:
            raise ConfigError("service_bot.token_file must differ from telegram.token_file")
        service_api_root = str(service.get("api_root", api_root)).rstrip("/")
        if not _safe_api_root(service_api_root):
            raise ConfigError("service_bot.api_root must be https (or plain http to a loopback address, for tests)")
    timezone = raw.get("timezone")
    if not isinstance(timezone, str):
        raise ConfigError('timezone is required, for example "Europe/Berlin"')
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"unknown timezone: {timezone}") from None
    code = raw.get("locale")
    if not isinstance(code, str):
        raise ConfigError(f"locale is required, one of: {', '.join(available_locales())}")
    try:
        locale = load_locale(code)
    except LocaleError as exc:
        raise ConfigError(f"locale: {exc}") from None
    default_space = str(spaces.get("default", "shared"))
    if default_space not in ("shared", "personal"):
        raise ConfigError("spaces.default must be 'shared' or 'personal'")

    album_quiet_seconds = float(intake.get("album_quiet_seconds", 60.0))
    if album_quiet_seconds < 0:
        raise ConfigError("intake.album_quiet_seconds must not be negative")

    schedule = raw.get("schedule", {})
    try:
        snapshot_at = parse_time_of_day(str(schedule.get("snapshot_at", "03:30")))
    except ValueError as exc:
        raise ConfigError(f"schedule.snapshot_at: {exc}") from None
    try:
        daily_line_at = parse_time_of_day(str(schedule.get("daily_line_at", "09:00")))
    except ValueError as exc:
        raise ConfigError(f"schedule.daily_line_at: {exc}") from None

    return Config(
        data_dir=data_dir,
        documents_dir=_abs_path(paths.get("documents_dir"), "paths.documents_dir"),
        token_file=token_file,
        api_root=api_root,
        service_token_file=service_token_file,
        service_api_root=service_api_root,
        timezone=timezone,
        locale=locale,
        default_space=default_space,
        shared_folder=str(spaces.get("shared_folder", locale.shared_folder)),
        members=_members(raw.get("members")),
        max_file_bytes=int(intake.get("max_file_bytes", 20 * 1024 * 1024)),
        batch_window_seconds=float(intake.get("batch_window_seconds", 2.0)),
        album_quiet_seconds=album_quiet_seconds,
        snapshot_at=snapshot_at,
        daily_line_at=daily_line_at,
        poll_timeout_seconds=int(intake.get("poll_timeout_seconds", 30)),
        private_keywords=tuple(str(k) for k in intake.get("private_keywords", locale.private_keywords)),
    )


def _read_secret(path: Path, what: str) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        raise ConfigError(f"{what} is missing") from None
    if mode & 0o077:
        raise ConfigError(f"{what} must be mode 0600")
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        raise ConfigError(f"{what} is empty")
    return secret


def read_token(cfg: Config) -> str:
    """Read the family bot token from its 0600 file. Errors never include the token."""
    return _read_secret(cfg.token_file, "token file")


def read_service_token(cfg: Config) -> str:
    """Read the service bot token from its 0600 file. Errors never include the token."""
    if cfg.service_token_file is None:
        raise ConfigError("service_bot is not configured")
    return _read_secret(cfg.service_token_file, "service bot token file")
