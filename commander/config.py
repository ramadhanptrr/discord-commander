from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from infisical_sdk import InfisicalSDKClient


def _read_secret_file(env_var: str) -> str:
    """Read one Docker-secret value from the path held in ``env_var``."""
    path = os.getenv(env_var)
    if not path:
        raise RuntimeError(f"Missing {env_var}")

    with open(path, encoding="utf-8") as secret_file:
        value = secret_file.read().strip()

    if not value:
        raise RuntimeError(f"Secret file for {env_var} is empty")
    return value


def _env_or_default(env_var: str, default: str) -> str:
    return os.getenv(env_var, default)


def get_infisical_secrets() -> dict[str, str]:
    """Load application values from the configured Infisical project and path."""
    client = InfisicalSDKClient(host="https://app.infisical.com", cache_ttl=600)
    client.auth.universal_auth.login(
        client_id=_read_secret_file("INFISICAL_CLIENT_ID_FILE"),
        client_secret=_read_secret_file("INFISICAL_CLIENT_SECRET_FILE"),
    )

    response = client.secrets.list_secrets(
        project_id=_read_secret_file("INFISICAL_PROJECT_ID_FILE"),
        environment_slug=_env_or_default("INFISICAL_ENVIRONMENT", "prod"),
        secret_path=_env_or_default("INFISICAL_SECRET_PATH", "/"),
    )
    return {secret.secretKey: secret.secretValue for secret in response.secrets}


class Secrets:
    """Small process-local cache; configuration itself is materialized at startup."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._loaded_at: float | None = None
        self._ttl_seconds = 600

    def get(self, key: str) -> str:
        now = time.time()
        if self._loaded_at is None or now - self._loaded_at > self._ttl_seconds:
            self._values = get_infisical_secrets()
            self._loaded_at = now
        return self._values.get(key, "")


_secrets = Secrets()


class ConfigSource(Protocol):
    """Anything that can hand back the flat key/value rows for a Turso identifier group."""

    def read_group(self, identifier: str) -> dict[str, str]: ...


class ConfigValues:
    """Validates configuration values pulled from an arbitrary key/value source.

    Discord and MikroTik values read straight from Infisical; edge/NAS/home-network values
    read from a Turso identifier group (already synced into the local replica). Both sources
    expose the same "get raw string for this key" shape, so the same validation rules apply
    regardless of where a value came from.
    """

    def __init__(self, get: Callable[[str], str]) -> None:
        self._get = get

    def optional(self, key: str) -> str:
        return self._get(key).strip()

    def required(self, key: str) -> str:
        value = self.optional(key)
        if not value:
            raise RuntimeError(f"Missing required secret: {key}")
        return value

    def positive_int(self, key: str, default: int | None = None) -> int:
        raw = self.optional(key)
        if not raw:
            if default is None:
                raise RuntimeError(f"Missing required secret: {key}")
            return default

        try:
            value = int(raw)
        except ValueError as error:
            raise RuntimeError(f"Secret {key} must be an integer") from error

        if value <= 0:
            raise RuntimeError(f"Secret {key} must be a positive integer")
        return value

    def non_negative_int(self, key: str, default: int | None = None) -> int:
        raw = self.optional(key)
        if not raw:
            if default is None:
                raise RuntimeError(f"Missing required secret: {key}")
            return default

        try:
            value = int(raw)
        except ValueError as error:
            raise RuntimeError(f"Secret {key} must be an integer") from error

        if value < 0:
            raise RuntimeError(f"Secret {key} must be zero or a positive integer")
        return value

    def snowflake_list(self, key: str) -> frozenset[int]:
        raw = self.required(key)
        values: set[int] = set()

        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                value = int(item)
            except ValueError as error:
                raise RuntimeError(f"Secret {key} contains a non-integer Discord ID") from error
            if value <= 0:
                raise RuntimeError(f"Secret {key} contains a non-positive Discord ID")
            values.add(value)

        if not values:
            raise RuntimeError(f"Secret {key} must contain at least one Discord ID")
        return frozenset(values)

    def host(self, key: str) -> str:
        value = self.required(key)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value):
            raise RuntimeError(f"Secret {key} must be a safe hostname or IP address")
        return value

    def ssh_username(self, key: str) -> str:
        value = self.required(key)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise RuntimeError(f"Secret {key} must be a safe SSH username")
        return value

    def script_path(self, key: str) -> str:
        value = self.required(key)
        if not re.fullmatch(r"/[\w./-]+", value):
            raise RuntimeError(f"Secret {key} must be an absolute safe script path")
        return value

    def mac_address(self, key: str) -> str:
        value = self.required(key).upper()
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value):
            raise RuntimeError(f"Secret {key} must be a colon-separated MAC address")
        return value

    def router_interface(self, key: str) -> str:
        value = self.required(key)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()/-]*", value):
            raise RuntimeError(f"Secret {key} must be a safe RouterOS interface name")
        return value


def _infisical_values() -> ConfigValues:
    return ConfigValues(_secrets.get)


def _group_values(group: dict[str, str]) -> ConfigValues:
    return ConfigValues(lambda key: group.get(key, ""))


@dataclass(frozen=True)
class DiscordConfig:
    bot_token: str
    guild_ids: frozenset[int]
    control_room_channel_id: int
    watchdog_channel_id: int
    allowed_user_ids: frozenset[int]


@dataclass(frozen=True)
class EdgeConfig:
    internal_ip: str
    ssh_port: int
    ssh_user: str
    info_script: str


@dataclass(frozen=True)
class MikroTikConfig:
    host: str
    port: int
    username: str
    ssh_key_path: str | None


@dataclass(frozen=True)
class NasConfig:
    ip: str
    mac: str
    wol_interface: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    shutdown_script: str
    uptime_script: str
    ssh_key_path: str | None


@dataclass(frozen=True)
class NasWatchdogConfig:
    check_interval_seconds: int
    max_age_seconds: int
    reminder_interval_seconds: int


@dataclass(frozen=True)
class NetworkConfig:
    host: str
    manual_probe_count: int
    ping_timeout_seconds: int
    watchdog_interval_seconds: int
    watchdog_probe_count: int
    watchdog_recovery_probe_count: int
    anchor_host: str | None
    confirm_delay_seconds: int


@dataclass(frozen=True)
class TursoConfig:
    database_url: str
    auth_token: str
    sync_interval_seconds: int
    down_reminder_seconds: int


@dataclass(frozen=True)
class Config:
    discord: DiscordConfig
    edge: EdgeConfig
    mikrotik: MikroTikConfig
    nas: NasConfig
    nas_watchdog: NasWatchdogConfig
    network: NetworkConfig


def load_turso_config() -> TursoConfig:
    """Read Turso connection/sync bootstrap values from Infisical.

    These four values must be available before the Turso local replica exists, so (per
    migrations/turso_migrations.md §3) they stay on the existing Infisical bootstrap path rather
    than living inside the Turso-backed data they are used to fetch.
    """
    v = _infisical_values()
    return TursoConfig(
        database_url=v.required("TURSO_DATABASE_URL"),
        auth_token=v.required("TURSO_AUTH_TOKEN"),
        sync_interval_seconds=v.positive_int("TURSO_DB_SYNC_INTERVAL") * 60,
        down_reminder_seconds=v.positive_int("TURSO_DB_DOWN_REMINDER") * 60,
    )


def _ssh_key_path(turso: ConfigSource) -> str | None:
    home_network = _group_values(turso.read_group("home_network"))
    return home_network.optional("SSH_KEY_PATH") or None


def load_edge_config(turso: ConfigSource) -> EdgeConfig:
    """Read the ``edge`` group fresh from the local replica. Safe to call on every action."""
    edge = _group_values(turso.read_group("edge"))
    return EdgeConfig(
        internal_ip=edge.host("EDGE_INTERNAL_IP"),
        ssh_port=edge.positive_int("EDGE_SSH_PORT", default=22),
        ssh_user=edge.ssh_username("EDGE_SSH_USER"),
        info_script=edge.script_path("EDGE_INFO_SCRIPT"),
    )


def load_mikrotik_config(turso: ConfigSource) -> MikroTikConfig:
    """Read MikroTik identity (Infisical) plus the shared SSH key path (Turso), fresh each call."""
    v = _infisical_values()
    return MikroTikConfig(
        host=v.host("MIKROTIK_HOST"),
        port=v.positive_int("MIKROTIK_PORT", default=22),
        username=v.ssh_username("MIKROTIK_USERNAME"),
        ssh_key_path=_ssh_key_path(turso),
    )


def load_nas_config(turso: ConfigSource) -> NasConfig:
    """Read the ``nas`` group (plus the shared SSH key path) fresh. Safe to call on every action."""
    nas = _group_values(turso.read_group("nas"))
    return NasConfig(
        ip=nas.host("NAS_IP"),
        mac=nas.mac_address("NAS_MAC"),
        wol_interface=nas.router_interface("NAS_WOL_INTERFACE"),
        ssh_host=nas.host("NAS_IP"),
        ssh_port=nas.positive_int("NAS_SSH_PORT", default=22),
        ssh_user=nas.ssh_username("NAS_USER"),
        shutdown_script=nas.script_path("NAS_SHUTDOWN_SCRIPT"),
        uptime_script=nas.script_path("NAS_UPTIME_SCRIPT"),
        ssh_key_path=_ssh_key_path(turso),
    )


def load_nas_watchdog_config(turso: ConfigSource) -> NasWatchdogConfig:
    """Read the NAS watchdog timing values fresh. Called on every watchdog tick."""
    nas = _group_values(turso.read_group("nas"))
    return NasWatchdogConfig(
        check_interval_seconds=nas.positive_int("NAS_MONITOR_INTERVAL", default=60),
        max_age_seconds=nas.positive_int("NAS_MAX_AGE_MINUTE") * 60,
        reminder_interval_seconds=nas.positive_int("NAS_MAX_AGE_REMINDER_MINUTE") * 60,
    )


def load_network_config(turso: ConfigSource) -> NetworkConfig:
    """Read the ``home_network`` group (plus the Infisical gateway host) fresh each call."""
    v = _infisical_values()
    home_network = _group_values(turso.read_group("home_network"))

    anchor_raw = home_network.optional("NETWORK_ANCHOR_HOST")
    anchor_host = None
    if anchor_raw:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", anchor_raw):
            raise RuntimeError("Secret NETWORK_ANCHOR_HOST must be a safe hostname or IP address")
        anchor_host = anchor_raw

    return NetworkConfig(
        host=v.host("MIKROTIK_HOST"),
        manual_probe_count=home_network.positive_int("NETWORK_RECOVERY_PING_COUNT", default=5),
        ping_timeout_seconds=home_network.positive_int("NETWORK_PING_TIMEOUT", default=3),
        watchdog_interval_seconds=home_network.positive_int("TIMED_OUT_INTERVAL", default=10) * 60,
        watchdog_probe_count=home_network.positive_int("NETWORK_PING_COUNT", default=10),
        watchdog_recovery_probe_count=home_network.positive_int(
            "NETWORK_RECOVERY_PING_COUNT", default=5
        ),
        anchor_host=anchor_host,
        confirm_delay_seconds=home_network.non_negative_int("NETWORK_CONFIRM_DELAY", default=15),
    )


def load_config(turso: ConfigSource) -> Config:
    """Build the startup configuration snapshot. No secret values are logged.

    Used once at process start (channel validation, Discord/MikroTik identity, initial watchdog
    interval for cosmetic display). Edge, NAS, and home-network operational values are re-read
    live from the Turso replica on every action instead of being held from this snapshot -- see
    ``load_edge_config``/``load_mikrotik_config``/``load_nas_config``/``load_nas_watchdog_config``/
    ``load_network_config``.
    """
    v = _infisical_values()
    return Config(
        discord=DiscordConfig(
            bot_token=v.required("DISCORD_BOT_TOKEN"),
            guild_ids=v.snowflake_list("DISCORD_GUILD_IDS"),
            control_room_channel_id=v.positive_int("DISCORD_CONTROL_ROOM_CHANNEL_ID"),
            watchdog_channel_id=v.positive_int("DISCORD_WATCHDOG_CHANNEL_ID"),
            allowed_user_ids=v.snowflake_list("DISCORD_ALLOWED_USER_IDS"),
        ),
        edge=load_edge_config(turso),
        mikrotik=load_mikrotik_config(turso),
        nas=load_nas_config(turso),
        nas_watchdog=load_nas_watchdog_config(turso),
        network=load_network_config(turso),
    )
