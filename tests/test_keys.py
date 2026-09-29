import io
import os
import stat
import sys

import pytest

from klepa_core.__main__ import main
from klepa_core.keys import (
    KeyFileError,
    ensure_private_dir,
    key_from_paper,
    load_or_create_key,
    paper_copy,
    restore_key,
)


def test_creates_private_key_once(tmp_path):
    path = tmp_path / "keys" / "k.key"
    first = load_or_create_key(path)
    assert len(first) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_or_create_key(path) == first


def test_refuses_readable_key(tmp_path):
    path = tmp_path / "keys" / "k.key"
    load_or_create_key(path)
    path.chmod(0o644)
    with pytest.raises(KeyFileError):
        load_or_create_key(path)


def test_refuses_open_directory(tmp_path):
    directory = tmp_path / "open"
    directory.mkdir()
    directory.chmod(0o755)
    with pytest.raises(KeyFileError):
        ensure_private_dir(directory)


def test_the_paper_copy_types_back_in_and_catches_a_typo(tmp_path):
    key = bytes(range(32))
    lines = paper_copy(key)
    assert len(lines) == 3
    assert lines[0].startswith("0001 0203 ")
    assert lines[-1].startswith("check ")
    assert key_from_paper("\n".join(lines)) == key
    with pytest.raises(KeyFileError, match="check value"):
        key_from_paper("\n".join(lines).replace("0001", "0010", 1))
    path = tmp_path / "keys" / "snapshot-signing.key"
    restore_key(path, key)
    assert path.read_bytes() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(KeyFileError, match="already exists"):
        restore_key(path, key)


def test_paper_backup_and_restore_through_the_cli(make_config, install, capsys, monkeypatch):
    cfg = make_config()
    config = str(install["tmp"] / "config.toml")
    key = load_or_create_key(cfg.signing_key_path)  # init made it
    previous = os.umask(0o022)
    try:
        assert main(["keys", "paper-backup", "--config", config]) == 0
        paper = capsys.readouterr().out.split("\n\n", 1)[1]  # what the owner writes down: the lines after the note
        cfg.signing_key_path.rename(install["tmp"] / "lost.key")  # the disk is gone
        monkeypatch.setattr(sys, "stdin", io.StringIO(paper))
        assert main(["keys", "restore", "--config", config]) == 0
    finally:
        os.umask(previous)
    assert cfg.signing_key_path.read_bytes() == key


def test_paper_backup_never_makes_a_key(make_config, install, capsys):
    cfg = make_config()
    previous = os.umask(0o022)
    try:
        # No key yet, or a wrong --config: printing a fresh key would give the owner a worthless paper copy.
        assert main(["keys", "paper-backup", "--config", str(install["tmp"] / "config.toml")]) == 2
    finally:
        os.umask(previous)
    assert not cfg.signing_key_path.exists()
    assert "check" not in capsys.readouterr().out
