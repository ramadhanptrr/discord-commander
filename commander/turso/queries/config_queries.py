"""SQL for reading Commander's operational configuration out of the Turso local replica.

migrations/turso_migrations.md originally sketched separate "remote" and "local" query files.
That split does not map onto the actual client library: pyturso's sync client (turso.sync)
refreshes the local replica through connection.pull()/push() -- non-SQL sync primitives, not
queries -- and every SQL statement always executes against the local, already-synced connection.
There is therefore only ever one kind of query here; TursoCacheManager owns the pull()/push()
calls, and this module owns the SQL text it runs afterward.
"""

from __future__ import annotations

MASTER_CONFIGURATIONS_TABLE = "master_configurations"

SELECT_GROUP_BY_IDENTIFIER = (
    f"SELECT attribute_key, attribute_value FROM {MASTER_CONFIGURATIONS_TABLE} WHERE identifier = ?"
)
