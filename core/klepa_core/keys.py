"""Key files: at least 32 random bytes, mode 0600, in a 0700 directory, never in snapshots."""

from __future__ import annotations

import contextlib
import hashlib
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
        with contextlib.suppress(FileExistsError):
            write_exclusive(path.parent, path.name, secrets.token_bytes(nbytes), mode=0o600)
    return _checked_key(path)


def load_key(path: Path) -> bytes:
    """An existing key; never makes one (a paper copy of a fresh key would be worthless)."""
    if not path.exists():
        raise KeyFileError(f"{path.name} is missing: run init first, or check --config")
    return _checked_key(path)


def _checked_key(path: Path) -> bytes:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise KeyFileError(f"{path.name} must be mode 0600 (is {mode:o})")
    data = path.read_bytes()
    if len(data) < 32:
        raise KeyFileError(f"{path.name} is shorter than 32 bytes")
    return data


def _check(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def paper_copy(key: bytes) -> list[str]:
    """The key as hex in groups of four for a paper copy (spec 5.3), then a check value that catches a mistyped
    group when the key is typed back in."""
    text = key.hex()
    groups = [text[i : i + 4] for i in range(0, len(text), 4)]
    lines = [" ".join(groups[i : i + 8]) for i in range(0, len(groups), 8)]
    return [*lines, f"check {_check(key)}"]


def key_from_paper(text: str) -> bytes:
    """The key back from its paper copy. Raises KeyFileError when it is malformed or does not match its check."""
    words = text.split()
    if len(words) < 3 or words[-2] != "check":
        raise KeyFileError("the paper copy ends with 'check' and its value")
    try:
        key = bytes.fromhex("".join(words[:-2]))
    except ValueError:
        raise KeyFileError("the paper copy holds hex digits only") from None
    if len(key) < 32 or _check(key) != words[-1].lower():
        raise KeyFileError("the paper copy does not match its check value; look for a mistyped group")
    return key


def restore_key(path: Path, key: bytes) -> None:
    """Write a key typed in from its paper copy. Never replaces an existing key."""
    ensure_private_dir(path.parent)
    try:
        write_exclusive(path.parent, path.name, key, mode=0o600)
    except FileExistsError:
        raise KeyFileError(f"{path.name} already exists; move it away first") from None
