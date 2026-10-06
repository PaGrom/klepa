from pathlib import Path

import pytest

from helpers import BASE_CONFIG
from klepa_core.host import reference as ref
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
        ("tools.alsoAllow", ["klepa__get", "klepa__search", "klepa__send_original"]),
        ("gateway.tools.deny", ["klepa__get", "klepa__search", "klepa__send_original"]),
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
    expect = ref.expectations(table, "a" * 64, "2026.9.4")
    assert expect.hooks == REQUIRED_HOOKS
    assert expect.plugin_sha256 == "a" * 64
    assert (expect.model, expect.runtime, expect.version) == ("anthropic/claude-sonnet-5", "openclaw", "2026.9.4")
    beat = {
        "registrations": sorted(REQUIRED_HOOKS),
        "plugin_sha256": "a" * 64,
        "policy": ref.policy(table),
        "model": "anthropic/claude-sonnet-5",
        "runtime": "openclaw",
        "version": "2026.9.4",
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
    settings = ref.settings_of(cfg, 2**51 + 9)
    assert settings.allowed == (111111, 222222, 2**51 + 9)
    assert (settings.gateway_port, settings.model) == (19400, "anthropic/claude-opus-5-5")
    assert ref.layout_of(cfg).host_dir == cfg.data_dir / "host"
    assert ref.layout_of(cfg).home == cfg.data_dir / "host" / "home"


def test_cores_tools_come_through_the_adapters_mcp_server():
    from klepa_core.host.instruction import TOOLS

    settings = ref.HostSettings(
        19300, 19201, 19202, Path("/run/adapter.sock"), Path("/k"), Path("/t"), (1,), "anthropic/claude-sonnet-5"
    )
    layout = ref.HostLayout(runtime_dir=Path("/rt"), host_dir=Path("/h"))
    table = ref.table(settings, layout)
    assert table["mcp.servers.klepa"] == {
        "command": "/rt/node-v24.21.0/bin/node",
        "args": ["/rt/adapter/mcp.ts"],
        "env": {"KLEPA_SOCKET": "/run/adapter.sock"},
    }
    assert sorted(table["tools.alsoAllow"]) == sorted(TOOLS)  # exact names only (spec 4.3)
    assert table["gateway.tools.deny"] == table["tools.alsoAllow"]  # never through /tools/invoke
    assert "mcp.servers.klepa" in ref.policy(table)  # every heartbeat proves the gateway runs this server
