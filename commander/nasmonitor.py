from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from commander.config import NasWatchdogConfig
from commander.nas import NasController
from commander.netmonitor import format_duration
from commander.power_state import PowerOperationState


logger = logging.getLogger("commander.nasmonitor")


class NasUptimeNotifier(Protocol):
    async def send_nas_uptime_reminder(self, uptime: float) -> None: ...


class NasUptimeWatchdog:
    """Remind operators when the NAS has been left online beyond the configured age."""

    def __init__(
        self,
        nas: NasController,
        config: NasWatchdogConfig,
        power_state: PowerOperationState,
        notifier: NasUptimeNotifier,
    ) -> None:
        self._nas = nas
        self._config = config
        self._power_state = power_state
        self._notifier = notifier
        self._last_alert_at: float | None = None

    async def run(self) -> None:
        logger.info(
            "NAS uptime watchdog started: interval=%ss max_age=%ss reminder=%ss",
            self._config.check_interval_seconds,
            self._config.max_age_seconds,
            self._config.reminder_interval_seconds,
        )
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("NAS uptime watchdog tick failed")
            await asyncio.sleep(self._config.check_interval_seconds)

    async def _tick(self) -> None:
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
        if uptime < self._config.max_age_seconds:
            self._last_alert_at = None
            return

        now = time.time()
        if (
            self._last_alert_at is None
            or now - self._last_alert_at >= self._config.reminder_interval_seconds
        ):
            await self._notifier.send_nas_uptime_reminder(uptime)
            self._last_alert_at = now
            logger.info("NAS uptime reminder sent: uptime=%s", format_duration(uptime))
