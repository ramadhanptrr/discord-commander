from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal, Protocol

from shared.config import ConfigSource, NetworkConfig, load_network_config
from shared.network import HOME_NETWORK_EVENT_IDENTIFIER, NetworkChecker
from shared.turso.event_state import EventHistorySource, parse_event_timestamp
from shared.turso.writer import EventWriter


logger = logging.getLogger("worker.network_watchdog")

# Sleep used after a tick that failed to even load a valid config, so a broken Turso value
# backs off sensibly instead of retrying in a tight loop.
_CONFIG_ERROR_RETRY_SECONDS = 60

_TransitionState = Literal["UP", "DOWN"]


class NetworkAlertNotifier(Protocol):
    async def send_network_down(self, host: str, checks: list[str]) -> None: ...

    async def send_network_recovered(self, host: str, downtime: float | None) -> None: ...


class NetworkStateSource(ConfigSource, EventHistorySource, Protocol):
    """Everything ``NetworkWatchdog`` needs from Turso: config reads plus state restore."""


class NetworkWatchdog:
    """Edge-triggered home-network monitor that only alerts on DOWN/RECOVERED transitions.

    Reads the ``home_network`` group fresh from Turso on every tick (including the tick interval
    itself), so an edited value applies on the next tick without a Commander restart. A bad/
    missing value fails just that tick (logged) and retries after ``_CONFIG_ERROR_RETRY_SECONDS``.
    """

    def __init__(
        self,
        checker: NetworkChecker,
        turso: NetworkStateSource,
        notifier: NetworkAlertNotifier,
        writer: EventWriter,
    ) -> None:
        self._checker = checker
        self._turso = turso
        self._notifier = notifier
        self._writer = writer
        self._is_down = False
        self._down_since: float | None = None
        # A transition that failed to persist to Turso Cloud; retried at the top of every
        # subsequent tick until record_event() succeeds. See
        # migrations/network_watchdog_state_migration.md §6.
        self._pending_persist: _TransitionState | None = None
        self._restore_state()

    @property
    def is_down(self) -> bool:
        return self._is_down

    @property
    def down_since(self) -> float | None:
        return self._down_since

    def _restore_state(self) -> None:
        """Best-effort restore from the last persisted transition. Never raises.

        Runs synchronously in __init__ (turso.read_latest_event() is a local-only read, and
        bootstrap() has already completed by the time NetworkWatchdog is constructed in
        worker/main.py), so a container rebuild during a real outage resumes in the DOWN state
        instead of re-detecting it as a fresh outage. See
        migrations/network_watchdog_state_migration.md §7.
        """
        try:
            event = self._turso.read_latest_event(HOME_NETWORK_EVENT_IDENTIFIER)
        except Exception:
            logger.exception(
                "Failed to restore network watchdog state from Turso; defaulting to UP"
            )
            return

        if event is None or event.get("current_state") != "DOWN":
            return

        down_since = parse_event_timestamp(event.get("created_at", ""))
        if down_since is None:
            logger.warning(
                "Restored a DOWN event_history row with an unparsable created_at value; "
                "defaulting to UP"
            )
            return

        self._is_down = True
        self._down_since = down_since
        logger.info(
            "Restored network watchdog state: DOWN since %s",
            time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(down_since)),
        )

    async def _flush_pending_persist(self) -> None:
        if self._pending_persist is not None:
            await self._persist(self._pending_persist)

    async def _persist(self, state: _TransitionState) -> None:
        """Best-effort persist of the current state to Turso Cloud; retried on later ticks."""
        if await self._writer.record_event(HOME_NETWORK_EVENT_IDENTIFIER, state):
            self._pending_persist = None
        else:
            self._pending_persist = state

    async def run(self) -> None:
        logger.info("Network watchdog started")
        while True:
            sleep_seconds = _CONFIG_ERROR_RETRY_SECONDS
            try:
                await self._flush_pending_persist()
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
        await self._persist("DOWN")

    async def _check_recovery(self, config: NetworkConfig) -> None:
        if not await self._ping(config.host, config.watchdog_recovery_probe_count):
            return

        downtime = time.time() - self._down_since if self._down_since is not None else None
        # Keep DOWN state until the recovery message is successfully delivered.
        await self._notifier.send_network_recovered(config.host, downtime)
        self._is_down = False
        self._down_since = None
        await self._persist("UP")

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
