import pytest

from auth.cache import TTLCache, role_cache_key
from auth.permissions import (
    ALL_PERMISSIONS,
    RoleLookupError,
    RoleStore,
    has_permission,
    resolve_permissions,
)


def test_known_roles_resolve():
    store = RoleStore()
    assert resolve_permissions("viewer", store) == frozenset({"content:read"})
    assert resolve_permissions("owner", store) == ALL_PERMISSIONS
    assert "content:publish" in resolve_permissions("editor", store)


def test_unknown_and_empty_roles_carry_nothing():
    store = RoleStore()
    assert resolve_permissions("intern", store) == frozenset()
    assert resolve_permissions("", store) == frozenset()
    assert store.calls == 1


def test_author_cannot_publish():
    store = RoleStore()
    perms = resolve_permissions("author", store)
    assert has_permission(perms, "content:write") is True
    assert has_permission(perms, "content:publish") is False


def test_a_broken_store_raises():
    store = RoleStore(failing={"editor"})
    with pytest.raises(RoleLookupError):
        resolve_permissions("editor", store)


def test_every_resolve_costs_a_round_trip():
    """REQ-118: this is the 40ms, once per request."""
    store = RoleStore()
    for _ in range(5):
        resolve_permissions("editor", store)
    assert store.calls == 5


def test_cache_expiry_and_invalidation():
    now = [0.0]
    cache = TTLCache(ttl_s=60.0, clock=lambda: now[0])
    key = role_cache_key("editor")
    cache.set(key, frozenset({"content:read"}))
    assert cache.get(key) == frozenset({"content:read"})
    now[0] = 61.0
    assert cache.get(key) is None

    cache.set(key, frozenset())
    assert cache.invalidate(key) is True
    assert len(cache) == 0
