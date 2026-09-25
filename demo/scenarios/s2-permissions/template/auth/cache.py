"""Caches for the authorization layer.

Today this holds one thing: the role *name* lookup, which is stable and cheap
to cache. Resolved permissions are not cached yet -- that is REQ-118.

The clock is injected so tests can move time without sleeping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

#: How long a cached authorization answer may be trusted.
DEFAULT_TTL_S = 60.0


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    """Keys expire on read."""

    def __init__(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = ttl_s
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
        ttl = self.ttl_s if ttl_s is None else ttl_s
        self._entries[key] = _Entry(value, self._clock() + ttl)

    def invalidate(self, key: str) -> bool:
        return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def role_cache_key(role_id: str) -> str:
    """The key an authorization answer for `role_id` is stored under."""
    return f"role:{role_id}"
