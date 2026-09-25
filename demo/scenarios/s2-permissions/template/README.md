# cms-core

A miniature headless CMS. Content lives behind an API, and every request goes
through the same authorization check.

| Path | What it is |
|---|---|
| `auth/permissions.py` | Role to permission resolution. The authorization source of truth. |
| `auth/cache.py` | TTL caches for the authorization layer. |
| `api/middleware.py` | `authorize()` — runs on every request, allows or refuses. |
| `tests/` | Real pytest tests, all offline. |

## Running the tests

```bash
python -m pytest -q
```

No dependencies beyond `pytest`.

## Open work

See [`ISSUE-118.md`](ISSUE-118.md).
