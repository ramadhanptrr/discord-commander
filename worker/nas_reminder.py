from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from shared.config import ConfigSource, NasWatchdogConfig, load_nas_watchdog_config
from shared.duration import format_duration
from shared.nas import NasController
from shared.turso.event_state import EventHistorySource, parse_event_timestamp
from shared.turso.writer import EventWriter


logger = logging.getLogger("worker.nas_reminder")

# Sleep used after a tick that failed to even load a valid config, so a broken Turso value
# backs off sensibly instead of retrying in a tight loop.
_CONFIG_ERROR_RETRY_SECONDS = 60

# Matches the "nas" identifier already used for this group in master_configurations
# (shared/config.py). See migrations/nas_uptime_watchdog_state_migration.md.
_EVENT_IDENTIFIER = "nas"
_REMINDER_SENT = "REMINDER_SENT"


class NasUptimeNotifier(Protocol):
    async def send_nas_uptime_reminder(self, uptime: float) -> None: ...


class NasStateSource(ConfigSource, EventHistorySource, Protocol):
    """Everything ``NasUptimeWatchdog`` needs from Turso: config reads plus state restore."""


class NasUptimeWatchdog:
    """Remind operators when the NAS has been left online beyond the configured age.

    Reads the ``nas`` group's watchdog values fresh from Turso on every tick (including the tick
    interval itself), so an edited value applies on the next tick without a Commander restart.
    """

    def __init__(
        self,
        nas: NasController,
        turso: NasStateSource,
        notifier: NasUptimeNotifier,
        writer: EventWriter,
    ) -> None:
        self._nas = nas
        self._turso = turso
        self._notifier = notifier
        self._writer = writer
        self._last_alert_at: float | None = None
        # A reminder timestamp that failed to persist to Turso Cloud; retried at the top of every
        # subsequent tick until record_event() succeeds. See
        # migrations/nas_uptime_watchdog_state_migration.md.
        self._pending_persist = False
        # Best-effort restore, applied (or discarded) on the first tick that has a live boot epoch
        # to validate it against -- see _apply_restored_alert() and the migration doc for why this
        # cannot simply be trusted at construction time the way NetworkWatchdog's restore can.
        self._restored_alert_at = self._load_restored_alert_at()

    def _load_restored_alert_at(self) -> float | None:
        """Best-effort read of the last persisted reminder timestamp. Never raises."""
        try:
            event = self._turso.read_latest_event(_EVENT_IDENTIFIER)
        except Exception:
            logger.exception("Failed to restore NAS uptime watchdog state from Turso")
            return None

        if event is None or event.get("current_state") != _REMINDER_SENT:
            return None

        return parse_event_timestamp(event.get("created_at", ""))

    def _apply_restored_alert(self, boot_epoch: int) -> None:
        """Apply the restored reminder timestamp exactly once, if it belongs to this boot cycle.

        A restored ``last_alert_at`` only means "don't remind again yet" if that reminder was sent
        during the NAS's *current* uptime streak. Since a reminder can only ever have been sent
        after the NAS finished booting, ``restored_alert_at >= boot_epoch`` is both necessary and
        sufficient proof it belongs to the current streak; if the NAS has rebooted since (Commander
        restart happened around a NAS reboot too), the restored value is stale and must not suppress
        a reminder that is actually due again.
        """
        if self._restored_alert_at is None:
            return

        if self._restored_alert_at >= boot_epoch:
            self._last_alert_at = self._restored_alert_at
            logger.info(
                "Restored NAS uptime watchdog state: last reminder at %s",
                time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self._restored_alert_at)),
            )
        else:
            logger.info(
                "Discarding restored NAS uptime watchdog state: the NAS has rebooted since that "
                "reminder was sent"
            )
        self._restored_alert_at = None

    async def _flush_pending_persist(self) -> None:
        if self._pending_persist:
            await self._persist()

    async def _persist(self) -> None:
        """Best-effort persist of the just-sent reminder to Turso Cloud; retried on later ticks."""
        self._pending_persist = not await self._writer.record_event(
            _EVENT_IDENTIFIER, _REMINDER_SENT
        )

    async def run(self) -> None:
        logger.info("NAS uptime watchdog started")
        while True:
            sleep_seconds = _CONFIG_ERROR_RETRY_SECONDS
            try:
                await self._flush_pending_persist()
                config = load_nas_watchdog_config(self._turso)
                await self._tick(config)
                sleep_seconds = config.check_interval_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("NAS uptime watchdog tick failed")
            await asyncio.sleep(sleep_seconds)

    async def _tick(self, config: NasWatchdogConfig) -> None:
        # No explicit Wake/Shutdown coordination needed here (they run in a different process):
        # is_online() is False for the whole boot window, and once online, uptime starts near 0s
        # -- both keep this naturally gated below without needing to know a power operation is
        # in progress. See migrations/worker_split_shared_cache.md.
        if not await asyncio.to_thread(self._nas.is_online):
            self._last_alert_at = None
            return

        boot_epoch = await asyncio.to_thread(self._nas.get_boot_epoch)
        if boot_epoch is None:
            return

        self._apply_restored_alert(boot_epoch)

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
            await self._persist()
            logger.info("NAS uptime reminder sent: uptime=%s", format_duration(uptime))
