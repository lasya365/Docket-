import pytest

from cache.store import TTLCache
from orders.models import Order, OrderStore
from payments.provider import OK, RATE_LIMITED, PaymentProvider, ProviderResponse
from payments.webhooks import handle_charge_event, order_cache_key


class FakeTransport:
    """Answers with a scripted list of responses, oldest first."""

    def __init__(self, *responses: ProviderResponse) -> None:
        self._responses = list(responses)
        self.paths: list[str] = []

    def get(self, path: str) -> ProviderResponse:
        self.paths.append(path)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


@pytest.fixture()
def world():
    orders = OrderStore([Order(id="o-1", total_cents=4200)])
    cache = TTLCache()
    cache.set(order_cache_key("o-1"), {"status": "awaiting_payment"})
    return orders, cache


def event(order_id="o-1", charge_id="ch-9", type="charge.succeeded", id="evt-1"):
    return {
        "id": id,
        "type": type,
        "data": {"order_id": order_id, "charge_id": charge_id},
    }


def run(world, *responses, ev=None):
    orders, cache = world
    transport = FakeTransport(*responses)
    result = handle_charge_event(
        ev or event(),
        provider=PaymentProvider(transport),
        orders=orders,
        cache=cache,
    )
    return result, transport


def test_captured_charge_marks_the_order_paid(world):
    orders, cache = world
    result, transport = run(world, ProviderResponse(OK, charge_status="captured"))
    assert result == {"ok": True, "order_status": "paid"}
    assert orders.get("o-1").charge_id == "ch-9"
    assert cache.get(order_cache_key("o-1")) is None
    assert transport.paths == ["/v1/merchants/acme/charges/ch-9"]


def test_pending_charge_leaves_the_order_alone(world):
    _, cache = world
    result, _ = run(world, ProviderResponse(OK, charge_status="pending"))
    assert result == {"ok": True, "order_status": "awaiting_payment"}
    assert cache.get(order_cache_key("o-1")) == {"status": "awaiting_payment"}


def test_rate_limited_callback_is_dropped(world):
    """REQ-114: today a single 429 loses the callback entirely."""
    orders, _ = world
    result, _ = run(world, ProviderResponse(RATE_LIMITED, retry_after_s=0.2))
    assert result == {"ok": False, "reason": "provider returned 429"}
    assert orders.get("o-1").status == "awaiting_payment"


@pytest.mark.parametrize(
    "ev, expected",
    [
        (event(type="invoice.paid"), {"ok": True, "ignored": "invoice.paid"}),
        ({"id": "evt-2", "type": "charge.succeeded", "data": {}}, {"ok": False, "reason": "malformed event"}),
        (event(order_id="o-404"), {"ok": False, "reason": "unknown order"}),
    ],
)
def test_events_that_do_nothing(world, ev, expected):
    result, _ = run(world, ProviderResponse(OK), ev=ev)
    assert result == expected
