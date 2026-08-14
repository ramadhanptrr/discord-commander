from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import turso.sync

from commander.config import TursoConfig
from commander.turso.queries.event_queries import INSERT_EVENT_HISTORY


logger = logging.getLogger("commander.turso.writer")


class EventWriter(Protocol):
    """The subset of ``TursoProdWriter`` a watchdog depends on to persist its own state.

    Kept here (next to the concrete implementation) rather than in each watchdog module, since
    every consumer needs the exact same one-method shape -- see
    ``commander/turso/event_state.py`` for the matching read-side ``EventHistorySource``.
    """

    async def record_event(self, identifier: str, current_state: str) -> bool: ...

# pyturso's turso.sync.connect() has no remote-only mode -- its first argument is always a local
# store, even ":memory:" counts, and there is no variant that talks to remote_url without one
# (confirmed against the installed pyturso==0.7.2 API surface; a true remote-only client would be a
# different package entirely). ":memory:" satisfies that requirement without a file, a directory, or
# a Docker volume: this manager never reads its own local store, only executes and immediately
# push()es, so nothing here needs to survive between writes, let alone between restarts -- unlike
# TursoCacheManager's local.db, which is read on every config lookup and therefore has to be a real
# file.
_LOCAL_REPLICA = ":memory:"


class TursoWriteError(RuntimeError):
    """Raised internally when a statement could not be written through to Turso Cloud.

    Never escapes ``execute()``/``record_event()`` -- both catch it (and anything else) and return
    ``False`` instead, so a Turso Cloud hiccup can never propagate into a caller's tick loop.
    """


class TursoProdWriter:
    """Owns a dedicated embedded-replica connection for pushing rows to Turso Cloud.

    Deliberately separate from ``TursoCacheManager``: that manager keeps a periodically refreshed
    *read* replica of operator-edited configuration, fails closed at startup, and is a hard
    dependency for the rest of the application. This class is the opposite profile -- an
    occasional, best-effort *write* path (e.g. watchdog transition history) that must never block
    application startup or bring the process down if Turso Cloud is briefly unreachable. Keeping
    the two managers separate keeps both profiles easy to reason about instead of overloading one
    manager with contradictory bootstrap/fail semantics. See
    ``migrations/network_watchdog_state_migration.md`` §4-5 for the full reasoning.

    Generic by design: ``execute()`` is the only primitive that talks to the database.
    Table-specific convenience methods (e.g. ``record_event``) are thin wrappers around it, so
    adding a write path for a new table later is a new query constant plus a one-line wrapper
    method here -- not a new connection, executor, or locking scheme.

    Threading model mirrors ``TursoCacheManager`` (see its class docstring for the full reasoning):
    ``pyturso`` declares DB-API ``threadsafety = 1`` -- a connection must never be used from more
    than one thread, and never concurrently. The one synchronized connection this class owns
    therefore lives entirely on its own dedicated single-worker executor (``self._executor``);
    every operation on it (connect, execute, push, close) is submitted there, serialized by
    ``self._write_lock``, and never overlaps with another such operation. This executor is
    intentionally separate from ``TursoCacheManager``'s -- they are unrelated connections (one
    disk-backed and read continuously, one in-memory and write-only), and sharing an executor would
    serialize unrelated read-sync and write-push work against each other for no benefit.
    """

    def __init__(self, config: TursoConfig) -> None:
        self._config = config
        self._connection: turso.sync.ConnectionSync | None = None
        # Dedicated single-worker executor: everything that touches the connection (connect/
        # execute/push/close) is submitted here so it always runs on the same thread and never
        # overlaps with another such operation. See class docstring.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="turso-writer")
        self._write_lock: asyncio.Lock | None = None

    # -- generic write primitive ---------------------------------------------------------

    async def execute(self, sql: str, params: tuple = ()) -> bool:
        """Run one write statement and push it to Turso Cloud. Never raises.

        Returns ``True`` once Turso Cloud has accepted the write, ``False`` on any failure
        (connection, SQL, or network) -- logged here so callers can implement their own retry
        policy (e.g. an edge-triggered watchdog retrying a failed persist on its next tick)
        without needing to know anything about the connection underneath.
        """
        try:
            loop = asyncio.get_running_loop()
            async with self._get_write_lock():
                await loop.run_in_executor(self._executor, self._execute_and_push, sql, params)
            return True
        except Exception:
            logger.exception("Failed to write to Turso Cloud")
            return False

    # -- table-specific convenience wrappers -----------------------------------------------

    async def record_event(self, identifier: str, current_state: str) -> bool:
        """Insert one ``event_history`` row and push it to Turso Cloud. See ``execute()``."""
        return await self.execute(INSERT_EVENT_HISTORY, (identifier, current_state))

    # -- connection lifecycle (dedicated writer thread only) --------------------------------

    def _get_write_lock(self) -> asyncio.Lock:
        # Created lazily so it always binds to the event loop that actually performs writes,
        # matching TursoCacheManager._get_sync_lock's reasoning.
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    def _execute_and_push(self, sql: str, params: tuple) -> None:
        """Runs on the dedicated writer thread. Connects lazily; discards on any failure.

        Lazy connect (as opposed to TursoCacheManager's fail-closed startup bootstrap) is
        deliberate: this manager must never block or fail application startup. A connection that
        failed a previous write is discarded rather than reused -- pyturso gives no documented
        recovery contract for a connection object after a failed push, so the safe default is a
        fresh connection (and therefore a fresh pull/schema baseline) on the next attempt instead
        of risking a wedged connection retried forever.
        """
        try:
            if self._connection is None:
                self._connection = self._connect_blocking()
            self._connection.execute(sql, params)
            self._connection.push()
        except Exception as error:
            self._discard_connection_blocking()
            raise TursoWriteError("Failed to execute and push a Turso write") from error

    def _connect_blocking(self) -> turso.sync.ConnectionSync:
        connection = turso.sync.connect(
            _LOCAL_REPLICA,
            remote_url=self._config.database_url,
            auth_token=self._config.auth_token,
        )
        connection.pull()
        return connection

    def _discard_connection_blocking(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            logger.exception("Failed to close a Turso writer connection after a failed write")

    # -- shutdown ---------------------------------------------------------------------------

    async def aclose(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(self._executor, connection.close)
            except Exception:
                logger.exception("Failed to close the Turso writer connection cleanly")
        await asyncio.to_thread(self._executor.shutdown)
