import pytest

from cache.store import TTLCache
from orders.models import Order, OrderNotFound, OrderStore


def test_order_starts_awaiting_payment():
    order = Order(id="o-1", total_cents=4200)
    assert order.status == "awaiting_payment"
    assert order.is_settled is False


def test_mark_paid_then_captured():
    order = Order(id="o-1", total_cents=4200)
    order.mark_paid()
    assert order.status == "paid"
    order.mark_captured()
    assert order.status == "captured"
    assert order.history == ["paid", "captured"]
    assert order.is_settled is True


def test_cancelled_order_cannot_settle():
    order = Order(id="o-2", total_cents=100)
    order.cancel()
    with pytest.raises(ValueError):
        order.mark_paid()
    with pytest.raises(ValueError):
        order.mark_captured()


def test_captured_order_cannot_be_cancelled():
    order = Order(id="o-3", total_cents=100)
    order.mark_captured()
    with pytest.raises(ValueError):
        order.cancel()


def test_store_get_and_save():
    store = OrderStore([Order(id="o-1", total_cents=1)])
    assert store.get("o-1").total_cents == 1
    store.save(Order(id="o-2", total_cents=2))
    assert store.ids() == ["o-1", "o-2"]
    with pytest.raises(OrderNotFound):
        store.get("nope")


def test_cache_expires_and_invalidates():
    now = [0.0]
    cache = TTLCache(ttl_s=10.0, clock=lambda: now[0])
    cache.set("order:o-1", {"total": 1})
    assert cache.get("order:o-1") == {"total": 1}
    assert cache.invalidate("order:o-1") is True
    assert cache.get("order:o-1") is None

    cache.set("order:o-2", {"total": 2})
    now[0] = 11.0
    assert cache.get("order:o-2") is None
    assert len(cache) == 0
