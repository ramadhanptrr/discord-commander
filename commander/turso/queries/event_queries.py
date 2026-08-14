"""SQL for Commander's ``event_history`` table (watchdog transition persistence).

Unlike ``config_queries.py`` (local-only reads against the periodically synced config replica),
``INSERT_EVENT_HISTORY`` runs against ``TursoProdWriter``'s own embedded-replica connection and is
pushed straight to Turso Cloud after every write (see ``commander/turso/writer.py``).
``SELECT_LATEST_EVENT_BY_IDENTIFIER`` is a local-only read, intended to run against
``TursoCacheManager``'s periodically synced replica the same way ``config_queries.py`` reads do.
"""

from __future__ import annotations

EVENT_HISTORY_TABLE = "event_history"

INSERT_EVENT_HISTORY = (
    f"INSERT INTO {EVENT_HISTORY_TABLE} (identifier, current_state) VALUES (?, ?)"
)

# id is monotonic (SQLite INTEGER PRIMARY KEY), so ordering by it is equivalent to insertion order
# without depending on created_at's whole-second string precision. Pair with the index:
#   CREATE INDEX idx_event_history_identifier_id ON event_history (identifier, id DESC);
SELECT_LATEST_EVENT_BY_IDENTIFIER = (
    f"SELECT current_state, created_at FROM {EVENT_HISTORY_TABLE} "
    "WHERE identifier = ? ORDER BY id DESC LIMIT 1"
)
