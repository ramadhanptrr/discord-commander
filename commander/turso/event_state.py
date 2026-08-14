"""Shared contracts/helpers for watchdogs that restore and persist state through ``event_history``.

Used by both ``commander.netmonitor.NetworkWatchdog`` and ``commander.nasmonitor.NasUptimeWatchdog``
-- see ``migrations/network_watchdog_state_migration.md`` and
``migrations/nas_uptime_watchdog_state_migration.md``. Kept separate from
``commander/turso/cache_manager.py`` and ``commander/turso/writer.py`` (the concrete
implementations) so a third watchdog adopting this pattern only needs this module plus its own
``identifier``/value vocabulary, not a new Protocol shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class EventHistorySource(Protocol):
    """The local read a watchdog needs to restore its own state from ``event_history``."""

    def read_latest_event(self, identifier: str) -> dict[str, str] | None: ...


def parse_event_timestamp(value: str) -> float | None:
    """Parse a SQLite ``CURRENT_TIMESTAMP`` string (UTC, whole-second) into a Unix epoch float.

    Returns ``None`` on anything unparsable rather than raising, since a malformed/unexpected value
    should make the caller fall back to "no restored state" instead of crashing restore.
    """
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()
