import stat

import pytest

from klepa_core.keys import KeyFileError, ensure_private_dir, load_or_create_key


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
