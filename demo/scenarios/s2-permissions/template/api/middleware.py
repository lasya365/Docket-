"""The authorization middleware every API request passes through."""

from __future__ import annotations

from dataclasses import dataclass, field

from auth.permissions import RoleLookupError, RoleStore, has_permission, resolve_permissions

OK = 200
UNAUTHENTICATED = 401
FORBIDDEN = 403
UNAVAILABLE = 503


@dataclass(frozen=True)
class Request:
    """Just enough of a request for the permission check."""

    path: str
    method: str = "GET"
    role_id: str | None = None
    tenant_id: str = "t-1"


@dataclass(frozen=True)
class Response:
    status: int
    body: dict = field(default_factory=dict)


#: Which permission each route family requires.
ROUTE_PERMISSIONS: dict[str, str] = {
    "/api/content": "content:read",
    "/api/content/publish": "content:publish",
    "/api/users": "users:read",
    "/api/roles": "roles:write",
    "/api/settings": "settings:write",
}


def required_permission(path: str) -> str | None:
    """The permission `path` needs, longest matching prefix first."""
    for route in sorted(ROUTE_PERMISSIONS, key=len, reverse=True):
        if path == route or path.startswith(route + "/"):
            return ROUTE_PERMISSIONS[route]
    return None


def authorize(request: Request, store: RoleStore) -> Response:
    """Allow or refuse one request. Refusing is the safe answer."""
    needed = required_permission(request.path)
    if needed is None:
        return Response(OK, {"route": request.path, "public": True})
    if not request.role_id:
        return Response(UNAUTHENTICATED, {"reason": "no role on the request"})

    try:
        permissions = resolve_permissions(request.role_id, store)
    except RoleLookupError as exc:
        return Response(UNAVAILABLE, {"reason": str(exc)})

    if not has_permission(permissions, needed):
        return Response(FORBIDDEN, {"reason": f"{request.role_id} lacks {needed}"})
    return Response(OK, {"route": request.path, "role": request.role_id})
