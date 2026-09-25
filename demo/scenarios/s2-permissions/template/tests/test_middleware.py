import pytest

from api.middleware import (
    FORBIDDEN,
    OK,
    UNAUTHENTICATED,
    UNAVAILABLE,
    Request,
    authorize,
    required_permission,
)
from auth.permissions import RoleStore


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/api/content", "content:read"),
        ("/api/content/publish", "content:publish"),
        ("/api/content/42", "content:read"),
        ("/api/roles", "roles:write"),
        ("/healthz", None),
    ],
)
def test_route_permissions(path, expected):
    assert required_permission(path) == expected


def test_owner_is_allowed_everywhere():
    store = RoleStore()
    for path in ("/api/settings", "/api/roles", "/api/content/publish"):
        assert authorize(Request(path, role_id="owner"), store).status == OK


def test_viewer_is_refused_a_write():
    store = RoleStore()
    response = authorize(Request("/api/content/publish", role_id="viewer"), store)
    assert response.status == FORBIDDEN
    assert "content:publish" in response.body["reason"]


def test_public_route_needs_no_role():
    assert authorize(Request("/healthz"), RoleStore()).status == OK


def test_missing_role_is_unauthenticated():
    assert authorize(Request("/api/content"), RoleStore()).status == UNAUTHENTICATED


def test_a_broken_store_refuses_rather_than_allows():
    store = RoleStore(failing={"editor"})
    response = authorize(Request("/api/content", role_id="editor"), store)
    assert response.status == UNAVAILABLE
