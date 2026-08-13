from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from commander.config import ConfigSource, NetworkConfig, load_network_config
from commander.network import NetworkChecker


logger = logging.getLogger("commander.netmonitor")

# Sleep used after a tick that failed to even load a valid config, so a broken Turso value
# backs off sensibly instead of retrying in a tight loop.
_CONFIG_ERROR_RETRY_SECONDS = 60


class NetworkAlertNotifier(Protocol):
    async def send_network_down(self, host: str, checks: list[str]) -> None: ...

    async def send_network_recovered(self, host: str, downtime: float | None) -> None: ...


def format_duration(seconds: float) -> str:
    total_minutes = max(int(seconds // 60), 0)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class NetworkWatchdog:
    """Edge-triggered home-network monitor that only alerts on DOWN/RECOVERED transitions.

    Reads the ``home_network`` group fresh from Turso on every tick (including the tick interval
    itself), so an edited value applies on the next tick without a Commander restart. A bad/
    missing value fails just that tick (logged) and retries after ``_CONFIG_ERROR_RETRY_SECONDS``.
    """

    def __init__(
        self,
        checker: NetworkChecker,
        turso: ConfigSource,
        notifier: NetworkAlertNotifier,
    ) -> None:
        self._checker = checker
        self._turso = turso
        self._notifier = notifier
        self._is_down = False
        self._down_since: float | None = None

    @property
    def is_down(self) -> bool:
        return self._is_down

    @property
    def down_since(self) -> float | None:
        return self._down_since

    async def run(self) -> None:
        logger.info("Network watchdog started")
        while True:
            sleep_seconds = _CONFIG_ERROR_RETRY_SECONDS
            try:
                config = load_network_config(self._turso)
                if self._is_down:
                    await self._check_recovery(config)
                else:
                    await self._check_outage(config)
                sleep_seconds = config.watchdog_interval_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Network watchdog tick failed")
            await asyncio.sleep(sleep_seconds)

    async def _ping(self, host: str, count: int) -> bool:
        return await asyncio.to_thread(self._checker.is_reachable, host, count)

    async def _check_outage(self, config: NetworkConfig) -> None:
        if await self._ping(config.host, config.watchdog_probe_count):
            return

        logger.warning(
            "No reply from %s after %s probes; verifying outage",
            config.host,
            config.watchdog_probe_count,
        )
        if not await self._anchor_healthy(config):
            return

        if config.confirm_delay_seconds:
            await asyncio.sleep(config.confirm_delay_seconds)
            if await self._ping(config.host, config.watchdog_probe_count):
                logger.info("Home network recovered during confirmation window; no alert")
                return
            if not await self._anchor_healthy(config):
                return

        checks = [f"{config.watchdog_probe_count} failed probe(s)"]
        if config.confirm_delay_seconds:
            checks.append("a delayed confirmation round")
        if config.anchor_host:
            checks.append(f"a healthy {config.anchor_host} uplink check")

        detected_at = time.time()
        # Notification delivery succeeds before state changes so a failed Discord request retries.
        await self._notifier.send_network_down(config.host, checks)
        self._is_down = True
        self._down_since = detected_at

    async def _check_recovery(self, config: NetworkConfig) -> None:
        if not await self._ping(config.host, config.watchdog_recovery_probe_count):
            return

        downtime = time.time() - self._down_since if self._down_since is not None else None
        # Keep DOWN state until the recovery message is successfully delivered.
        await self._notifier.send_network_recovered(config.host, downtime)
        self._is_down = False
        self._down_since = None

    async def _anchor_healthy(self, config: NetworkConfig) -> bool:
        if not config.anchor_host:
            return True
        if await self._ping(config.anchor_host, config.watchdog_recovery_probe_count):
            return True

        logger.warning(
            "Anchor %s is unreachable; home network state remains unchanged",
            config.anchor_host,
        )
        return False
