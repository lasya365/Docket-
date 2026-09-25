import pytest

from provisioning.seats import can_provision, seats_for, seats_remaining


def test_seats_for_known_and_unknown_plans():
    assert seats_for("team") == 25
    assert seats_for("enterprise") == 500
    assert seats_for("no-such-plan") == seats_for("free")


def test_seats_remaining_never_goes_negative():
    assert seats_remaining("team", 20) == 5
    assert seats_remaining("team", 30) == 0


def test_can_provision():
    assert can_provision("team", 24) is True
    assert can_provision("team", 25) is False
    assert can_provision("enterprise", 400, requested=50) is True
    assert can_provision("free", 0, requested=2) is False


@pytest.mark.parametrize("bad", [-1, -10])
def test_negative_usage_is_rejected(bad):
    with pytest.raises(ValueError):
        seats_remaining("team", bad)


def test_requested_must_be_at_least_one():
    with pytest.raises(ValueError):
        can_provision("team", 0, requested=0)
