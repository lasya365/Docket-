"""Role resolution.

`entitlement/**` is listed under `sensitive.paths` in Docket's config.json, so a
change here raises the blast-radius signal and, when the change cannot be matched
to a session, fires hard stop HS3.
"""

from __future__ import annotations

ROLE_GRANTS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"tickets:read"}),
    "agent": frozenset({"tickets:read", "tickets:write"}),
    "approver": frozenset({"tickets:read", "tickets:write", "entitlements:approve"}),
    "admin": frozenset({"tickets:read", "tickets:write", "entitlements:approve", "entitlements:write"}),
}


def permissions_for(role: str) -> frozenset[str]:
    """The permissions `role` carries. An unknown role carries none."""
    return ROLE_GRANTS.get(role, frozenset())


def may(role: str, permission: str) -> bool:
    """True when `role` carries `permission`."""
    return permission in permissions_for(role)


def is_privileged(role: str) -> bool:
    """True when `role` can change other people's entitlements."""
    return may(role, "entitlements:write")
