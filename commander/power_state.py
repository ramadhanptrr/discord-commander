from __future__ import annotations

import asyncio


class PowerOperationState:
    """One process-local lock prevents wake and shutdown from overlapping."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active_operation: str | None = None

    async def acquire(self, operation: str) -> bool:
        async with self._lock:
            if self._active_operation is not None:
                return False
            self._active_operation = operation
            return True

    async def release(self, operation: str) -> None:
        async with self._lock:
            if self._active_operation == operation:
                self._active_operation = None

    @property
    def active_operation(self) -> str | None:
        return self._active_operation
