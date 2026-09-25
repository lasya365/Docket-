# S4 — knowledge article rewrite, kb-2211 revision 7 → 8

`kind: knowledge` · **a support agent changed production**

A Freshservice solution article — *VPN certificate errors* — that three agents
retrieve from. Someone with permission to fix a typo reorders the diagnosis
steps and deletes a workaround. It is live the moment Save is clicked, nobody
approves it, and it now contradicts `kb-1180`, an approved article that still
describes the workaround.

---

## What the audience sees

| | |
|---|---|
| **change_key** | `mf-resolution-agent-09479364f9b4` |
| **kind** | `knowledge` |
| **title** | `resolution-agent: knowledge changed` |
| **decision** | **HOLD**, composite **60.0** (threshold 45, risk `high`) |
| **hard stop** | `HS1` — "No reviewable artifact" scored 100 |
| **evidence chain** | **4 of 6 links MISSING**: `diff`, `review`, `verify`, `deploy` |
| **review** | `none: no reviewer, no approval` — **0 reviewers** |
| **deploy** | `no planned window; already live across 3 workflows` |
| **intent** | present but weak: `INC-4390` — a support ticket asking for a refresh, not a change request |
| **backout** | Docket writes one: `kb-2211 r8 → kb-2211 r7`, restore bundle #3 |

The backout line is the payoff. Before Docket there was no rollback plan, because
there was no change:

```
Restore agent bundle #4 → #3
knowledge: kb-2211 r8, kb-1180 r3 → kb-2211 r7, kb-1180 r3  (-kb-2211 r8, +kb-2211 r7)
```

---

## The exact keystrokes

Docket must be running first (`make serve` from the repo root, or set
`DOCKET_URL` if it is on another port).

```bash
# one command, on stage
./demo/scenarios/s4-knowledge/run.sh

# put the article back to revision 7 afterwards
./demo/scenarios/s4-knowledge/run.sh --reset
```

`DOCKET_API_TOKEN` is sent as a bearer token when it is set. `DOCKET_URL`
defaults to `http://localhost:8000`.

What `run.sh` does, in order:

1. `GET /healthz`, then prints what the bundle pins: `kb-2211 r7, kb-1180 r3`.
2. `POST /manifest/check` to baseline the ledger (a first sighting files
   nothing, by design).
3. **`diff -u demo/knowledge/kb-2211.r7.md demo/knowledge/kb-2211.r8.md`** — the
   audience reads the actual edit: the diagnosis steps reverse (revision 7 reads
   the certificate first, revision 8 checks the concentrator first), and the
   whole *"Workaround while a certificate is being reissued"* section, plus its
   pointer to `kb-1180`, disappears.
4. Prints `demo/knowledge/retrieval-map.json` — the **SIMULATED** mapping of who
   retrieves the article: `resolution-agent`, `onboarding-agent`,
   `self-service-portal`, and the contradiction with `kb-1180`.
5. Edits `demo/agent.manifest.json`: `kb-2211` revision `7` → `8`, and sets
   `change_ref: "INC-4390"` (the support ticket). `change_ref` is excluded from
   the bundle hash (RFC 7.5 step 2), so it never files a change of its own.
6. `POST /manifest/check`, prints the `change_key`.
7. `GET /changes/{change_key}` — decision, composite, signals, the six evidence
   links with the missing ones starred, and the backout plan.

---

## The line to say out loud

> **"Someone with permission to fix a typo just changed how every VPN incident
> gets resolved. No ITSM tool filed a record for it. Docket did — and it wrote
> the backout plan that never existed."**

---

## Why there is no Freshservice account in this

The demo document stages S4 as a live edit in a real Freshservice instance. That
needs an account nobody here has, so S4 runs entirely off files and still travels
**the real code path**:

* The article is pinned in the agent bundle — `demo/agent.manifest.json` →
  `knowledge: [{"id": "kb-2211", "revision": "7"}, …]`. Bumping that revision is
  what the Manifest Watcher sees (RFC 7.5). No new pipeline was invented.
* The two revisions ship as readable files so the before/after can be shown:
  `demo/knowledge/kb-2211.r7.md` and `kb-2211.r8.md`.
* `demo/knowledge/kb-1180.md` is the approved article that still describes the
  removed workaround.

**The real-Freshservice seam** is one small component that does not exist yet: a
poller that `GET`s `/api/v2/solutions/articles/{id}` on the domain in
`config.json` and writes the article's revision into the manifest's `knowledge`
entry. Everything downstream is unchanged. It was not written because RFC
section 0 rule 5 forbids guessing vendor field names, and RFC section 7 has no
mapping table for solution articles. Docket's Freshservice client
(`backend/src/docket/record/freshservice.py`) only **writes** change records
today; it never reads articles. `demo/knowledge/retrieval-map.json` says all of
this in its `real_freshservice_path` block.

---

## Real vs simulated

| Evidence | Status |
|---|---|
| The manifest, the bundle hash, the ledger, the decision, the backout plan | **real** — computed by Docket |
| The articles (`kb-2211.r7.md`, `kb-2211.r8.md`, `kb-1180.md`) | **simulated** — written for the demo; each carries `provenance: simulated` in its front matter |
| `retrieval-map.json` — which agents retrieve which article | **simulated** — *configured, not discovered*. `provenance: "simulated"` at the top of the file, with a `simulated_because` line saying why |

---

## What Docket does not yet measure

Two things the demo document lists as S4 signals are **not** Docket signals
today, and the script does not pretend otherwise — it prints them from
`retrieval-map.json`, clearly labelled simulated, instead of dressing them up as
computed evidence:

* **"RETRIEVAL IMPACT — 3 agents"**: Docket's blast radius counts the *workflows*
  on the changed bundle (3), not retrievers of an article. The numbers agree by
  coincidence, not by measurement.
* **"CONTRADICTS — kb-1180"**: there is no contradiction detector. `kb-1180` is
  pinned in the bundle so it is at least visible in the record and in the backout
  plan, but nothing computes the conflict.
