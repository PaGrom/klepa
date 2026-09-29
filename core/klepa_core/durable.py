"""Durable file primitives for macOS (docs/architecture.md: Storage).

Plain fsync on macOS does not flush the disk cache, so every durable write uses F_FULLFSYNC.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
from pathlib import Path

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def full_fsync(fd: int) -> None:
    """Flush a file descriptor to stable storage (F_FULLFSYNC, falling back to fsync)."""
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    except (AttributeError, OSError):
        os.fsync(fd)


def fsync_dir(directory: Path) -> None:
    """Make a new directory entry durable."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        full_fsync(fd)
    finally:
        os.close(fd)


def write_exclusive(directory: Path, name: str, data: bytes, mode: int = 0o600) -> Path:
    """Create `directory/name` with O_EXCL, write `data`, flush the file and the directory.

    Raises FileExistsError if the name exists (a symlink included) and ValueError if
    `name` is not a single path component inside `directory`.
    """
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise ValueError(f"not a single file name: {name!r}")
    directory = Path(directory)
    target = directory / name
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        full_fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(directory)
    return target


def partial_name(name: str) -> str:
    """Temporary name used while writing `name`; short and fixed per final name."""
    return ".partial-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def write_new_atomically(directory: Path, name: str, data: bytes, mode: int = 0o600) -> Path:
    """Write through a temporary file and rename, so `name` only ever appears complete.

    Used where a retry after a crash writes the same name again (the documents folder).
    Raises FileExistsError if `name` already exists; never replaces it.
    """
    directory = Path(directory)
    temp = directory / partial_name(name)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(temp)
    write_exclusive(directory, temp.name, data, mode)
    target = directory / name
    if target.exists() or target.is_symlink():
        os.unlink(temp)
        raise FileExistsError(str(target))
    os.rename(temp, target)
    fsync_dir(directory)
    return target
