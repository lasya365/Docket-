# storefront-payments

A miniature commerce service. It takes an order, asks the payment provider what
happened to the charge, and moves the order along.

| Path | What it is |
|---|---|
| `payments/webhooks.py` | The endpoint the provider calls back on. |
| `payments/provider.py` | The provider client. The transport is injected; nothing here touches the network. |
| `orders/models.py` | `Order` and `OrderStore`. `mark_paid()` / `mark_captured()` live here. |
| `cache/store.py` | A small TTL cache for storefront order summaries. |
| `tests/` | Real pytest tests, all offline. |

## Running the tests

```bash
python -m pytest -q
```

No dependencies beyond `pytest`.

## Open work

See [`ISSUE-114.md`](ISSUE-114.md).
