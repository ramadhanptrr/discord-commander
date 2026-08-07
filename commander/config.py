from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

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


def _required(key: str) -> str:
    value = _secrets.get(key).strip()
    if not value:
        raise RuntimeError(f"Missing required secret: {key}")
    return value


def _positive_int(key: str, default: int | None = None) -> int:
    raw = _secrets.get(key).strip()
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


def _non_negative_int(key: str, default: int | None = None) -> int:
    raw = _secrets.get(key).strip()
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


def _snowflake_list(key: str) -> frozenset[int]:
    raw = _required(key)
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


def _host(key: str) -> str:
    value = _required(key)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value):
        raise RuntimeError(f"Secret {key} must be a safe hostname or IP address")
    return value


def _ssh_username(key: str) -> str:
    value = _required(key)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError(f"Secret {key} must be a safe SSH username")
    return value


def _script_path(key: str) -> str:
    value = _required(key)
    if not re.fullmatch(r"/[\w./-]+", value):
        raise RuntimeError(f"Secret {key} must be an absolute safe script path")
    return value


def _mac_address(key: str) -> str:
    value = _required(key).upper()
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value):
        raise RuntimeError(f"Secret {key} must be a colon-separated MAC address")
    return value


def _router_interface(key: str) -> str:
    value = _required(key)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()/-]*", value):
        raise RuntimeError(f"Secret {key} must be a safe RouterOS interface name")
    return value


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
    ssh_key_path: str | None


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
class Config:
    discord: DiscordConfig
    edge: EdgeConfig
    mikrotik: MikroTikConfig
    nas: NasConfig
    network: NetworkConfig


def load_config() -> Config:
    """Build immutable runtime configuration. No secret values are logged."""
    ssh_key_path = _secrets.get("SSH_KEY_PATH").strip() or None

    anchor_raw = _secrets.get("NETWORK_ANCHOR_HOST").strip()
    anchor_host = None
    if anchor_raw:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", anchor_raw):
            raise RuntimeError("Secret NETWORK_ANCHOR_HOST must be a safe hostname or IP address")
        anchor_host = anchor_raw

    return Config(
        discord=DiscordConfig(
            bot_token=_required("DISCORD_BOT_TOKEN"),
            guild_ids=_snowflake_list("DISCORD_GUILD_IDS"),
            control_room_channel_id=_positive_int("DISCORD_CONTROL_ROOM_CHANNEL_ID"),
            watchdog_channel_id=_positive_int("DISCORD_WATCHDOG_CHANNEL_ID"),
            allowed_user_ids=_snowflake_list("DISCORD_ALLOWED_USER_IDS"),
        ),
        edge=EdgeConfig(
            internal_ip=_host("EDGE_INTERNAL_IP"),
            ssh_port=_positive_int("EDGE_SSH_PORT", default=22),
            ssh_user=_ssh_username("EDGE_SSH_USER"),
            info_script=_script_path("EDGE_INFO_SCRIPT"),
        ),
        mikrotik=MikroTikConfig(
            host=_host("MIKROTIK_HOST"),
            port=_positive_int("MIKROTIK_PORT", default=22),
            username=_ssh_username("MIKROTIK_USERNAME"),
            ssh_key_path=ssh_key_path,
        ),
        nas=NasConfig(
            ip=_host("NAS_IP"),
            mac=_mac_address("NAS_MAC"),
            wol_interface=_router_interface("NAS_WOL_INTERFACE"),
            ssh_host=_host("NAS_IP"),
            ssh_port=_positive_int("NAS_SSH_PORT", default=22),
            ssh_user=_ssh_username("NAS_USER"),
            shutdown_script=_script_path("NAS_SHUTDOWN_SCRIPT"),
            ssh_key_path=ssh_key_path,
        ),
        network=NetworkConfig(
            host=_host("MIKROTIK_HOST"),
            manual_probe_count=_positive_int("NETWORK_RECOVERY_PING_COUNT", default=5),
            ping_timeout_seconds=_positive_int("NETWORK_PING_TIMEOUT", default=3),
            watchdog_interval_seconds=_positive_int("TIMED_OUT_INTERVAL", default=10) * 60,
            watchdog_probe_count=_positive_int("NETWORK_PING_COUNT", default=10),
            watchdog_recovery_probe_count=_positive_int(
                "NETWORK_RECOVERY_PING_COUNT", default=5
            ),
            anchor_host=anchor_host,
            confirm_delay_seconds=_non_negative_int("NETWORK_CONFIRM_DELAY", default=15),
        ),
    )
