"""Key files: at least 32 random bytes, mode 0600, in a 0700 directory, never in snapshots."""
from __future__ import annotations

import secrets
import stat
from pathlib import Path

from .durable import write_exclusive


class KeyFileError(Exception):
    """A key file or its directory has unsafe permissions or content."""


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise KeyFileError(f"{path} must be mode 0700 (is {mode:o})")


def load_or_create_key(path: Path, nbytes: int = 32) -> bytes:
    ensure_private_dir(path.parent)
    if not path.exists():
        try:
            write_exclusive(path.parent, path.name, secrets.token_bytes(nbytes), mode=0o600)
        except FileExistsError:
            pass
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise KeyFileError(f"{path.name} must be mode 0600 (is {mode:o})")
    data = path.read_bytes()
    if len(data) < 32:
        raise KeyFileError(f"{path.name} is shorter than 32 bytes")
    return data
