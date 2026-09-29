import pytest

from helpers import BASE_CONFIG
from klepa_core.config import ConfigError, read_token


def test_loads_members_and_defaults(make_config):
    cfg = make_config()
    assert [m.person_id for m in cfg.members] == ["owner", "member"]
    assert cfg.default_space == "shared"
    assert cfg.shared_folder == "Shared"
    assert cfg.locale.code == "en"
    assert cfg.private_keywords == cfg.locale.private_keywords
    assert cfg.max_file_bytes == 20 * 1024 * 1024
    assert cfg.members_by_telegram_id()[111111].role == "owner"
    assert cfg.journal_path == cfg.data_dir / "inbound-journal" / "inbound.db"
    assert cfg.signing_key_path == cfg.data_dir / "keys" / "snapshot-signing.key"


def test_rejects_data_dir_in_cloud_storage(make_config, install):
    bad = BASE_CONFIG.replace("{data_dir}", str(install["tmp"] / "Library" / "CloudStorage" / "x"))
    with pytest.raises(ConfigError, match="iCloud or CloudStorage"):
        make_config(text=bad)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda t: t.replace("telegram_id = 222222", "telegram_id = 111111"), "unique"),
        (lambda t: t.replace('role = "member"', 'role = "owner"'), "exactly one"),
        (lambda t: t.replace('role = "member"', 'role = "admin"'), "role"),
        (lambda t: t.replace('person_id = "member"', 'person_id = "Bad Id"'), "person_id"),
    ],
)
def test_rejects_bad_members(make_config, mutation, message):
    with pytest.raises(ConfigError, match=message):
        make_config(text=mutation(BASE_CONFIG))


@pytest.mark.parametrize("line", ['locale = "xx"', 'locale = "../en"', ""])
def test_locale_is_required_and_known(make_config, line):
    with pytest.raises(ConfigError, match="locale"):
        make_config(text=BASE_CONFIG.replace('locale = "en"', line))


@pytest.mark.parametrize("line", ['timezone = "Mars/Olympus_Mons"', ""])
def test_timezone_is_required_and_known(make_config, line):
    with pytest.raises(ConfigError, match="timezone"):
        make_config(text=BASE_CONFIG.replace('timezone = "Europe/Berlin"', line))


@pytest.mark.parametrize(
    "api_root",
    ["http://example.com", "http://localhost.evil.example", "http://127.0.0.1.evil.example", "ftp://127.0.0.1"],
)
def test_rejects_plain_http_api_root(make_config, api_root):
    with pytest.raises(ConfigError, match="https"):
        make_config(api_root=api_root)


@pytest.mark.parametrize(
    "api_root", ["https://api.telegram.org", "http://127.0.0.1:8081", "http://localhost:8081", "http://[::1]:8081"]
)
def test_accepts_https_and_loopback_http(make_config, api_root):
    assert make_config(api_root=api_root).api_root == api_root


def test_read_token_requires_0600_and_never_echoes_it(make_config, install):
    cfg = make_config()
    assert read_token(cfg) == "123:TEST-TOKEN"
    install["token_file"].chmod(0o644)
    with pytest.raises(ConfigError) as info:
        read_token(cfg)
    assert "TEST-TOKEN" not in str(info.value)
