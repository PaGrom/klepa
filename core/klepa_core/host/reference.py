"""The host's reference configuration (spec 7.1): every OpenClaw setting the engine relies on, with its exact value.

Core writes the gateway's openclaw.json from this table and nothing else, and OpenClaw cannot change the file
(OPENCLAW_CONFIG_READONLY). Every heartbeat carries the values of the policy keys as the running gateway holds them,
and Core compares them with the table (spec 4.6). A setting missing from the table keeps OpenClaw's default; the
defaults the engine relies on are all written down here.

Keys are dotted paths; no part of a path contains a dot, which `check_table` makes sure of.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from .supervisor import REQUIRED_HOOKS, Expectations

PLUGIN_ID = "klepa-adapter"
MODEL_PROFILE = "anthropic:klepa"  # the gateway's auth profile for the model: `host login` stores the token there
RUNTIME_ID = "openclaw"  # OpenClaw's built-in runtime; never claude-cli, which leaves the proxy (spec 7.1)
GATEWAY_TOKEN_PROVIDER = "klepa-gateway-token"

FIXED: dict[str, Any] = {
    # The gateway: loopback only, no browser surfaces, no live reload, nothing paired silently.
    "gateway.mode": "local",
    "gateway.bind": "loopback",
    "gateway.auth.mode": "token",
    "gateway.reload.mode": "off",
    "gateway.terminal.enabled": False,
    "gateway.controlUi.enabled": False,
    "gateway.nodes.pairing.autoApproveLocal": False,
    "gateway.tailscale.mode": "off",
    "gateway.http.endpoints.chatCompletions.enabled": False,
    "gateway.http.endpoints.responses.enabled": False,
    "discovery.mdns.mode": "off",
    # Plugins: Telegram, the model's provider and the adapter. OpenClaw adds memory-core to the list on its own (spike
    # report, point 24), so the table says so; the memory slot "none" keeps it from loading.
    "plugins.allow": ["telegram", "anthropic", PLUGIN_ID, "memory-core"],
    "plugins.slots.memory": "none",
    "plugins.entries.memory-core.config.dreaming.enabled": False,
    f"plugins.entries.{PLUGIN_ID}.enabled": True,
    f"plugins.entries.{PLUGIN_ID}.hooks.allowConversationAccess": True,
    # Tools: none in stage 1; the engine's own arrive by exact name in stage 2.
    "tools.profile": "minimal",
    "tools.alsoAllow": [],
    "tools.deny": ["session_status"],
    "tools.sessions.visibility": "self",
    "tools.fs.workspaceOnly": True,
    "tools.media.image.enabled": False,
    "tools.media.audio.enabled": False,
    "tools.media.video.enabled": False,
    # Commands: nobody has any. An empty allowFrom object would mean "not set" (spec 12).
    "commands.allowFrom": {"*": []},
    "commands.ownerAllowFrom": [],
    "commands.native": False,
    "commands.nativeSkills": False,
    "commands.restart": False,
    "commands.bash": False,
    "commands.config": False,
    # Telegram, through the gatekeeper only.
    "channels.telegram.enabled": True,
    "channels.telegram.dmPolicy": "allowlist",
    "channels.telegram.groupPolicy": "disabled",
    "channels.telegram.streaming.mode": "off",
    "channels.telegram.linkPreview": False,
    "channels.telegram.configWrites": False,
    "channels.telegram.reactionNotifications": "off",
    "channels.telegram.ackReaction": "",
    "channels.telegram.textChunkLimit": 3000,
    "channels.telegram.errorPolicy": "silent",
    "channels.telegram.capabilities.inlineButtons": "off",
    "channels.telegram.actions.reactions": False,
    "channels.telegram.actions.poll": False,
    "channels.telegram.actions.deleteMessage": False,
    "channels.telegram.actions.editMessage": False,
    "channels.telegram.actions.sticker": False,
    "channels.telegram.actions.createForumTopic": False,
    "channels.telegram.actions.editForumTopic": False,
    # The network: everything through Core's egress proxy.
    "proxy.enabled": True,
    "proxy.loopbackMode": "gateway-only",
    # Messages and sessions.
    "messages.queue.mode": "followup",
    "messages.visibleReplies": "automatic",
    "messages.ackReactionScope": "off",
    "messages.statusReactions.enabled": False,
    "session.dmScope": "per-channel-peer",
    "session.reset.mode": "daily",
    "session.reset.atHour": 4,
    # Off: sandbox, heartbeat turns, skills, background learning, scheduled jobs, statistics and updates.
    "agents.defaults.sandbox.mode": "off",
    "agents.defaults.heartbeat.every": "0m",
    "agents.defaults.skills": [],
    "skills.workshop.autonomous.mode": "off",
    "cron.enabled": False,
    "telemetry.enabled": False,
    "models.catalogRefresh.enabled": False,
    "update.checkOnStart": False,
    "update.auto.enabled": False,
    # The host's media stay local and short-lived.
    "attachments.ttlHours": 24,
    "logging.level": "info",
    "logging.consoleLevel": "error",  # launchd keeps the console in files that nobody rotates
    "logging.maxFileBytes": 20_000_000,  # the log keeps metadata only; five archives of this size at most
}

# Not in heartbeats: the gateway resolves the token reference into the secret itself, and it keeps the plugin's own
# config out of the configuration it shows plugins. The adapter's config proves itself: its heartbeat reaches the
# socket, signed with the key.
UNREPORTED = frozenset(
    {"gateway.auth.token", f"secrets.providers.{GATEWAY_TOKEN_PROVIDER}", f"plugins.entries.{PLUGIN_ID}.config"}
)


@dataclass(frozen=True)
class HostLayout:
    """Where one installation keeps its gateway: the runtime in Klepa's program folder, the rest in the data folder."""

    runtime_dir: Path  # Node, OpenClaw and the adapter
    host_dir: Path  # the gateway's config, state, workspace and logs; personal data, like the rest of data_dir

    @property
    def adapter_dir(self) -> Path:
        return self.runtime_dir / "adapter"

    @property
    def config_path(self) -> Path:
        return self.host_dir / "openclaw.json"

    @property
    def home(self) -> Path:
        """The gateway's own HOME: OpenClaw's caches go here, and it finds nothing of the owner's (~/.claude,
        ~/.openclaw) by default paths."""
        return self.host_dir / "home"

    @property
    def state_dir(self) -> Path:
        return self.host_dir / "state"

    @property
    def workspace(self) -> Path:
        return self.host_dir / "workspace"

    @property
    def logs_dir(self) -> Path:
        return self.host_dir / "logs"

    @property
    def gateway_log(self) -> Path:
        return self.logs_dir / "gateway.log"

    @property
    def gateway_token(self) -> Path:
        return self.host_dir / "gateway.token"


@dataclass(frozen=True)
class HostSettings:
    """What differs between installations."""

    gateway_port: int
    api_port: int
    proxy_port: int
    socket_path: Path
    adapter_key_path: Path
    host_token_path: Path
    allowed: tuple[int, ...]  # the members and the probe peer
    model: str


def layout_of(cfg: Config) -> HostLayout:
    assert cfg.host is not None
    return HostLayout(runtime_dir=cfg.host.runtime_dir, host_dir=cfg.host_dir)


def settings_of(cfg: Config, probe: int) -> HostSettings:
    """The installation's values: the members in their config order, then the probe peer (spec 4.6)."""
    assert cfg.host is not None
    return HostSettings(
        gateway_port=cfg.host.gateway_port,
        api_port=cfg.host.api_port,
        proxy_port=cfg.host.proxy_port,
        socket_path=cfg.host.socket_path,
        adapter_key_path=cfg.adapter_key_path,
        host_token_path=cfg.host_token_path,
        allowed=(*(member.telegram_id for member in cfg.members), probe),
        model=cfg.host.model,
    )


def table(settings: HostSettings, layout: HostLayout) -> dict[str, Any]:
    """The whole reference table of one installation."""
    model = settings.model
    values: dict[str, Any] = {
        "gateway.port": settings.gateway_port,
        "gateway.auth.token": {"source": "file", "provider": GATEWAY_TOKEN_PROVIDER, "id": "value"},
        f"secrets.providers.{GATEWAY_TOKEN_PROVIDER}": {
            "source": "file",
            "path": str(layout.gateway_token),
            "mode": "singleValue",
        },
        "channels.telegram.apiRoot": f"http://127.0.0.1:{settings.api_port}",  # exactly: the Host check (plan 1c-1)
        "channels.telegram.tokenFile": str(settings.host_token_path),
        "channels.telegram.allowFrom": [str(peer) for peer in settings.allowed],
        "proxy.proxyUrl": f"http://127.0.0.1:{settings.proxy_port}",
        "agents.defaults.workspace": str(layout.workspace),
        "agents.defaults.model.primary": model,
        "agents.defaults.modelPolicy.allow": [model],  # one model, and nothing a session could switch to
        f"agents.defaults.models.{model}.agentRuntime.id": RUNTIME_ID,
        "plugins.load.paths": [str(layout.adapter_dir)],
        "logging.file": str(layout.gateway_log),
    }
    values[f"plugins.entries.{PLUGIN_ID}.config"] = {
        "socket": str(settings.socket_path),
        "keyFile": str(settings.adapter_key_path),
        "policy": sorted(key for key in (FIXED | values) if key not in UNREPORTED),
    }
    result = FIXED | values
    check_table(result)
    return result


def policy(reference: Mapping[str, Any]) -> dict[str, Any]:
    """The values every heartbeat must report, by key."""
    return {key: value for key, value in reference.items() if key not in UNREPORTED}


def expectations(reference: Mapping[str, Any], adapter_sha256: str, version: str) -> Expectations:
    """What Core checks in every heartbeat of a gateway it runs itself (spec 4.6, 7.1): the adapter it shipped, the
    table, the model, the runtime, and the OpenClaw release the adapter was proven with (spec 14.1)."""
    return Expectations(
        hooks=REQUIRED_HOOKS,
        plugin_sha256=adapter_sha256,
        policy=policy(reference),
        model=str(reference["agents.defaults.model.primary"]),
        runtime=RUNTIME_ID,
        version=version,
    )


def check_table(reference: Mapping[str, Any]) -> None:
    """No key may be a prefix of another, and no part of a key may be empty."""
    keys = sorted(reference)
    for key in keys:
        if any(part == "" for part in key.split(".")):
            raise ValueError(f"empty part in reference key {key!r}")
    for shorter, longer in itertools.pairwise(keys):
        if longer.startswith(shorter + "."):
            raise ValueError(f"reference key {shorter!r} hides {longer!r}")


def render(reference: Mapping[str, Any]) -> dict[str, Any]:
    """The nested openclaw.json from the dotted table."""
    config: dict[str, Any] = {}
    for key, value in sorted(reference.items()):
        node = config
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return config
