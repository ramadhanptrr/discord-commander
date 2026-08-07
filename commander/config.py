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
class NetworkConfig:
    host: str
    manual_probe_count: int
    ping_timeout_seconds: int


@dataclass(frozen=True)
class Config:
    discord: DiscordConfig
    edge: EdgeConfig
    network: NetworkConfig


def load_config() -> Config:
    """Build immutable runtime configuration. No secret values are logged."""
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
        network=NetworkConfig(
            host=_host("MIKROTIK_HOST"),
            manual_probe_count=_positive_int("NETWORK_RECOVERY_PING_COUNT", default=5),
            ping_timeout_seconds=_positive_int("NETWORK_PING_TIMEOUT", default=3),
        ),
    )
