import aiohttp
import pytest

from faketg import FakeTelegram
from helpers import BASE_CONFIG, SERVICE_TOKEN, TEST_TOKEN
from klepa_core.config import load_config
from klepa_core.telegram.client import BotApi


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
    service_token_file = keys / "service-bot.token"
    service_token_file.write_text(SERVICE_TOKEN)
    service_token_file.chmod(0o600)
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    return {
        "data_dir": data_dir,
        "documents_dir": documents_dir,
        "token_file": token_file,
        "service_token_file": service_token_file,
        "tmp": tmp_path,
    }


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


@pytest.fixture
async def fake_tg():
    telegram = FakeTelegram(token=TEST_TOKEN)
    await telegram.start()
    yield telegram
    await telegram.stop()


@pytest.fixture
async def api(fake_tg):
    async with aiohttp.ClientSession() as session:
        yield BotApi(session, fake_tg.url, TEST_TOKEN)


@pytest.fixture
async def service_tg():
    telegram = FakeTelegram(token=SERVICE_TOKEN, username="klepa_service_test_bot")
    await telegram.start()
    yield telegram
    await telegram.stop()


@pytest.fixture
async def service_api(service_tg):
    async with aiohttp.ClientSession() as session:
        yield BotApi(session, service_tg.url, SERVICE_TOKEN)
