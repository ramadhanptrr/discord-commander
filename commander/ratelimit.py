from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class RateLimiter:
    """In-memory per-user limits for power actions in the control room."""

    _LIMITS: dict[str, tuple[int, int]] = {
        "wake": (2, 120),
        "shutdown": (2, 120),
    }

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        self._lock = asyncio.Lock()

    async def is_allowed(self, user_id: int, operation: str) -> tuple[bool, int]:
        max_calls, window_seconds = self._LIMITS[operation]
        now = time.monotonic()
        window_start = now - window_seconds

        async with self._lock:
            calls = [timestamp for timestamp in self._calls[user_id][operation] if timestamp > window_start]
            self._calls[user_id][operation] = calls
            if len(calls) >= max_calls:
                retry_after = int(calls[0] + window_seconds - now) + 1
                return False, max(retry_after, 1)

            calls.append(now)
            return True, 0
