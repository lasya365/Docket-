# Docket demo assets

Everything in this directory is demo scaffolding (RFC appendix D). None of it is
imported by `backend/src/docket/`, and every value in it that was not read from a
real system carries `provenance = "simulated"` (RFC section 0, rule 7). The UI
shows that label.

| Path | What it is |
|---|---|
| `agent.manifest.json` | The `AgentManifest` for `resolution-agent`. `config.json` -> `manifest.path` points at it. |
| `prompts/system.md` | The agent's system prompt. It must exist so the watcher can hash it into `prompt.sha256`. |
| `rehearsal/` | Stored 50-case rehearsal corpora, one per "after" bundle. `config.json` -> `manifest.rehearsal_dir` points here. Each file is named `{agent_id}-{first 12 chars of the after-bundle hash}.json` — recompute it with `manifest_hash()`, never by hand. |
| `knowledge/` | The two Freshservice solution articles S4 uses, one file per revision, plus `retrieval-map.json` (a **simulated**, configured map of which agents retrieve which article). Nothing under `backend/src/docket/` imports them. |
| `scenarios/` | One runnable folder per demo scenario. Each has a `run.sh` and a `README.md` with the keystrokes and the line to say out loud. |
| `seed/` | The four seeded replay cases. **Generated** by `backend/tests/fixtures/build_fixtures.py` — do not hand-edit; regenerate instead. Everything under it is `provenance = "simulated"`. |
| `sample-repo/` | The small governed service Docket watches. See its own README. |
| `entitlement_mcp_stub.py` | A stub MCP server named `entitlement_svc` with one no-op tool, `grant(user, role)`. Registered in `sample-repo`'s Claude Code so a session can really call it. |

Start the backend first; all three demos assume it is running:

```bash
make serve          # from the repo root; see the top-level README for setup
```

---

## Demo 1 — replay: the four seeded cases

```bash
curl -X POST http://localhost:8000/demo/seed
```

This loads the four seeded cases (`cleared`, `held`, `unmatched`, `model-bump`)
from `demo/seed/` and runs each one through the whole pipeline, so the board has
something on it with no GitHub, no CI and no live agent. Expected decisions:
`held` HOLD, `cleared` APPROVE, `model-bump` HOLD, `unmatched` HOLD. The scores are
not the RFC 15.2 golden numbers: the seeds are `provenance = "simulated"`, and a
simulated input degrades a signal, which the gate floors at 40. The golden numbers
are asserted by the test suite on the same fixtures marked `real`.

To regenerate `demo/seed/` after a model or fixture change:

```bash
make fixtures       # from the repo root
```

---

## Demo 2 — the no-code model bump

The point: a change with **no diff at all** still gets a change record, a score
and a decision.

1. Edit `demo/agent.manifest.json` and change

   ```json
   "model": "vendor-4.1"
   ```

   to

   ```json
   "model": "vendor-4.2"
   ```

2. Ask the watcher to look:

   ```bash
   curl -X POST http://localhost:8000/manifest/check
   ```

What you should see: one `NonCodeChange` with `kind = "model_version"` and
`changed_keys = ["model"]`. The manifest hash of the new bundle is
`2feef8d89d6d…`, so the watcher finds and attaches
`demo/rehearsal/resolution-agent-2feef8d89d6d.json` — 50 cases, 44 identical,
4 changed acceptably, **2 regressed** (`INC-4102` and `INC-4188`, where 4.2 names
the wrong root cause on a ticket whose real answer is already closed),
`provenance = "simulated"`. The decision is **HOLD** at composite **84.5**, and
the evidence chain has four missing links (intent, diff, review, deploy),
because a model bump has no diff and no review.

This is scenario **S3**. To run all of it in one command, including the reset:

```bash
./demo/scenarios/s3-model-bump/run.sh
./demo/scenarios/s3-model-bump/run.sh --reset
```

The very first `POST /manifest/check` after a fresh database files **nothing**:
it only records the baseline snapshot, so the next change has something to roll
back to. Run it once before you edit the manifest.

Adding `"change_ref": "CHG-9001"` is deliberately **not** a change: `change_ref`
is excluded from the manifest hash, so attaching a ticket reference to a bundle
never files a second change record.

Reset by changing `model` back to `vendor-4.1`.

---

## Demo 3 — the permission change

The point: widening what an agent may *do* is the highest-risk no-code change,
and Docket treats it that way.

1. In `demo/agent.manifest.json`, add `entitlement_svc.grant` to `tools`:

   ```json
   "tools": ["kb.search", "ticket.update", "entitlement_svc.grant"]
   ```

2. ```bash
   curl -X POST http://localhost:8000/manifest/check
   ```

What you should see: one `NonCodeChange` with `kind = "permission"` (permission
beats every other kind, even if `model` moved in the same edit) and
`changed_keys = ["tools"]`. The new bundle hashes to `4ffbb579cdeb…`, so
`demo/rehearsal/resolution-agent-4ffbb579cdeb.json` is attached: 50 cases, 39
identical, 6 changed acceptably, **5 regressed** — including cases where the
agent now grants a role instead of escalating. `entitlement_svc.*` and `*.grant`
are both in `config.json` -> `sensitive.tools`.

To make the change visible in telemetry too, register the stub server in the
sample repo and have a session actually call it:

```bash
claude mcp add entitlement_svc -- "$PWD/.venv/bin/python" "$PWD/demo/entitlement_mcp_stub.py"
```

(or drop the equivalent block into `demo/sample-repo/.mcp.json` — the snippet is
in the stub's docstring).

Reset by removing `entitlement_svc.grant` from `tools`.

---

## Demo 4 — the knowledge article rewrite

The point: an edit nobody calls a change. A support agent rewrites a Freshservice
solution article that three agents ground their answers on. It is live on save,
no reviewer, no window — and it now contradicts an approved article.

The article is pinned in the agent bundle, so bumping its revision travels the
same Manifest Watcher as every other no-code change.

1. In `demo/agent.manifest.json`, change `kb-2211`'s revision:

   ```json
   "knowledge": [ { "id": "kb-2211", "revision": "8" }, { "id": "kb-1180", "revision": "3" } ]
   ```

2. ```bash
   curl -X POST http://localhost:8000/manifest/check
   ```

What you should see: one `NonCodeChange` with `kind = "knowledge"` and
`changed_keys = ["knowledge"]`, hashing to `09479364f9b4…`. **HOLD** at composite
**60.0**, four missing links (diff, review, verify, deploy), and a backout plan
Docket wrote for a change nobody filed:
`knowledge: kb-2211 r8 → kb-2211 r7`.

The before and after are readable on stage as files — `demo/knowledge/kb-2211.r7.md`
and `kb-2211.r8.md` — and `demo/knowledge/kb-1180.md` is the approved article
that still describes the workaround revision 8 deleted. No Freshservice account
is needed; `demo/knowledge/retrieval-map.json` says what a real instance would
need and why it was not written.

This is scenario **S4**:

```bash
./demo/scenarios/s4-knowledge/run.sh
./demo/scenarios/s4-knowledge/run.sh --reset
```

Reset by setting `kb-2211` back to revision `7` and `change_ref` back to `null`.
