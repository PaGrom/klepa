import errno
import fcntl
import os
import stat

import pytest

from klepa_core import durable
from klepa_core.durable import full_fsync, make_dirs_durably, partial_name, write_exclusive, write_new_atomically


def test_write_exclusive_creates_private_file(tmp_path):
    path = write_exclusive(tmp_path, "a.bin", b"hello")
    assert path == tmp_path / "a.bin"
    assert path.read_bytes() == b"hello"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_exclusive_refuses_existing_name(tmp_path):
    write_exclusive(tmp_path, "a.bin", b"one")
    with pytest.raises(FileExistsError):
        write_exclusive(tmp_path, "a.bin", b"two")
    assert (tmp_path / "a.bin").read_bytes() == b"one"


@pytest.mark.parametrize("bad", ["../x", "sub/x", "/etc/x", "", ".", "..", "a\x00b"])
def test_write_exclusive_rejects_names_leaving_directory(tmp_path, bad):
    with pytest.raises(ValueError, match="not a single file name"):
        write_exclusive(tmp_path, bad, b"x")


def test_write_exclusive_does_not_follow_symlink(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_bytes(b"keep")
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(FileExistsError):
        write_exclusive(tmp_path, "link", b"overwrite")
    assert target.read_bytes() == b"keep"


def test_full_fsync_accepts_regular_file(tmp_path):
    fd = os.open(tmp_path / "f", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"x")
        full_fsync(fd)
    finally:
        os.close(fd)


def test_write_new_atomically_replaces_stale_partial_and_refuses_existing(tmp_path):
    (tmp_path / partial_name("doc.pdf")).write_bytes(b"half from a crash")
    path = write_new_atomically(tmp_path, "doc.pdf", b"complete")
    assert path.read_bytes() == b"complete"
    assert not (tmp_path / partial_name("doc.pdf")).exists()
    with pytest.raises(FileExistsError):
        write_new_atomically(tmp_path, "doc.pdf", b"other")
    assert path.read_bytes() == b"complete"


def _failing_fcntl(code):
    def fail(fd, command):
        raise OSError(code, os.strerror(code))

    return fail


def test_full_fsync_raises_a_real_io_error(tmp_path, monkeypatch):
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)  # present on Linux runners too
    monkeypatch.setattr(fcntl, "fcntl", _failing_fcntl(errno.EIO))
    fd = os.open(tmp_path / "f", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        with pytest.raises(OSError, match=os.strerror(errno.EIO)) as info:
            full_fsync(fd)
        assert info.value.errno == errno.EIO
    finally:
        os.close(fd)


@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.EINVAL])
def test_full_fsync_falls_back_to_fsync_where_unsupported(tmp_path, monkeypatch, code):
    flushed = []
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(fcntl, "fcntl", _failing_fcntl(code))
    monkeypatch.setattr(os, "fsync", flushed.append)
    fd = os.open(tmp_path / "f", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        full_fsync(fd)
    finally:
        os.close(fd)
    assert flushed == [fd]


def test_make_dirs_durably_flushes_each_parent_that_got_a_new_entry(tmp_path, monkeypatch):
    flushed = []
    monkeypatch.setattr(durable, "fsync_dir", flushed.append)
    path = make_dirs_durably(tmp_path, "2026/10")
    assert path == tmp_path / "2026" / "10"
    assert path.is_dir()
    assert flushed == [tmp_path, tmp_path / "2026"]
    flushed.clear()
    make_dirs_durably(tmp_path, "2026/11")
    assert flushed == [tmp_path / "2026"]
