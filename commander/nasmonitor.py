from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from commander.config import ConfigSource, NasWatchdogConfig, load_nas_watchdog_config
from commander.nas import NasController
from commander.netmonitor import format_duration
from commander.power_state import PowerOperationState


logger = logging.getLogger("commander.nasmonitor")

# Sleep used after a tick that failed to even load a valid config, so a broken Turso value
# backs off sensibly instead of retrying in a tight loop.
_CONFIG_ERROR_RETRY_SECONDS = 60


class NasUptimeNotifier(Protocol):
    async def send_nas_uptime_reminder(self, uptime: float) -> None: ...


class NasUptimeWatchdog:
    """Remind operators when the NAS has been left online beyond the configured age.

    Reads the ``nas`` group's watchdog values fresh from Turso on every tick (including the tick
    interval itself), so an edited value applies on the next tick without a Commander restart.
    """

    def __init__(
        self,
        nas: NasController,
        turso: ConfigSource,
        power_state: PowerOperationState,
        notifier: NasUptimeNotifier,
    ) -> None:
        self._nas = nas
        self._turso = turso
        self._power_state = power_state
        self._notifier = notifier
        self._last_alert_at: float | None = None

    async def run(self) -> None:
        logger.info("NAS uptime watchdog started")
        while True:
            sleep_seconds = _CONFIG_ERROR_RETRY_SECONDS
            try:
                config = load_nas_watchdog_config(self._turso)
                await self._tick(config)
                sleep_seconds = config.check_interval_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("NAS uptime watchdog tick failed")
            await asyncio.sleep(sleep_seconds)

    async def _tick(self, config: NasWatchdogConfig) -> None:
        # A wake or shutdown is a temporary transition, not a forgotten-online state.
        if self._power_state.active_operation is not None:
            return

        if not await asyncio.to_thread(self._nas.is_online):
            self._last_alert_at = None
            return

        boot_epoch = await asyncio.to_thread(self._nas.get_boot_epoch)
        if boot_epoch is None:
            return

        uptime = time.time() - boot_epoch
        if uptime < config.max_age_seconds:
            self._last_alert_at = None
            return

        now = time.time()
        if (
            self._last_alert_at is None
            or now - self._last_alert_at >= config.reminder_interval_seconds
        ):
            await self._notifier.send_nas_uptime_reminder(uptime)
            self._last_alert_at = now
            logger.info("NAS uptime reminder sent: uptime=%s", format_duration(uptime))
