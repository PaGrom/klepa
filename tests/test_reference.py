import dataclasses
from pathlib import Path

import pytest

from helpers import BASE_CONFIG
from klepa_core.host import reference as ref
from klepa_core.host.peers import probe_peer, stand_in
from klepa_core.host.supervisor import REQUIRED_HOOKS, check_heartbeat

LAYOUT = ref.HostLayout(runtime_dir=Path("/runtime"), host_dir=Path("/data/host"))
SETTINGS = ref.HostSettings(
    gateway_port=19300,
    api_port=19201,
    proxy_port=19202,
    socket_path=Path("/data/run/adapter.sock"),
    adapter_key_path=Path("/data/keys/adapter.key"),
    host_token_path=Path("/data/keys/host-bot.token"),
    allowed=(111111, 222222, 2**51 + 5),
    model="anthropic/claude-sonnet-5",
    time_zone="UTC",
)


def test_the_table_carries_the_installation_values():
    table = ref.table(SETTINGS, LAYOUT)
    assert table["gateway.port"] == 19300
    assert table["channels.telegram.apiRoot"] == "http://127.0.0.1:19201"  # exactly: the gatekeeper checks Host
    assert table["proxy.proxyUrl"] == "http://127.0.0.1:19202"
    assert table["channels.telegram.tokenFile"] == "/data/keys/host-bot.token"
    assert table["channels.telegram.allowFrom"] == ["111111", "222222", str(2**51 + 5)]
    assert table["agents.defaults.model.primary"] == "anthropic/claude-sonnet-5"
    assert table["agents.defaults.models.anthropic/claude-sonnet-5.agentRuntime.id"] == "openclaw"
    assert table["agents.defaults.modelPolicy.allow"] == ["anthropic/claude-sonnet-5"]
    assert table["agents.defaults.workspace"] == "/data/host/workspace"
    assert table["plugins.load.paths"] == ["/runtime/adapter"]
    assert table["logging.file"] == "/data/host/logs/gateway.log"
    assert table["gateway.auth.token"] == {"source": "file", "provider": "klepa-gateway-token", "id": "value"}
    assert table["secrets.providers.klepa-gateway-token"]["path"] == "/data/host/gateway.token"


def test_the_adapter_reports_every_value_but_the_unreported():
    table = ref.table(SETTINGS, LAYOUT)
    config = table["plugins.entries.klepa-adapter.config"]
    assert config["socket"] == "/data/run/adapter.sock"
    assert config["keyFile"] == "/data/keys/adapter.key"
    assert config["policy"] == sorted(ref.policy(table))
    assert not ref.UNREPORTED & set(config["policy"])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("gateway.bind", "loopback"),
        ("gateway.reload.mode", "off"),
        ("gateway.terminal.enabled", False),
        ("gateway.nodes.pairing.autoApproveLocal", False),
        ("discovery.mdns.mode", "off"),
        ("plugins.entries.klepa-adapter.hooks.allowConversationAccess", True),
        ("plugins.slots.memory", "none"),
        ("tools.profile", "minimal"),
        ("tools.toolSearch", False),  # OpenClaw 2026.10 hides tools behind search/describe/dispatch unless told not to
        ("tools.codeMode", False),  # and turns a JavaScript surface on by itself for some models
        ("channels.telegram.dmHistoryLimit", 3),
        ("agents.defaults.imageMaxDimensionPx", 1568),
        (
            "tools.alsoAllow",
            [
                "klepa__get",
                "klepa__make_pdf",
                "klepa__save_text",
                "klepa__search",
                "klepa__send_original",
                "klepa__view_draft",
                "klepa__view_page",
            ],
        ),
        (
            "gateway.tools.deny",
            [
                "klepa__get",
                "klepa__make_pdf",
                "klepa__save_text",
                "klepa__search",
                "klepa__send_original",
                "klepa__view_draft",
                "klepa__view_page",
            ],
        ),
        ("tools.sessions.visibility", "self"),
        ("commands.allowFrom", {"*": []}),
        ("commands.restart", False),
        ("channels.telegram.dmPolicy", "allowlist"),
        ("channels.telegram.groupPolicy", "disabled"),
        ("channels.telegram.linkPreview", False),
        ("channels.telegram.configWrites", False),
        ("proxy.loopbackMode", "gateway-only"),
        ("messages.queue.mode", "followup"),
        ("session.dmScope", "per-channel-peer"),
        ("agents.defaults.sandbox.mode", "off"),
        ("update.checkOnStart", False),
        ("update.auto.enabled", False),
        ("cron.enabled", False),
        # OpenClaw's own persona and first-run ritual stay out of the prompt (review of plan 2a), and so do the
        # features plan 1c-2 left to stage 2: internal hooks, among them session-memory, and ACP.
        ("agents.defaults.skipBootstrap", True),
        ("agents.defaults.contextInjection", "never"),
        ("hooks.internal.enabled", False),
        ("acp.enabled", False),
    ],
)
def test_the_spec_values_are_in_the_table(key, value):
    """Spec 7.1, item 2: the values the engine's safety rests on."""
    assert ref.table(SETTINGS, LAYOUT)[key] == value


def test_render_nests_the_dotted_keys():
    config = ref.render(ref.table(SETTINGS, LAYOUT))
    assert config["gateway"]["reload"]["mode"] == "off"
    assert config["agents"]["defaults"]["models"]["anthropic/claude-sonnet-5"] == {"agentRuntime": {"id": "openclaw"}}
    assert config["plugins"]["entries"]["klepa-adapter"]["hooks"] == {"allowConversationAccess": True}
    assert config["commands"]["allowFrom"] == {"*": []}


def test_a_table_where_one_key_hides_another_is_refused():
    with pytest.raises(ValueError, match="hides"):
        ref.check_table({"tools": {}, "tools.profile": "minimal"})
    with pytest.raises(ValueError, match="empty part"):
        ref.check_table({"tools..profile": "minimal"})


def test_expectations_come_from_the_table():
    table = ref.table(SETTINGS, LAYOUT)
    expect = ref.expectations(table, "a" * 64, "2026.10.33")
    assert expect.hooks == REQUIRED_HOOKS
    assert expect.plugin_sha256 == "a" * 64
    assert (expect.model, expect.runtime, expect.version) == ("anthropic/claude-sonnet-5", "openclaw", "2026.10.33")
    beat = {
        "registrations": sorted(REQUIRED_HOOKS),
        "plugin_sha256": "a" * 64,
        "policy": ref.policy(table),
        "model": "anthropic/claude-sonnet-5",
        "runtime": "openclaw",
        "version": "2026.10.33",
    }
    assert check_heartbeat(beat, expect) == []
    assert check_heartbeat(beat | {"policy": ref.policy(table) | {"tools.profile": "full"}}, expect) == [
        "policy:tools.profile"
    ]
    assert check_heartbeat(beat | {"runtime": "claude-cli"}, expect) == ["runtime"]
    assert check_heartbeat(beat | {"version": "2026.10.1"}, expect) == ["version"]  # an OpenClaw upgraded by hand


def test_settings_follow_the_config(make_config, short_dir):
    text = (
        "\n[host]\n"
        f'socket = "{short_dir / "run" / "adapter.sock"}"\ngateway_port = 19400\nmodel = "anthropic/claude-opus-5-5"\n'
    )
    cfg = make_config(text=BASE_CONFIG + text)
    key = b"k" * 32
    settings = ref.settings_of(cfg, key)
    # The gateway admits the members by their stand-ins, never by their Telegram ids, and the probe peer.
    assert settings.allowed == (stand_in(key, "owner"), stand_in(key, "member"), probe_peer(key))
    assert (settings.gateway_port, settings.model) == (19400, "anthropic/claude-opus-5-5")
    assert ref.layout_of(cfg).host_dir == cfg.data_dir / "host"
    assert ref.layout_of(cfg).home == cfg.data_dir / "host" / "home"


def test_cores_tools_come_through_the_adapters_mcp_server():
    from klepa_core.host.instruction import TOOLS

    settings = ref.HostSettings(
        19300, 19201, 19202, Path("/run/adapter.sock"), Path("/k"), Path("/t"), (1,), "anthropic/claude-sonnet-5", "UTC"
    )
    layout = ref.HostLayout(runtime_dir=Path("/rt"), host_dir=Path("/h"))
    table = ref.table(settings, layout)
    assert table["mcp.servers.klepa"] == {
        "command": "/rt/node-v24.22.3/bin/node",
        "args": ["/rt/adapter/mcp.ts"],
        "env": {"KLEPA_SOCKET": "/run/adapter.sock"},
    }
    from klepa_core.host.instruction import READER_TOOLS

    assert sorted(table["tools.alsoAllow"]) == sorted({*TOOLS, *READER_TOOLS})  # exact names only (spec 4.3)
    assert table["gateway.tools.deny"] == table["tools.alsoAllow"]  # never through /tools/invoke
    assert "mcp.servers.klepa" in ref.policy(table)  # every heartbeat proves the gateway runs this server


@pytest.mark.parametrize(
    ("zone", "when", "expected"),
    [
        ("Europe/Berlin", "2026-01-15T12:00:00+00:00", "+01:00"),  # winter
        ("Europe/Berlin", "2026-07-15T12:00:00+00:00", "+02:00"),  # summer
        ("America/New_York", "2026-01-15T12:00:00+00:00", "-05:00"),
        ("UTC", "2026-01-15T12:00:00+00:00", "UTC"),
        ("Asia/Kolkata", "2026-01-15T12:00:00+00:00", "+05:30"),  # Node shows it as GMT+5:30
        ("America/St_Johns", "2026-01-15T12:00:00+00:00", "-03:30"),
    ],
)
def test_the_gateway_gets_the_familys_offset_never_its_place(zone, when, expected):
    """OpenClaw 2026.10 puts the time zone's name into every turn (its Temporal Context): an Area/City name says where
    the family lives (the canary test). The gateway gets a zone of the same offset without a place."""
    from datetime import datetime

    assert ref.placeless_zone(zone, datetime.fromisoformat(when)) == expected


def test_the_table_carries_the_placeless_zone():
    table = ref.table(dataclasses.replace(SETTINGS, time_zone="+01:00"), LAYOUT)
    assert table["agents.defaults.userTimezone"] == "+01:00"
    assert "agents.defaults.userTimezone" in ref.policy(table)


def test_each_spaces_reader_peer_is_admitted_and_keeps_one_turn_of_history(make_config, short_dir):
    """Stage 2c-2: the gateway admits the reader peers beside the members and the probe, and a reader's session keeps
    no more than one turn (a second wall beside Core's reset after every reading)."""
    from klepa_core.host.peers import reader_peer

    cfg = make_config(text=BASE_CONFIG + f'\n[host]\nsocket = "{short_dir / "run" / "adapter.sock"}"\n')
    key = b"k" * 32
    settings = ref.settings_of(cfg, key)
    spaces = ("shared", "personal:owner", "personal:member")
    assert settings.readers == tuple(reader_peer(key, space) for space in spaces)
    table = ref.table(settings, ref.layout_of(cfg))
    assert table["channels.telegram.allowFrom"][-3:] == [str(peer) for peer in settings.readers]
    assert table["channels.telegram.dms"] == {str(peer): {"historyLimit": 1} for peer in settings.readers}
