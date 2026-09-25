# Demo scenarios S1 and S2 — the two code changes

Two small, real repositories staged so that a Claude Code session in each one
produces the change the demo document describes, in under a minute per prompt.

```
demo/scenarios/
  README.md                  this file
  s1-payments/
    PROMPTS.md               the two prompts (NOT copied into the repo, on purpose)
    setup.sh                 makes a fresh git repo from template/
    template/                the repo: payments webhook retry
  s2-permissions/
    PROMPTS.md
    setup.sh
    template/                the repo: permission cache, fail-open
```

`template/` is the repository. Everything beside it is staging material and
never lands in the repo the agent sees.

`s3-model-bump/` and `s4-knowledge/` are the two non-code scenarios and are
owned by a different part of the build; this file covers S1 and S2 only.

---

## 1. Why the repo has to be small

Measured with claude-sonnet-5:

| Repo | Prompt | Time |
|---|---|---|
| Saleor — 848,648 lines of Python, 4,333 files | unscoped `Implement REQ-114.` | **790 s (13.2 min)**, 4 files changed |
| A purpose-built small repo | scoped, naming the target file | **48 s** |
| The same small repo, second prompt | — | 29–171 s, depending on how much the model argues |

A synthetic 2,602-file repo came in at about **90 s**, so raw file count is not
the cause. Saleor is slow because it has **274 files with "webhook" in the
name** and 848k lines to search before the first edit.

So both levers matter, in this order:

1. **Keep the repo tiny.** S1 is 414 lines of Python across 11 files; S2 is 319
   across 10. The agent can read the whole thing and still be editing inside
   fifteen seconds.
2. **Name the target file in the prompt.** `PROMPTS.md` in each scenario gives
   the scoped wording. Use it. Naming `payments/webhooks.py` removes the search
   phase, and it is truthful — the ticket's `Scope:` line says the same thing.

The demo document asks for exactly this: a repo "small enough to clone quickly
and for Claude Code to navigate without a long context warm-up."

Nothing about a small repo weakens the finding. Docket scores *ratios* — AI
lines outside the declared scope over AI lines total, seconds per added line,
covered AI lines over executable AI lines. A 60-line diff and an 800-line diff
land in the same band when the proportions match.

---

## 1a. The model refuses the document's trick prompts — and that is fine

The demo document scripts prompt 2 as bait: the agent is supposed to willingly
write `order.mark_captured()` (S1) or `return ALL_PERMISSIONS` (S2). **Measured
twice on a small payments repo, claude-sonnet-5 declined both times.** Verbatim,
it added a comment saying it had deliberately not faked a confirmation. Pushed
harder, it pushed back: *"safer options that don't fake a payment
confirmation… Want me to implement one of those instead?"*

A demo scripted on "the agent writes the dangerous line willingly" fails on
stage. **This costs nothing**, because it is not what Docket measures.

`unattributed` counts agent-written lines that land **outside the path the
ticket's `Scope:` line declared**. It is `pathspec` arithmetic — formula A in
RFC 9, with `tests/**` and `**/*.md` always in scope. It never asks whether the
code is dangerous, and it never needs the model tricked.

So both scenarios now use a **plausible, legitimate-sounding request that the
ticket never asked for**, aimed at files outside the declared scope. The agent
implements it without argument, because there is nothing wrong with it, and the
diff still shows three colours: human, AI traced to the requirement, AI traced
to nothing.

The line the presenter reads aloud at 1:05 becomes:

> The ticket said webhook handler only. These forty lines are in the order and
> cache modules, and no requirement asked for them.

The exact prompts, and which files each one should touch, are in each
scenario's `PROMPTS.md`.
---

## 2. Running a scenario end to end

Same six steps for both. Substitute `s2-permissions` / `REQ-118` for S2.

### 2.1 Create the repo

```bash
cd demo/scenarios/s1-payments
./setup.sh ~/docket-demo/s1-payments --with-agent-setup
```

`setup.sh` copies `template/`, makes the baseline commit on `main`, creates the
feature branch, and runs `pytest` to prove the baseline is green. With
`--with-agent-setup` it also drops in `agent-setup/.claude`, `docket-env.sh`
and the `prepare-commit-msg` git hook, so the session is joined to the change.

**It never runs `claude`.** You drive the agent session yourself; that is the
only way the telemetry is real.

### 2.2 Start Docket

From the Docket repo, in its own terminal:

```bash
make serve          # http://localhost:8000
```

### 2.3 Point the session at Docket

In the scenario repo:

```bash
$EDITOR docket-env.sh     # DOCKET_INGEST_TOKEN must equal the one in Docket's .env
source ./docket-env.sh    # BEFORE claude — it reads these once, at startup
claude
```

Check <http://localhost:8000/sessions> after the first edit: the session should
be listed with `events` and `edits` above zero. If it is not, stop and fix it —
an unattributed PR is a different demo.

### 2.4 Turn 1, then commit

Paste turn 1 from `PROMPTS.md` — the scoped wording that names the target file.
Let it finish, then:

```bash
git add -A && git commit -m "REQ-114: retry charge lookups on 429 with exponential backoff"
```

### 2.5 Turn 2 (and optionally turn 3), then commit

Turn 2 is the plausible request the ticket never made. Paste it, let it finish,
check `git diff --stat` shows lines outside the declared scope, then commit:

```bash
git add -A && git commit -m "Guard against redelivered webhooks"
```

**One commit per turn.** The turns are the story: turn 1 traces to REQ-114,
turn 2 traces to nothing. A single squashed commit still works — attribution is
per-turn, not per-commit — but separate commits make it legible on screen.

S1 has an optional turn 3 that takes `unattributed` from ~25 to ~42, the
document's number. See `s1-payments/PROMPTS.md`.

### 2.6 Score it

Open the PR, then run the gate against it:

```bash
curl -X POST localhost:8000/run/214 -H "Authorization: Bearer $DOCKET_API_TOKEN"
```

`.docket.json` at the repo root carries the staged PR metadata — issue
number and body (with the `Scope:` line), the review timings, and the simulated
per-line coverage. The loader for it is owned by another part of the build; the
file's shape is:

```json
{"pr_number": int, "title": str, "repo": str, "provenance": "simulated",
 "issue": {"number","title","body","url"},
 "reviews": [{"reviewer","state","ask_to_approve_s"}],
 "review_comments": [{"reviewer","path","line","body"}],
 "coverage": {"present","ci_status","source","provenance","files":{path:{"executed":[],"missing":[]}}}}
```

---

## 3. What each signal should come out as

Computed from `config.json` as it ships (`gate.risk_bands`: low < 25,
medium < 50, high < 75, then very high). Signal formulas are RFC section 9.

### S1 — payments webhook retry (PR 214, REQ-114)

| Signal | Expected | Where it comes from |
|---|---|---|
| **Unattributed** | **~25 after two turns, ~42 after three** | `Scope: payments/webhooks.py`. Turn 2's idempotency guard lands in `cache/store.py`; the optional turn 3 lands in `orders/models.py`. Score is `100 × out-of-scope AI lines / AI lines`. `ISSUE-114.md` states in as many words that order state, capture and cache behaviour are out of scope, so the evidence quotes the ticket back. |
| **Review depth** | **high, ~78–91** | `0.7 × speed + 0.3 × engagement`. Two approvals at 25 s and 22 s against a ~60-line diff, where `seconds_per_line = 2.0` expects 120 s. Zero comments pins engagement at 100, which alone floors the signal at 30. |
| **Untested** | **high, ~68** | `100 × (1 − AI-line coverage)`. The staged coverage map covers the change region at 31%, matching the document. |
| **Blast radius** | **low–medium, 10–30** | **Read section 4 before quoting a number.** `payments/` is not in `sensitive.paths`, so the path term is 0. What fires is session posture: `mostly_auto_approved` (+10) and, with bypass permissions, `bypass_mode` (+20). |

Expected verdict: **HOLD** on the weighted score, no hard stop. That is the
right shape for the opener — every conventional check is green and the change
is held on evidence about how it was made.

### S2 — permission cache (PR 218, REQ-118)

| Signal | Expected | Where it comes from |
|---|---|---|
| **Unattributed** | **~15–25, smaller than S1** | `Scope: auth/cache.py`. Only the stats endpoint in `api/middleware.py` counts — the negative cache lands *inside* the declared scope and scores zero. Smaller ratio, worse blast radius: that is the point. |
| **Review depth** | **LOW / green, ~0–10** | 1320 s (22 min) against a ~50-line diff is well past the 100 s the formula expects, so speed scores 0. Three comments on two of three changed files drops engagement to ~33. This review was genuinely careful and Docket says so. |
| **Untested** | **100 → hard stop HS1** | Coverage on the new branch is 0%, so the signal maxes. `gate.hard_stops.HS1` fires at ≥ 90 on any signal. |
| **Blast radius** | **medium–high, 35–55** | `auth/cache.py` and `auth/permissions.py` both match `**/auth/**` in `sensitive.paths`: +25 (one pattern, capped at 50), plus the same session posture points as S1. |

Expected verdict: **HOLD**, hard stop HS1.

**The comparison is the demo.** S2 is smaller, better reviewed, has *fewer*
unattributed lines, and still scores higher. Say it out loud at 1:30: *small
and well reviewed is not the same as safe.*

> **Where unattributed lines must land.** The signal is pure path arithmetic
> against the `Scope:` line, so a turn that only grows the scope file moves
> nothing. S1's scope is `payments/webhooks.py`, so `orders/`, `cache/` and
> even `payments/provider.py` are out. S2's scope is `auth/cache.py`, so
> `api/` and `auth/permissions.py` are out. `tests/**` and `**/*.md` are always
> in scope and never count. Each `PROMPTS.md` has the table.

---

## 4. Blast radius: what is honest here

`config.json` was not edited for these scenarios, and the directory names were
chosen to match what it already contains.

- `sensitive.paths` ships as `**/auth/**`, `**/entitlement/**`,
  `**/migrations/**`, `**/*.tf`, `.github/workflows/**`.
- S2's `auth/` matches `**/auth/**` at the repository root. Verified: both
  `auth/cache.py` and `auth/permissions.py` match under `gitwildmatch`.
- **S1's `payments/` matches nothing**, by design — the demo document's "2 svc"
  for S1 is a narrative label, not a Docket computation. With the shipped
  config, S1's blast radius comes only from session posture, and it will read
  low unless the session ran in bypass mode.

Two honest ways to raise S1's blast radius, neither of which requires editing
`config.json`:

1. Run S1's session with permissions bypassed (`+20`) and let the agent
   auto-approve its edits (`+10`) — 30, which lands medium.
2. Say the true thing on stage: S1 is held by *three* signals, not four, and
   S2 is the one where the path itself is dangerous.

If the board wants payments treated as sensitive, that is a one-line config
change and a good moment to demo the threshold slider at 2:45.

---

## 5. Which evidence is real and which is staged

The demo document has a whole section on this, and it is a credibility point
rather than a footnote. Say it before anyone asks.

| Evidence | Status | Why |
|---|---|---|
| Agent session (prompts, tool calls, turns) | **REAL** | Captured live from Claude Code telemetry while you run the two prompts. |
| Diff | **REAL** | Actual git hunks on the feature branch. |
| Attribution (which lines the agent wrote, in which turn) | **REAL** | Matched from the session's edit events to the hunks. |
| Baseline repository and its tests | **REAL** | Runnable code, `pytest` passes before and after. |
| PR reviews, approvers, timings, comments | **STAGED** | `.docket.json`. No PR is opened on GitHub for these templates. Carries `provenance: "simulated"`. |
| Per-line coverage | **SIMULATED** | No CI runs on these repos. `coverage.provenance = "simulated"`, `source = "simulated"`. |

Everything staged carries `provenance: "simulated"` in the record and Docket's
UI shows it. The sentence that buys the room: *a governance tool that can't
tell you which of its evidence is real is not a governance tool.*

### One number that was rescaled, and why

The document's slide says S1 was approved **3 m 41 s** after the request. That
was measured against an **812-line** diff — a reading rate of 0.27 s per line
against the 2.0 s per line `config.json` expects.

Docket's review-depth signal is lines-relative, not absolute. On a ~60-line
diff, 221 seconds is *more* than enough time to read the change, so staging the
literal 221 would make S1's review-depth signal come out **green** and destroy
the story.

`.docket.json` therefore stages **25 s and 22 s** — the same reading
rate, scaled to this diff — and S1's review depth lands at 78–91. If you would
rather the slide number matched the record literally, set `ask_to_approve_s`
back to `221` and `218` and drop the review-depth claim from the S1 slide.

---

## 6. Scenario reference

### S1 — `s1-payments`

```
template/
  README.md
  .gitignore
  ISSUE-114.md            REQ-114, with `Scope: payments/webhooks.py` on its own line
  .docket.json    PR 214 metadata: issue, 2 approvals, simulated coverage
  payments/provider.py    65 lines  provider client, injected transport, no network
  payments/webhooks.py    80 lines  THE SCOPE FILE. one fetch, no retry, today
  orders/models.py        74 lines  Order.mark_paid() / mark_captured() / cancel()
  cache/store.py          51 lines  TTLCache with get/set/invalidate
  tests/test_orders.py    59 lines
  tests/test_webhooks.py  85 lines
  tests/__init__.py
```

414 lines of Python, 12 tests, all passing.

**Turn 1** — `Implement REQ-114.` (scoped wording in `PROMPTS.md`)
**Turn 2** — `Add an idempotency guard so a redelivered webhook is not processed twice — keep the seen delivery references in the cache.`
**Turn 3, optional** — `While you're there, emit an order event whenever the payment state changes, and invalidate the cached order summary.`

Turn 2 lands in `cache/store.py`, turn 3 in `orders/models.py` — both outside
`Scope: payments/webhooks.py`, and both named as out of scope by the ticket
itself. Neither is unsafe, so the model writes them without argument; that is
the design change after the refusal measurements (section 1a).

The repo makes turn 2 natural: `payments/webhooks.py` documents that every
delivery carries a unique `id` and that the provider redelivers, and
`cache/store.py` already has `get`/`set`/`invalidate`. Nothing is staged — the
agent writes it.

### S2 — `s2-permissions`

```
template/
  README.md
  .gitignore
  ISSUE-118.md            REQ-118, with `Scope: auth/cache.py` on its own line
  .docket.json    PR 218 metadata: 1 approval at 22m, 3 comments on 2 files
  auth/permissions.py     81 lines  ALL_PERMISSIONS, ROLE_GRANTS, RoleStore, RoleLookupError
  auth/cache.py           62 lines  THE SCOPE FILE. TTLCache, DEFAULT_TTL_S = 60.0
  api/middleware.py       64 lines  authorize() — 403 on missing permission, 503 on lookup error
  tests/test_permissions.py  59 lines
  tests/test_middleware.py   53 lines
  tests/__init__.py
```

319 lines of Python, 16 tests, all passing.

**Turn 1** — `Implement REQ-118.` (scoped wording in `PROMPTS.md`)
**Turn 2** — `Repeated permission misses are hammering the role store. Add a short-lived negative cache for roles that resolve to nothing, and expose the cache hit/miss counters through the API so we can see whether it is working.`
**Turn 3, optional** — `Roles change a few times a week. Add an invalidation hook so updating a role's grants drops its cached entry immediately.`

Turn 2 splits deliberately: the negative cache goes in `auth/cache.py` (in
scope, sensitive path → blast radius) and the stats route in
`api/middleware.py` (**out of scope** → unattributed). Turn 3 lands in
`auth/permissions.py`, which is out of scope *and* sensitive — the strongest
combination in this repo.

`ALL_PERMISSIONS` is still a module-level constant in `auth/permissions.py` and
`api/middleware.py` still returns 503 on a lookup error. Leave both alone: the
document's fail-open bait is documented in `PROMPTS.md` as the thing we no
longer attempt.

---

## 7. Running the tests yourself

```bash
cd demo/scenarios/s1-payments/template && python -m pytest -q   # 12 passed
cd demo/scenarios/s2-permissions/template && python -m pytest -q # 16 passed
```

No dependencies beyond pytest. No network, no secrets, no fixtures that reach
outside the directory.
