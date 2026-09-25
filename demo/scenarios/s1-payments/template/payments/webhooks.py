"""The endpoint the payment provider calls back on.

The provider POSTs a small event; we ask it for the authoritative charge state
and move the order accordingly. We never trust the status inside the event
itself, because the webhook is public.

Every delivery carries a unique `id`. The provider redelivers a callback it did
not get a 2xx for, so the same `id` can arrive more than once.

Known limitation (REQ-114): the provider rate-limits hard during sale periods
and `fetch_charge` is issued exactly once, so a 429 drops the callback on the
floor and the order sits in awaiting_payment until someone notices.
"""

from __future__ import annotations

import logging

from cache.store import TTLCache
from orders.models import Order, OrderNotFound, OrderStore
from payments.provider import CAPTURED, PaymentProvider

log = logging.getLogger(__name__)

#: Event types this handler acts on. Anything else is logged and ignored.
HANDLED_EVENTS = frozenset({"charge.succeeded", "charge.updated"})


def order_cache_key(order_id: str) -> str:
    """The key the storefront caches an order summary under."""
    return f"order:{order_id}"


def handle_charge_event(
    event: dict,
    *,
    provider: PaymentProvider,
    orders: OrderStore,
    cache: TTLCache,
) -> dict:
    """Process one provider callback. Never raises on a malformed event."""
    event_type = event.get("type")
    if event_type not in HANDLED_EVENTS:
        log.debug("ignoring webhook event type %r", event_type)
        return {"ok": True, "ignored": event_type}

    data = event.get("data") or {}
    order_id = data.get("order_id")
    charge_id = data.get("charge_id")
    if not order_id or not charge_id:
        log.debug("webhook event is missing order_id/charge_id: %r", data)
        return {"ok": False, "reason": "malformed event"}

    try:
        order = orders.get(order_id)
    except OrderNotFound:
        log.debug("webhook names an unknown order %r", order_id)
        return {"ok": False, "reason": "unknown order"}

    response = provider.fetch_charge(charge_id)
    if not response.ok:
        log.debug(
            "provider answered %s for charge %s", response.status_code, charge_id
        )
        return {"ok": False, "reason": f"provider returned {response.status_code}"}

    if response.charge_status == CAPTURED:
        _settle(order, charge_id, orders, cache)

    return {"ok": True, "order_status": order.status}


def _settle(
    order: Order, charge_id: str, orders: OrderStore, cache: TTLCache
) -> None:
    """Record the confirmed charge and drop the stale storefront summary."""
    order.charge_id = charge_id
    order.mark_paid()
    orders.save(order)
    cache.invalidate(order_cache_key(order.id))
