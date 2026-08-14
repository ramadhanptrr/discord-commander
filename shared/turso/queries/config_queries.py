"""SQL for reading Commander's operational configuration out of the Turso local replica.

pyturso's sync client (turso.sync) refreshes the local replica through connection.pull()/push()
-- non-SQL sync primitives, not queries -- and every SQL statement here always executes against a
local, already-synced connection. There is therefore only ever one kind of query in this module;
TursoCacheManager owns the pull()/push() calls, and this module owns the SQL text it runs
afterward (see machine_lore/ARCHITECTURE.md for the overall read/sync split).
"""

from __future__ import annotations

MASTER_CONFIGURATIONS_TABLE = "master_configurations"

SELECT_GROUP_BY_IDENTIFIER = (
    f"SELECT attribute_key, attribute_value FROM {MASTER_CONFIGURATIONS_TABLE} WHERE identifier = ?"
)
