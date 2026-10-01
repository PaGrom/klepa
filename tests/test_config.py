from pathlib import Path

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


def test_rejects_data_dir_behind_a_symlink_into_cloud_storage(make_config, install):
    target = install["tmp"] / "Library" / "CloudStorage" / "SomeDrive"
    target.mkdir(parents=True)
    link = install["tmp"] / "innocent-looking"
    link.symlink_to(target)
    bad = BASE_CONFIG.replace("{data_dir}", str(link / "data"))
    with pytest.raises(ConfigError, match="iCloud or CloudStorage"):
        make_config(text=bad)


@pytest.mark.parametrize(
    ("folder", "named"),
    [("Desktop", "Desktop"), ("Documents", "Documents"), ("desktop", "Desktop")],
)
def test_rejects_data_dir_in_folders_icloud_may_sync(make_config, install, monkeypatch, folder, named):
    home = install["tmp"] / "home"
    (home / folder).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    bad = BASE_CONFIG.replace("{data_dir}", str(home / folder / "KlepaData"))
    with pytest.raises(ConfigError, match=f"~/{named}"):
        make_config(text=bad)


def test_accepts_data_dir_elsewhere_in_home(make_config, install, monkeypatch):
    home = install["tmp"] / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    good = BASE_CONFIG.replace("{data_dir}", str(home / "KlepaData"))
    assert make_config(text=good).data_dir == home / "KlepaData"


def test_album_quiet_period_defaults_to_a_minute(make_config):
    assert make_config().album_quiet_seconds == 0.3
    text = BASE_CONFIG.replace("album_quiet_seconds = 0.3\n", "")
    assert make_config(text=text).album_quiet_seconds == 60.0
    with pytest.raises(ConfigError, match="album_quiet_seconds"):
        make_config(text=BASE_CONFIG.replace("album_quiet_seconds = 0.3", "album_quiet_seconds = -1"))


def test_schedule_defaults_and_validation(make_config):
    from datetime import time

    assert (make_config().snapshot_at, make_config().daily_line_at) == (None, None)  # BASE_CONFIG turns them off
    text = BASE_CONFIG.replace('snapshot_at = "off"\ndaily_line_at = "off"\n', "")
    cfg = make_config(text=text)
    assert (cfg.snapshot_at, cfg.daily_line_at) == (time(3, 30), time(9, 0))
    with pytest.raises(ConfigError, match=r"schedule\.snapshot_at"):
        make_config(text=BASE_CONFIG.replace('snapshot_at = "off"', 'snapshot_at = "3am"'))


def test_service_bot_is_optional_and_checked(make_config, install):
    from helpers import with_service_bot
    from klepa_core.config import read_service_token

    cfg = make_config()
    assert cfg.service_token_file is None
    with pytest.raises(ConfigError, match="not configured"):
        read_service_token(cfg)
    text = with_service_bot(BASE_CONFIG, install["service_token_file"], "https://api.telegram.org")
    cfg = make_config(text=text)
    assert read_service_token(cfg) == "456:SERVICE-TOKEN"
    same = with_service_bot(BASE_CONFIG, install["token_file"], "https://api.telegram.org")
    with pytest.raises(ConfigError, match="must differ"):
        make_config(text=same)
    plain = with_service_bot(BASE_CONFIG, install["service_token_file"], "http://example.com")
    with pytest.raises(ConfigError, match=r"service_bot\.api_root"):
        make_config(text=plain)


def test_the_service_bot_must_be_another_bot(make_config, install):
    from helpers import with_service_bot
    from klepa_core.config import read_service_token

    # A new token of the family bot (same bot id, 123): two pollers on one bot would lose family updates.
    install["service_token_file"].write_text("123:ANOTHER-SECRET-OF-THE-FAMILY-BOT")
    cfg = make_config(text=with_service_bot(BASE_CONFIG, install["service_token_file"], "https://api.telegram.org"))
    with pytest.raises(ConfigError, match="family bot") as info:
        read_service_token(cfg)
    assert "SECRET" not in str(info.value)


def test_the_host_section_is_optional(make_config):
    assert make_config().host is None


def test_host_section_defaults(make_config, short_dir):
    data_dir = short_dir / "data"  # the default socket lives in the data directory; tmp_path is too long for it
    text = BASE_CONFIG.replace("{data_dir}", str(data_dir)) + '\n[host]\negress_allow = ["API.Anthropic.com.:443"]\n'
    cfg = make_config(text=text)
    assert cfg.host is not None
    assert (cfg.host.api_port, cfg.host.proxy_port) == (19201, 19202)
    assert cfg.host.egress_allow == (("api.anthropic.com", 443),)
    assert cfg.host.socket_path == data_dir / "run" / "adapter.sock"
    assert cfg.host_token_path == data_dir / "keys" / "host-bot.token"
    assert cfg.adapter_key_path == data_dir / "keys" / "adapter.key"


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ('egress_allow = ["*.anthropic.com:443"]', "exact"),
        ('egress_allow = ["api.anthropic.com"]', "exact"),
        ('egress_allow = ["localhost:443"]', "exact"),
        ('egress_allow = ["1.2.3.4:443"]', "not an address"),
        ('egress_allow = ["[::1]:443"]', "not an address"),
        ('egress_allow = "api.anthropic.com:443"', "list"),
        ("api_port = 0", "port"),
        ("proxy_port = 19201", "differ"),
        ('socket = "relative.sock"', "absolute"),
        ('socket = "/tmp/' + "x" * 120 + '.sock"', "longer than"),
    ],
)
def test_bad_host_sections_are_refused(make_config, section, message):
    socket = "" if section.startswith("socket") else 'socket = "/tmp/klepa-test.sock"\n'
    with pytest.raises(ConfigError, match=message):
        make_config(text=BASE_CONFIG + f"\n[host]\n{socket}{section}\n")


def test_the_socket_needs_a_private_directory(make_config, short_dir):
    open_dir = short_dir / "open"
    open_dir.mkdir()
    open_dir.chmod(0o777)
    with pytest.raises(ConfigError, match="0700"):
        make_config(text=BASE_CONFIG + f'\n[host]\nsocket = "{open_dir / "adapter.sock"}"\n')


def test_a_port_in_other_digits_is_refused(make_config):
    with pytest.raises(ConfigError, match="exact"):
        make_config(text=BASE_CONFIG + '\n[host]\nsocket = "/tmp/klepa-test.sock"\negress_allow = ["x.example:4²"]\n')
