"""Role -> permission resolution.

This is the authorization source of truth. `api/middleware.py` calls
`resolve_permissions` on every request, which is why REQ-118 exists: the role
store is a network hop and it costs about 40ms each time.
"""

from __future__ import annotations

from typing import Iterable

CONTENT_READ = "content:read"
CONTENT_WRITE = "content:write"
CONTENT_PUBLISH = "content:publish"
USERS_READ = "users:read"
USERS_WRITE = "users:write"
ROLES_WRITE = "roles:write"
SETTINGS_WRITE = "settings:write"

#: Everything a role could possibly carry. Only `owner` is granted all of it.
ALL_PERMISSIONS = frozenset(
    {
        CONTENT_READ,
        CONTENT_WRITE,
        CONTENT_PUBLISH,
        USERS_READ,
        USERS_WRITE,
        ROLES_WRITE,
        SETTINGS_WRITE,
    }
)

ROLE_GRANTS: dict[str, frozenset[str]] = {
    "owner": ALL_PERMISSIONS,
    "editor": frozenset({CONTENT_READ, CONTENT_WRITE, CONTENT_PUBLISH, USERS_READ}),
    "author": frozenset({CONTENT_READ, CONTENT_WRITE}),
    "viewer": frozenset({CONTENT_READ}),
}


class RoleLookupError(RuntimeError):
    """The role store could not be reached, or answered with garbage."""


class RoleStore:
    """Stands in for the roles table behind a network hop.

    `latency_ms` is descriptive only -- nothing sleeps, so the tests stay fast.
    """

    def __init__(
        self,
        grants: dict[str, Iterable[str]] | None = None,
        *,
        latency_ms: float = 40.0,
        failing: Iterable[str] = (),
    ) -> None:
        source = ROLE_GRANTS if grants is None else grants
        self._grants = {k: frozenset(v) for k, v in source.items()}
        self.latency_ms = latency_ms
        self._failing = set(failing)
        self.calls = 0

    def fetch(self, role_id: str) -> frozenset[str]:
        """The permissions stored for `role_id`. Unknown roles carry none."""
        self.calls += 1
        if role_id in self._failing:
            raise RoleLookupError(f"role store unavailable for {role_id!r}")
        return self._grants.get(role_id, frozenset())


def resolve_permissions(role_id: str, store: RoleStore) -> frozenset[str]:
    """Resolve one role. Costs a round trip every single time -- see REQ-118."""
    if not role_id:
        return frozenset()
    return store.fetch(role_id)


def has_permission(permissions: Iterable[str], required: str) -> bool:
    """True when `required` is present."""
    return required in set(permissions)
