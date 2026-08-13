from __future__ import annotations

import asyncio
import logging
import os
import time
from enum import Enum
from pathlib import Path
from typing import Protocol

import turso.sync

from commander.config import TursoConfig
from commander.turso.queries.config_queries import SELECT_GROUP_BY_IDENTIFIER


logger = logging.getLogger("commander.turso")

BOOTSTRAP_TIMEOUT_SECONDS = 5


class TursoState(Enum):
    UNKNOWN = "UNKNOWN"
    UP = "UP"
    DOWN = "DOWN"


class TursoBootstrapError(RuntimeError):
    """Raised when the startup Turso bootstrap cannot produce a usable local database."""


class TursoAlertNotifier(Protocol):
    """The subset of DiscordWatchdogNotifier that TursoCacheManager depends on."""

    async def send_turso_down(self, last_successful_sync: float | None) -> None: ...

    async def send_turso_down_reminder(
        self, down_since: float, last_successful_sync: float | None
    ) -> None: ...

    async def send_turso_recovered(self, downtime: float | None) -> None: ...


class TursoCacheManager:
    """Owns the local libSQL replica lifecycle: fresh bootstrap, periodic sync, health state.

    Turso Cloud is the source of truth; ``local_db_path`` is the runtime configuration source.
    Nothing outside this class talks to Turso Cloud directly. Config reads (``read_group``) are
    plain local-file reads and never trigger network traffic.
    """

    def __init__(self, config: TursoConfig, local_db_path: str | os.PathLike[str]) -> None:
        self._config = config
        self._local_db_path = Path(local_db_path)
        self._connection: turso.sync.Connection | None = None
        self._sync_lock: asyncio.Lock | None = None
        self._notifier: TursoAlertNotifier | None = None

        self._state = TursoState.UNKNOWN
        self._last_successful_sync: float | None = None
        self._last_failed_sync: float | None = None
        self._down_since: float | None = None
        self._last_down_notification_at: float | None = None

    # -- observability -----------------------------------------------------------------

    @property
    def state(self) -> TursoState:
        return self._state

    @property
    def local_db_path(self) -> Path:
        return self._local_db_path

    def status_summary(self) -> dict[str, str]:
        """Plain-string status snapshot for future health/status surfaces."""

        def _age(timestamp: float | None) -> str:
            if timestamp is None:
                return "never"
            return f"{int(time.time() - timestamp)}s ago"

        return {
            "turso_state": self._state.value,
            "last_successful_sync": _age(self._last_successful_sync),
            "last_failed_sync": _age(self._last_failed_sync),
            "down_since": _age(self._down_since),
            "local_db_path": str(self._local_db_path),
        }

    def attach_notifier(self, notifier: TursoAlertNotifier) -> None:
        """Wire the Discord watchdog notifier once the bot/channel exist (see lifecycle notes)."""
        self._notifier = notifier

    # -- startup bootstrap --------------------------------------------------------------

    async def bootstrap(self) -> None:
        """Delete any existing local replica and build a fresh one from Turso Cloud.

        Fails closed: any error here must stop application startup rather than run with a stale
        or empty configuration (migrations/turso_migrations.md §6-§8).
        """
        await asyncio.to_thread(self._remove_existing_local_db)

        logger.info("Starting Turso database bootstrap.")
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._connect_and_pull_blocking),
                timeout=BOOTSTRAP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as error:
            raise TursoBootstrapError(
                "Turso bootstrap timed out during application startup."
            ) from error
        except Exception as error:
            raise TursoBootstrapError(
                "Turso bootstrap failed during application startup. "
                "A fresh local database could not be created."
            ) from error

        self._state = TursoState.UP
        self._last_successful_sync = time.time()
        logger.info("Turso database bootstrap completed successfully.")

    def _remove_existing_local_db(self) -> None:
        parent = self._local_db_path.parent
        if not parent.is_dir():
            return

        # The sync engine keeps more than just the main file next to it (WAL, and a "-info"
        # sync-metadata file tracking synced_revision/client_unique_id, at least). Deleting only
        # the main file leaves orphaned metadata that makes the next connect() fail with
        # "main DB file doesn't exists, but metadata is" -- so remove everything that shares its
        # name as a prefix rather than guessing exact companion suffixes.
        matches = sorted(parent.glob(f"{self._local_db_path.name}*"))
        if not matches:
            return

        logger.info("Removing existing Turso local database before startup bootstrap.")
        try:
            for path in matches:
                path.unlink()
        except OSError as error:
            raise TursoBootstrapError(
                "Failed to remove the existing Turso local database before startup bootstrap."
            ) from error

    def _connect_and_pull_blocking(self) -> None:
        self._local_db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = turso.sync.connect(
            str(self._local_db_path),
            remote_url=self._config.database_url,
            auth_token=self._config.auth_token,
        )
        connection.pull()
        self._connection = connection

    # -- runtime config reads (local-only, no network) -----------------------------------

    def read_group(self, identifier: str) -> dict[str, str]:
        """Return ``{attribute_key: attribute_value}`` for one identifier group.

        Local-only; must be called after a successful ``bootstrap()``. Used once at startup to
        materialize the immutable app Config -- it does not re-run on every subsequent read.
        """
        if self._connection is None:
            raise RuntimeError("Turso local database is not ready; bootstrap() must run first")

        rows = self._connection.execute(SELECT_GROUP_BY_IDENTIFIER, (identifier,)).fetchall()
        group = {row[0]: row[1] for row in rows}

        # Key names (not values) are safe to log and make a missing/blank row diagnosable
        # without reaching for a separate DB client.
        blank_keys = sorted(key for key, value in group.items() if not str(value).strip())
        logger.info(
            "Turso group '%s' loaded %d key(s): %s%s",
            identifier,
            len(group),
            sorted(group.keys()),
            f" (blank value: {blank_keys})" if blank_keys else "",
        )
        return group

    # -- periodic synchronization ---------------------------------------------------------

    async def run_periodic_sync(self) -> None:
        logger.info(
            "Starting periodic Turso database synchronization: interval=%ss reminder=%ss",
            self._config.sync_interval_seconds,
            self._config.down_reminder_seconds,
        )
        while True:
            try:
                await asyncio.sleep(self._config.sync_interval_seconds)
                await self._sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Turso periodic synchronization tick failed unexpectedly")

    async def _sync_once(self) -> None:
        async with self._get_sync_lock():
            try:
                await asyncio.to_thread(self._pull_blocking)
            except Exception:
                logger.warning(
                    "Turso database synchronization failed. "
                    "Continuing with the last available local configuration.",
                    exc_info=True,
                )
                self._last_failed_sync = time.time()
                await self._handle_sync_failure()
                return

            logger.info("Turso database synchronization completed successfully.")
            self._last_successful_sync = time.time()
            await self._handle_sync_success()

    def _pull_blocking(self) -> None:
        if self._connection is None:
            self._connect_and_pull_blocking()
        else:
            self._connection.pull()

    def _get_sync_lock(self) -> asyncio.Lock:
        # Created lazily so it always binds to the event loop that actually runs periodic sync
        # (the Discord bot's loop), not to the short-lived loop used for the startup bootstrap.
        if self._sync_lock is None:
            self._sync_lock = asyncio.Lock()
        return self._sync_lock

    async def _handle_sync_failure(self) -> None:
        now = time.time()

        if self._state != TursoState.DOWN:
            self._state = TursoState.DOWN
            self._down_since = now
            self._last_down_notification_at = now
            logger.warning(
                "Turso remains unavailable. A DOWN reminder will be sent if the reminder "
                "interval has elapsed."
            )
            if self._notifier is not None:
                await self._send_notification(
                    self._notifier.send_turso_down(self._last_successful_sync)
                )
            return

        reminder_due = (
            self._last_down_notification_at is None
            or now - self._last_down_notification_at >= self._config.down_reminder_seconds
        )
        if not reminder_due:
            return

        self._last_down_notification_at = now
        if self._notifier is not None:
            await self._send_notification(
                self._notifier.send_turso_down_reminder(
                    self._down_since or now, self._last_successful_sync
                )
            )

    async def _handle_sync_success(self) -> None:
        if self._state != TursoState.DOWN:
            self._state = TursoState.UP
            return

        downtime = time.time() - self._down_since if self._down_since is not None else None
        self._state = TursoState.UP
        self._down_since = None
        self._last_down_notification_at = None
        if self._notifier is not None:
            await self._send_notification(self._notifier.send_turso_recovered(downtime))

    async def _send_notification(self, coro) -> None:
        try:
            await coro
        except Exception:
            logger.exception("Failed to send Turso watchdog notification")

    # -- shutdown ---------------------------------------------------------------------------

    async def aclose(self) -> None:
        if self._connection is not None:
            try:
                await asyncio.to_thread(self._connection.close)
            except Exception:
                logger.exception("Failed to close the Turso local database connection cleanly")
