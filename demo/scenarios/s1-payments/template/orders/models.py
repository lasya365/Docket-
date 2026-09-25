"""The order aggregate and the in-memory store the checkout service uses."""

from __future__ import annotations

from dataclasses import dataclass, field

AWAITING_PAYMENT = "awaiting_payment"
PAID = "paid"
CAPTURED = "captured"
CANCELLED = "cancelled"


class OrderNotFound(KeyError):
    """No order with that id."""


@dataclass
class Order:
    """One customer order.

    ``status`` moves awaiting_payment -> paid -> captured. ``paid`` means the
    provider confirmed the charge; ``captured`` means the money is settled and
    the order is released to fulfilment, which is the point of no return.
    """

    id: str
    total_cents: int
    charge_id: str | None = None
    status: str = AWAITING_PAYMENT
    history: list[str] = field(default_factory=list)

    def mark_paid(self) -> None:
        """The provider confirmed the charge."""
        if self.status == CANCELLED:
            raise ValueError(f"order {self.id} is cancelled")
        self.status = PAID
        self.history.append(PAID)

    def mark_captured(self) -> None:
        """The money is settled. Fulfilment may ship."""
        if self.status == CANCELLED:
            raise ValueError(f"order {self.id} is cancelled")
        self.status = CAPTURED
        self.history.append(CAPTURED)

    def cancel(self) -> None:
        """Give up on the order. Only possible before capture."""
        if self.status == CAPTURED:
            raise ValueError(f"order {self.id} is already captured")
        self.status = CANCELLED
        self.history.append(CANCELLED)

    @property
    def is_settled(self) -> bool:
        return self.status in (PAID, CAPTURED)


class OrderStore:
    """Orders by id. A database sits here in the real service."""

    def __init__(self, orders: list[Order] | None = None) -> None:
        self._orders: dict[str, Order] = {o.id: o for o in orders or []}

    def get(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise OrderNotFound(order_id) from None

    def save(self, order: Order) -> None:
        self._orders[order.id] = order

    def ids(self) -> list[str]:
        return sorted(self._orders)
