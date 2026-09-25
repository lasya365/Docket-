# S3 — support agent model 4.1 → 4.2

`kind: model_version` · **the change with nothing to look at**

The support agent that triages incidents in Freshservice has its model bumped by
a vendor release. There is no pull request, no diff, no reviewer and no window.
No ITSM tool in the world creates a record for this. Docket does.

---

## What the audience sees

| | |
|---|---|
| **change_key** | `mf-resolution-agent-2feef8d89d6d` |
| **kind** | `model_version` |
| **title** | `resolution-agent: model vendor-4.1 → vendor-4.2` |
| **decision** | **HOLD**, composite **84.5** (threshold 45, risk `very_high`) |
| **hard stop** | `HS1` — "No reviewable artifact" scored 100 |
| **evidence chain** | **4 of 6 links MISSING**: `intent`, `diff`, `review`, `deploy` |
| **blast radius** | 90 — *3 workflows*, all at once, no canary possible |
| **verify** | rehearsed · 50 cases · **2 regressed** · `provenance = simulated` |
| **backout** | Docket writes one: `model: vendor-4.2 → vendor-4.1`, restore bundle #1 |

The two regressions are the ones the demo document names, and they are read off
resolved tickets — the answer is already in the closed incident:

```
INC-4102   4.1 → entitlement revoked by transfer     correct
           4.2 → account lockout                     wrong
INC-4188   4.1 → VPN certificate expired             correct
           4.2 → network outage                      wrong
```

---

## The exact keystrokes

Docket must be running first (`make serve` from the repo root, or set
`DOCKET_URL` if it is on another port).

```bash
# one command, on stage
./demo/scenarios/s3-model-bump/run.sh

# put the manifest back afterwards
./demo/scenarios/s3-model-bump/run.sh --reset
```

If Docket needs a token, export it and the script sends it:

```bash
export DOCKET_API_TOKEN=…        # optional; only if config demands it
export DOCKET_URL=http://localhost:8000   # optional; this is the default
```

What `run.sh` does, in order:

1. `GET /healthz` — refuses to run if Docket is not up.
2. Prints the before state and copies `demo/agent.manifest.json` to
   `.manifest.before.json` so `--reset` is exact.
3. `POST /manifest/check` to baseline the ledger. A *first* sighting of an agent
   files nothing by design — it only records the snapshot, so the next change
   has something to roll back to.
4. Edits `demo/agent.manifest.json`: `"model": "vendor-4.1"` → `"vendor-4.2"`,
   then re-hashes the bundle and names the stored rehearsal corpus it found.
   **If the hash and the corpus filename disagree, it says so loudly** rather
   than quietly filing a change with no Verify link.
5. `POST /manifest/check` again, prints the `change_key`.
6. `GET /changes/{change_key}` — decision, composite, the four signals, the six
   evidence links with the missing ones starred, and the backout plan.

Want the hand-typed version instead? Open `demo/agent.manifest.json`, change
`vendor-4.1` to `vendor-4.2`, save, and either wait 30s for the poll or run
`curl -X POST $DOCKET_URL/manifest/check`.

---

## The line to say out loud

> **"The resolved ticket is the label."**

You do not need ground truth from anywhere else. Every closed incident already
contains the right answer, which is what makes rehearsal possible at all.

---

## Real vs simulated

| Evidence | Status |
|---|---|
| The manifest, the bundle hash, the ledger, the decision, the backout plan | **real** — computed by Docket from the file on disk |
| The rehearsal corpus (`demo/rehearsal/resolution-agent-2feef8d89d6d.json`) | **simulated** — 50 stored cases with stored outcomes, not a live replay. `provenance = "simulated"`, and the UI shows it |

Say it before anyone asks: a governance tool that cannot tell you which of its
evidence is real is not a governance tool.

---

## If you edit the manifest

The rehearsal corpus is found **by filename**:
`demo/rehearsal/{agent_id}-{first 12 chars of the after-bundle hash}.json`.
Any edit to `demo/agent.manifest.json` — even whitespace inside a value — moves
the hash and orphans the corpus. Recompute it, never guess:

```bash
./.venv/bin/python - <<'PY'
from pathlib import Path
from docket.collectors.watchers.manifest import load_manifest, manifest_hash
m, _ = load_manifest(Path("demo/agent.manifest.json"))
bumped = m.model_copy(deep=True, update={"model": "vendor-4.2"})
print(f"{bumped.agent_id}-{manifest_hash(bumped)[:12]}.json")
PY
```

`backend/tests/test_demo_assets.py` fails if the name drifts.
