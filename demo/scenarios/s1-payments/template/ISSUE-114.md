# REQ-114 — Retry payment webhooks under provider rate limiting

**Issue:** #114
**Component:** payments
**Priority:** P2 — customer-visible during sale periods

Scope: payments/webhooks.py

## Problem

Payment provider webhooks fail under rate limiting during sale periods. When the
provider answers `429 Too Many Requests`, `handle_charge_event` gives up on the
first attempt, returns `{"ok": false, ...}`, and the callback is lost. The order
stays in `awaiting_payment` until someone reconciles it by hand.

Last sale weekend this affected 1,842 callbacks across 41 minutes.

## Requirement

Add retry with exponential backoff on HTTP 429.

- Retry only on `429`. Every other status keeps today's behaviour.
- Back off exponentially between attempts.
- Honour `retry_after_s` from the provider response when it is set.
- Give up after a bounded number of attempts and return the same failure shape
  as today.

## Out of scope

Order state transitions, capture, cancellation and cache behaviour are **not**
part of this change. This ticket is the webhook handler only.

## Done when

- A `429` followed by a `200` settles the order.
- A run of `429`s ends in a bounded failure, not an exception.
- `pytest` passes.
