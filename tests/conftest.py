import pytest

from helpers import BASE_CONFIG, TEST_TOKEN
from klepa_core.config import load_config


@pytest.fixture
def install(tmp_path):
    """Directories and a 0600 token file for a test installation."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    keys = data_dir / "keys"
    keys.mkdir(mode=0o700)
    token_file = keys / "family-bot.token"
    token_file.write_text(TEST_TOKEN)
    token_file.chmod(0o600)
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    return {"data_dir": data_dir, "documents_dir": documents_dir, "token_file": token_file, "tmp": tmp_path}


@pytest.fixture
def make_config(install):
    def make(api_root="https://api.telegram.org", text=None):
        body = (text or BASE_CONFIG).format(
            api_root=api_root,
            data_dir=install["data_dir"],
            documents_dir=install["documents_dir"],
            token_file=install["token_file"],
        )
        path = install["tmp"] / "config.toml"
        path.write_text(body, encoding="utf-8")
        return load_config(path)

    return make
