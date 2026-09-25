"""A very small TTL cache.

The clock is injected so tests can move time without sleeping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_TTL_S = 30.0


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    """Keys expire on read. Good enough for order summaries."""

    def __init__(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        return entry.value

    def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        ttl = self._ttl_s if ttl_s is None else ttl_s
        self._entries[key] = _Entry(value, self._clock() + ttl)

    def invalidate(self, key: str) -> bool:
        """Drop `key`. True when something was actually dropped."""
        return self._entries.pop(key, None) is not None

    def __len__(self) -> int:
        return len(self._entries)
