"""Client for the payment provider's charge API.

There is no network here on purpose. The transport is injected, so the tests
drive it with canned responses and the demo never leaves the machine.

`fetch_charge` makes exactly one call and hands the response back untouched.
Deciding what to do about a 429 is the caller's job -- see REQ-114.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

OK = 200
RATE_LIMITED = 429
SERVER_ERROR = 500

#: Charge states the provider reports. ``None`` means "no status yet".
CAPTURED = "captured"
PENDING = "pending"
FAILED = "failed"


@dataclass(frozen=True)
class ProviderResponse:
    """One HTTP answer from the provider."""

    status_code: int
    charge_status: str | None = None
    retry_after_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status_code == OK

    @property
    def rate_limited(self) -> bool:
        return self.status_code == RATE_LIMITED


class Transport(Protocol):
    """Whatever actually performs the request."""

    def get(self, path: str) -> ProviderResponse:  # pragma: no cover - protocol
        ...


class ProviderError(RuntimeError):
    """The request could not be formed at all."""


class PaymentProvider:
    """Reads charge state from the provider."""

    def __init__(self, transport: Transport, merchant_id: str = "acme") -> None:
        self._transport = transport
        self._merchant_id = merchant_id

    def fetch_charge(self, charge_id: str) -> ProviderResponse:
        """The charge as the provider currently sees it. One call, no retry."""
        if not charge_id:
            raise ProviderError("charge_id is required")
        path = f"/v1/merchants/{self._merchant_id}/charges/{charge_id}"
        return self._transport.get(path)
