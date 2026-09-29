import os
import stat

import pytest

from klepa_core.durable import full_fsync, partial_name, write_exclusive, write_new_atomically


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
