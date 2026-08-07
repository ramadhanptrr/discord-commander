from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from commander.config import NetworkConfig
from commander.network import NetworkChecker


logger = logging.getLogger("commander.netmonitor")


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
    """Edge-triggered home-network monitor that only alerts on DOWN/RECOVERED transitions."""

    def __init__(
        self,
        checker: NetworkChecker,
        config: NetworkConfig,
        notifier: NetworkAlertNotifier,
    ) -> None:
        self._checker = checker
        self._config = config
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
        logger.info(
            "Network watchdog started: host=%s interval=%ss probes=%s recovery_probes=%s "
            "timeout=%ss anchor=%s confirm_delay=%ss",
            self._config.host,
            self._config.watchdog_interval_seconds,
            self._config.watchdog_probe_count,
            self._config.watchdog_recovery_probe_count,
            self._config.ping_timeout_seconds,
            self._config.anchor_host or "disabled",
            self._config.confirm_delay_seconds,
        )
        while True:
            try:
                if self._is_down:
                    await self._check_recovery()
                else:
                    await self._check_outage()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Network watchdog tick failed")
            await asyncio.sleep(self._config.watchdog_interval_seconds)

    async def _ping(self, host: str, count: int) -> bool:
        return await asyncio.to_thread(self._checker.is_reachable, host, count)

    async def _check_outage(self) -> None:
        if await self._ping(self._config.host, self._config.watchdog_probe_count):
            return

        logger.warning(
            "No reply from %s after %s probes; verifying outage",
            self._config.host,
            self._config.watchdog_probe_count,
        )
        if not await self._anchor_healthy():
            return

        if self._config.confirm_delay_seconds:
            await asyncio.sleep(self._config.confirm_delay_seconds)
            if await self._ping(self._config.host, self._config.watchdog_probe_count):
                logger.info("Home network recovered during confirmation window; no alert")
                return
            if not await self._anchor_healthy():
                return

        checks = [f"{self._config.watchdog_probe_count} failed probe(s)"]
        if self._config.confirm_delay_seconds:
            checks.append("a delayed confirmation round")
        if self._config.anchor_host:
            checks.append(f"a healthy {self._config.anchor_host} uplink check")

        detected_at = time.time()
        # Notification delivery succeeds before state changes so a failed Discord request retries.
        await self._notifier.send_network_down(self._config.host, checks)
        self._is_down = True
        self._down_since = detected_at

    async def _check_recovery(self) -> None:
        if not await self._ping(self._config.host, self._config.watchdog_recovery_probe_count):
            return

        downtime = time.time() - self._down_since if self._down_since is not None else None
        # Keep DOWN state until the recovery message is successfully delivered.
        await self._notifier.send_network_recovered(self._config.host, downtime)
        self._is_down = False
        self._down_since = None

    async def _anchor_healthy(self) -> bool:
        if not self._config.anchor_host:
            return True
        if await self._ping(
            self._config.anchor_host, self._config.watchdog_recovery_probe_count
        ):
            return True

        logger.warning(
            "Anchor %s is unreachable; home network state remains unchanged",
            self._config.anchor_host,
        )
        return False
