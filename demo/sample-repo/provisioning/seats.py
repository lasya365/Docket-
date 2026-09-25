"""Seat accounting: who may still be provisioned on a plan."""

from __future__ import annotations

PLAN_SEATS: dict[str, int] = {"free": 1, "team": 25, "enterprise": 500}


def seats_for(plan: str) -> int:
    """How many seats `plan` includes. An unknown plan gets the free allowance."""
    return PLAN_SEATS.get(plan, PLAN_SEATS["free"])


def seats_remaining(plan: str, used: int) -> int:
    """Seats left on `plan`. Never negative: an over-provisioned account has 0."""
    if used < 0:
        raise ValueError("used must not be negative")
    return max(seats_for(plan) - used, 0)


def can_provision(plan: str, used: int, requested: int = 1) -> bool:
    """True when `requested` more seats fit inside the plan's allowance."""
    if requested < 1:
        raise ValueError("requested must be at least 1")
    return seats_remaining(plan, used) >= requested
