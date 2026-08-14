from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

import turso.sync

from commander.config import TursoConfig
from commander.turso.queries.config_queries import SELECT_GROUP_BY_IDENTIFIER
from commander.turso.queries.event_queries import SELECT_LATEST_EVENT_BY_IDENTIFIER


logger = logging.getLogger("commander.turso")

# Hardcoded per design (see bootstrap()'s docstring for why this cannot be a hard cancellation
# guarantee, and why a longer, fixed value is intentional for a fresh remote bootstrap).
BOOTSTRAP_TIMEOUT_SECONDS = 30

# Exact companion files pyturso==0.7.2 keeps next to the main database file for a synced
# connection: a WAL file and a sync-metadata file (synced_revision/client_unique_id). Confirmed by
# inspecting the installed package (string literals "-wal" and "-info" next to the sync engine's
# own source-file name inside its compiled extension) and by the absence of any "-shm"/"-journal"
# companions (this engine does not use SQLite's multi-process shared-memory index). Matching these
# exact names -- instead of a "starts with the db filename" glob -- means an unrelated file an
# operator drops in the same data directory (e.g. a manual "local.db-backup" copy) is never touched.
_LOCAL_REPLICA_COMPANION_SUFFIXES = ("-wal", "-info")


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


_DownNotificationKind = Literal["initial", "reminder"]


class TursoCacheManager:
    """Owns the local libSQL replica lifecycle: fresh bootstrap, periodic sync, health state.

    Turso Cloud is the source of truth; ``local_db_path`` is the runtime configuration source.
    Nothing outside this class talks to Turso Cloud directly.

    Threading model: ``pyturso`` declares DB-API ``threadsafety = 1`` -- a connection must never be
    used from more than one thread, and never concurrently. The single synchronized connection
    (``turso.sync`` -- the one that owns ``pull()``/``close()`` and talks to Turso Cloud) therefore
    lives entirely on one dedicated single-worker executor (``self._executor``); every operation on
    it (``connect``, ``pull``, ``close``) is submitted to that same executor so it always runs on the
    same thread and never overlaps with another operation on that connection. Local reads
    (``read_group``) never touch that connection at all: each call opens its own short-lived,
    plain (non-sync) ``turso.connect()`` connection directly against the local replica file, reads,
    and closes it. This keeps local reads available (from any calling thread) even while Turso Cloud
    is unreachable or a sync/bootstrap call is in flight, without ever sharing a connection object
    across threads.
    """

    def __init__(self, config: TursoConfig, local_db_path: str | os.PathLike[str]) -> None:
        self._config = config
        self._local_db_path = Path(local_db_path)
        self._connection: turso.sync.ConnectionSync | None = None
        self._bootstrapped = False
        # Dedicated single-worker executor: everything that touches the synchronized connection
        # (connect/pull/close) is submitted here so it always runs on the same thread and never
        # overlaps with another such operation. See class docstring.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="turso-sync")
        self._sync_lock: asyncio.Lock | None = None
        self._notifier: TursoAlertNotifier | None = None

        self._state = TursoState.UNKNOWN
        self._last_successful_sync: float | None = None
        self._last_failed_sync: float | None = None
        self._down_since: float | None = None

        # Notification-delivery state, tracked separately from Turso health (self._state) so a
        # failed Discord send is retried instead of being silently treated as delivered.
        self._last_down_notification_at: float | None = None
        self._pending_down_kind: _DownNotificationKind | None = None
        self._recovered_notification_pending = False
        self._pending_recovered_downtime: float | None = None

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
        or empty configuration (see machine_lore/ARCHITECTURE.md for the overall lifecycle).

        Timeout limitation: ``connect``/``pull`` runs as a single blocking call in pyturso, with no
        cooperative cancellation hook. Wrapping it in ``asyncio.wait_for(..., timeout=...)`` only
        stops *waiting* for it after ``BOOTSTRAP_TIMEOUT_SECONDS`` -- it cannot interrupt the
        dedicated worker thread mid-call, which keeps running until pyturso's own call returns (or
        the process exits). If that late call eventually succeeds, the resulting connection is
        closed and discarded rather than adopted (see ``_discard_late_bootstrap_connection``), so a
        timeout can never let a partially-completed bootstrap mutate this manager's state.
        """
        await asyncio.to_thread(self._remove_existing_local_db)

        logger.info("Starting Turso database bootstrap.")
        connect_future: Future[turso.sync.ConnectionSync] = self._executor.submit(
            self._connect_and_pull_blocking
        )
        try:
            connection = await asyncio.wait_for(
                asyncio.wrap_future(connect_future), timeout=BOOTSTRAP_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as error:
            connect_future.add_done_callback(self._discard_late_bootstrap_connection)
            raise TursoBootstrapError(
                f"Turso bootstrap timed out after {BOOTSTRAP_TIMEOUT_SECONDS} seconds during "
                "application startup."
            ) from error
        except Exception as error:
            raise TursoBootstrapError(
                "Turso bootstrap failed during application startup. "
                "A fresh local database could not be created."
            ) from error

        self._connection = connection
        self._bootstrapped = True
        self._state = TursoState.UP
        self._last_successful_sync = time.time()
        logger.info("Turso database bootstrap completed successfully.")

    def _discard_late_bootstrap_connection(self, future: "Future[turso.sync.ConnectionSync]") -> None:
        """Close a connection that finished connecting only after bootstrap() already timed out.

        Runs on the dedicated Turso worker thread (concurrent.futures invokes done-callbacks on
        the thread that produced the result), so closing here still respects the connection's
        thread affinity. bootstrap() never adopted this connection -- self._connection was left
        untouched -- so without this it would leak an open connection and its sync state forever.
        """
        error = future.exception()
        if error is not None:
            return
        connection = future.result()
        try:
            connection.close()
        except Exception:
            logger.exception(
                "Failed to close a Turso connection that finished bootstrapping after the timeout"
            )

    def _remove_existing_local_db(self) -> None:
        parent = self._local_db_path.parent
        if not parent.is_dir():
            return

        name = self._local_db_path.name
        candidates = [self._local_db_path]
        candidates.extend(
            parent / f"{name}{suffix}" for suffix in _LOCAL_REPLICA_COMPANION_SUFFIXES
        )
        # A crashed atomic replace during a previous bootstrap can leave a temp file behind (see
        # pyturso's full-file-write helper, which writes to "<name>.tmp.<pid>.<tid>" before
        # renaming it over the target). This is a narrow, exact-scheme match, not a broad prefix.
        candidates.extend(sorted(parent.glob(f"{name}.tmp.*")))

        matches = [path for path in candidates if path.exists()]
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

    def _connect_and_pull_blocking(self) -> turso.sync.ConnectionSync:
        self._local_db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = turso.sync.connect(
            str(self._local_db_path),
            remote_url=self._config.database_url,
            auth_token=self._config.auth_token,
        )
        connection.pull()
        return connection

    # -- runtime config reads (local-only, no network) -----------------------------------

    def read_group(self, identifier: str) -> dict[str, str]:
        """Return ``{attribute_key: attribute_value}`` for one identifier group.

        Local-only; must be called after a successful ``bootstrap()``. Opens its own short-lived
        local connection instead of the synchronized connection (see class docstring), so it is
        safe to call from any thread, including concurrently with a periodic sync pull.
        """
        if not self._bootstrapped:
            raise RuntimeError("Turso local database is not ready; bootstrap() must run first")

        connection = turso.connect(str(self._local_db_path))
        try:
            rows = connection.execute(SELECT_GROUP_BY_IDENTIFIER, (identifier,)).fetchall()
        finally:
            connection.close()

        group: dict[str, str] = {}
        for row in rows:
            raw_key, raw_value = row[0], row[1]
            # A NULL/blank attribute_key can never be a valid config key; reject the row instead
            # of silently coercing it (e.g. via str(None) -> "None") into something that could pass
            # downstream validation.
            key = "" if raw_key is None else str(raw_key).strip()
            if not key:
                raise RuntimeError(
                    f"Turso group '{identifier}' contains a row with a blank or NULL "
                    "configuration key"
                )
            if key in group:
                # Values are intentionally not included here or in logs below.
                raise RuntimeError(
                    f"Turso group '{identifier}' contains duplicate configuration key '{key}'. "
                    "Each (identifier, attribute_key) pair must be unique -- fix the duplicate "
                    "row in Turso."
                )
            # SQL NULL must not become the literal string "None" -- treat it as an empty value so
            # existing required/optional validation handles it the same way as a blank string.
            group[key] = "" if raw_value is None else str(raw_value).strip()

        # Key names (not values) are safe to log. This runs on every action (live-read config), so
        # routine reads stay at DEBUG; a blank value is still actionable and stays visible at
        # WARNING regardless of LOG_LEVEL.
        blank_keys = sorted(key for key, value in group.items() if not value)
        if blank_keys:
            logger.warning(
                "Turso group '%s' loaded %d key(s) with blank value(s): %s",
                identifier,
                len(group),
                blank_keys,
            )
        else:
            logger.debug(
                "Turso group '%s' loaded %d key(s): %s",
                identifier,
                len(group),
                sorted(group.keys()),
            )
        return group

    def read_latest_event(self, identifier: str) -> dict[str, str] | None:
        """Return the most recent ``event_history`` row for ``identifier``, or ``None``.

        Local-only, same guarantees as ``read_group()`` (opens its own short-lived local
        connection). ``TursoProdWriter`` pushes ``event_history`` rows straight to Turso Cloud; this
        reads them back out of the same periodically synced local replica ``read_group()`` uses, so
        a row written before a restart is already present by the time ``bootstrap()`` finishes the
        next fresh pull -- no direct connection to ``TursoProdWriter`` or Turso Cloud is needed here.
        """
        if not self._bootstrapped:
            raise RuntimeError("Turso local database is not ready; bootstrap() must run first")

        connection = turso.connect(str(self._local_db_path))
        try:
            rows = connection.execute(SELECT_LATEST_EVENT_BY_IDENTIFIER, (identifier,)).fetchall()
        finally:
            connection.close()

        if not rows:
            return None

        raw_state, raw_created_at = rows[0][0], rows[0][1]
        return {
            "current_state": "" if raw_state is None else str(raw_state).strip(),
            "created_at": "" if raw_created_at is None else str(raw_created_at).strip(),
        }

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
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._executor, self._pull_blocking)
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
            self._connection = self._connect_and_pull_blocking()
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
            self._pending_down_kind = "initial"
            self._recovered_notification_pending = False
            self._pending_recovered_downtime = None
            logger.warning(
                "Turso is now considered DOWN. A DOWN notification will be sent (and retried on "
                "a later sync tick if delivery fails)."
            )
        elif self._pending_down_kind is None:
            reminder_due = (
                self._last_down_notification_at is None
                or now - self._last_down_notification_at >= self._config.down_reminder_seconds
            )
            if reminder_due:
                self._pending_down_kind = "reminder"

        kind = self._pending_down_kind
        if kind is None or self._notifier is None:
            return

        if kind == "initial":
            delivered = await self._send_notification(
                self._notifier.send_turso_down(self._last_successful_sync)
            )
        else:
            delivered = await self._send_notification(
                self._notifier.send_turso_down_reminder(
                    self._down_since or now, self._last_successful_sync
                )
            )

        if delivered:
            self._pending_down_kind = None
            self._last_down_notification_at = now

    async def _handle_sync_success(self) -> None:
        if self._state == TursoState.DOWN:
            self._pending_recovered_downtime = (
                time.time() - self._down_since if self._down_since is not None else None
            )
            self._recovered_notification_pending = True
            self._state = TursoState.UP
            self._down_since = None
            self._pending_down_kind = None

        if not self._recovered_notification_pending or self._notifier is None:
            return

        delivered = await self._send_notification(
            self._notifier.send_turso_recovered(self._pending_recovered_downtime)
        )
        if delivered:
            self._recovered_notification_pending = False
            self._pending_recovered_downtime = None

    async def _send_notification(self, coro) -> bool:
        try:
            await coro
            return True
        except Exception:
            logger.exception("Failed to send Turso watchdog notification")
            return False

    # -- shutdown ---------------------------------------------------------------------------

    async def aclose(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(self._executor, connection.close)
            except Exception:
                logger.exception("Failed to close the Turso local database connection cleanly")
        await asyncio.to_thread(self._executor.shutdown)
