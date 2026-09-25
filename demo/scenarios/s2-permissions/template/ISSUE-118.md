# REQ-118 — Cache resolved permissions per role

**Issue:** #118
**Component:** auth
**Priority:** P2 — latency on every authenticated request

Scope: auth/cache.py

## Problem

Permission resolution adds 40ms to every request. `api/middleware.py` calls
`resolve_permissions()` on every authenticated route, and every call is a round
trip to the role store. Roles change a few times a week; we are paying for a
fresh lookup thousands of times a second.

## Requirement

Cache resolved permissions per role for 60 seconds.

- Key the cache by role id.
- 60 second TTL. `auth/cache.py` already defines `DEFAULT_TTL_S`.
- A hit must return exactly what the store would have returned.
- Provide a way to drop a role's entry when its grants change.

## Out of scope

`auth/permissions.py` and `api/middleware.py` keep their current behaviour.
This ticket is the cache only.

## Done when

- A second resolve of the same role inside 60 seconds does not hit the store.
- After 60 seconds the store is consulted again.
- `pytest` passes.
