"""Installation config (TOML). Installation data never goes into the repository."""
from __future__ import annotations

import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .locale import Locale, LocaleError, available_locales, load_locale

_PERSON_ID = re.compile(r"^[a-z0-9_-]{1,32}$")
_FORBIDDEN_DATA_ROOTS = ("Library/CloudStorage", "Library/Mobile Documents")


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
    timezone: str
    locale: Locale
    default_space: str
    shared_folder: str
    members: tuple[Member, ...]
    max_file_bytes: int
    batch_window_seconds: float
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


def _members(items: object) -> tuple[Member, ...]:
    members: list[Member] = []
    for i, item in enumerate(items if isinstance(items, list) else []):
        try:
            member = Member(str(item["person_id"]), int(item["telegram_id"]), str(item["name"]),
                            str(item.get("role", "member")))
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
    if any(root in str(data_dir) for root in _FORBIDDEN_DATA_ROOTS):
        raise ConfigError("paths.data_dir must not be inside iCloud or CloudStorage")
    api_root = str(telegram.get("api_root", "https://api.telegram.org")).rstrip("/")
    if not api_root.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise ConfigError("telegram.api_root must be https (or loopback http for tests)")
    timezone = str(raw.get("timezone", "Europe/Berlin"))
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

    return Config(
        data_dir=data_dir,
        documents_dir=_abs_path(paths.get("documents_dir"), "paths.documents_dir"),
        token_file=_abs_path(telegram.get("token_file"), "telegram.token_file"),
        api_root=api_root,
        timezone=timezone,
        locale=locale,
        default_space=default_space,
        shared_folder=str(spaces.get("shared_folder", locale.shared_folder)),
        members=_members(raw.get("members")),
        max_file_bytes=int(intake.get("max_file_bytes", 20 * 1024 * 1024)),
        batch_window_seconds=float(intake.get("batch_window_seconds", 2.0)),
        poll_timeout_seconds=int(intake.get("poll_timeout_seconds", 30)),
        private_keywords=tuple(str(k) for k in intake.get("private_keywords", locale.private_keywords)),
    )


def read_token(cfg: Config) -> str:
    """Read the bot token from its 0600 file. Errors never include the token."""
    try:
        mode = stat.S_IMODE(cfg.token_file.stat().st_mode)
    except OSError:
        raise ConfigError("token file is missing") from None
    if mode & 0o077:
        raise ConfigError("token file must be mode 0600")
    token = cfg.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ConfigError("token file is empty")
    return token
