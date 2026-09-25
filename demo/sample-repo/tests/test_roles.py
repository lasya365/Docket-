from entitlement.roles import is_privileged, may, permissions_for


def test_permissions_for_known_role():
    assert "tickets:write" in permissions_for("agent")
    assert permissions_for("viewer") == frozenset({"tickets:read"})


def test_unknown_role_carries_nothing():
    assert permissions_for("intern") == frozenset()
    assert may("intern", "tickets:read") is False


def test_may():
    assert may("approver", "entitlements:approve") is True
    assert may("approver", "entitlements:write") is False


def test_is_privileged():
    assert is_privileged("admin") is True
    assert is_privileged("approver") is False
    assert is_privileged("viewer") is False
