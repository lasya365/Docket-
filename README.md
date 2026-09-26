## Docket — AI-Native Change Governance
As SDLC evolves into ADLC, AI agents are driving change at unprecedented scale. Docket extends Freshservice to bring AI-driven changes into governed, intelligent change management.

## What it is

Docket is an AI-native change governance system for changes created or modified by AI coding agents.

It extends the traditional Change Advisory Board (CAB) process by collecting evidence from AI-agent activity, source-code changes, pull requests, reviews, CI verification, and agent configuration, and bringing that evidence into a single change record.

Docket is designed to make AI-generated changes traceable, reviewable, and governable while keeping the final approval decision with a human.

## What it does

Docket:

* Collects Claude Code telemetry, including agent sessions, prompts, tool calls, and edits.
* Correlates AI-agent sessions with the commits and changes they produced.
* Reads GitHub pull requests, linked issues, declared scope, reviews, review comments, and CI coverage.
* Attributes changed lines to AI, human, mixed, or unknown authorship where evidence permits.
* Evaluates four risk signals: **Unattributed Change, Review Depth, Untested Generation, and Blast Radius**.
* Detects missing evidence and applies penalties rather than treating missing information as approval evidence.
* Supports governance of no-code AI changes such as model-version and agent-manifest changes.
* Applies deterministic scoring and produces an **APPROVE** or **HOLD** decision.
* Records the decision and evidence in Freshservice.
* Can trigger a Freshservice CAB approval workflow and synchronize the resulting human approval.
* Publishes the `docket/gate` status to GitHub for merge governance.
* Optionally uses Anthropic Claude after the decision is sealed to generate an advisory brief for the CAB.

The decision engine itself does **not** use an LLM. Docket's decision is based on collected evidence, configured scoring rules, and hard-stop conditions.

## What it doesn't do

Docket is intentionally scoped as a hackathon prototype. It does not:

* Replace the human CAB or change approver.
* Use an LLM to make the APPROVE/HOLD decision.
* Treat missing telemetry or missing evidence as positive evidence.
* Support every AI coding agent; the current agent integration is Claude Code.
* Provide multi-tenant accounts, enterprise roles, or identity management.
* Provide production-grade database backup, migrations, rate limiting, or monitoring.
* Provide TLS directly.
* Guarantee perfect AI/human line attribution when telemetry is unavailable or code is substantially rewritten.
* Measure actual human reading time; the review-depth signal is an evidence-based estimate.
* Automatically replace the human approval process.

The current implementation deliberately focuses on Claude Code, GitHub, and Freshservice integrations.

## Product Integrations

| Product                       | Used for                                                                                                                                                                                             |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Freshworks — Freshservice** | Filing the Docket change record with the decision, score, evidence seal, and source URL; triggering CAB approval through Workflow Automator; and pulling the human approval result back into Docket. |
| **Anthropic — Claude Code**   | Primary AI-agent evidence source. Docket collects Claude Code telemetry and hooks covering sessions, prompts, tool calls, and edits.                                                                 |
| **Anthropic — Claude**        | Optional post-decision advisory brief and CAB questions. The brief is generated only after the decision is sealed and is explicitly advisory, not part of the decision.                              |
| **GitHub**                    | Reading pull requests, linked issues, declared scope, commits, reviews, review comments, and CI coverage artifacts; and publishing the `docket/gate` commit status.                                  |

### Integration details

**Freshservice**

Docket writes the decision, score, seal, and source URL into Freshservice custom fields. A Freshservice Workflow Automator rule can request CAB approval when the Docket decision requires it. Docket can then synchronize the resulting approval state.

**Anthropic**

Claude Code is used as the governed coding agent and provides the primary telemetry source. Separately, Anthropic Claude can optionally generate a short advisory brief after the decision has been sealed. The brief does not affect the decision.
**GitHub**

Docket reads the PR, linked issue, declared scope, reviews, review comments, commits, and CI coverage. It can also publish the `docket/gate` commit status, with `pending` for HOLD and `success` for APPROVE.

> **Note:** Sarvam, Vobiz, and Databricks are not listed as implemented integrations because the current README does not document an actual Docket integration with those products. They should only be added here if they are genuinely used in the submitted implementation.

## System Interaction Diagram

```text
                         ┌──────────────────────┐
                         │      AI Change       │
                         │  Code / Model /      │
                         │  Agent Configuration │
                         └──────────┬───────────┘
                                    │
                                    ▼
                    ┌────────────────────────────┐
                    │          DOCKET            │
                    │                            │
                    │  1. Collect Evidence       │
                    │  2. Correlate Evidence     │
                    │  3. Deterministic Decision │
                    │  4. Record & Seal          │
                    └─────────────┬──────────────┘
                                  │
             ┌────────────────────┼────────────────────┐
             │                    │                    │
             ▼                    ▼                    ▼
      ┌─────────────┐      ┌─────────────┐     ┌──────────────┐
      │  Anthropic  │      │   GitHub    │     │ Agent        │
      │ Claude Code │      │             │     │ Manifest     │
      │             │      │ PR / Issue  │     │              │
      │ Telemetry   │      │ Reviews     │     │ Model /      │
      │ Prompts     │      │ CI Coverage │     │ Tools /      │
      │ Tool Calls  │      │ Commits     │     │ Permissions  │
      │ Edits       │      │ Scope       │     │              │
      └──────┬──────┘      └──────┬──────┘     └──────┬───────┘
             │                    │                    │
             └────────────────────┼────────────────────┘
                                  ▼
                       ┌─────────────────────┐
                       │   ChangeRecord      │
                       │                     │
                       │ Evidence + Signals  │
                       └──────────┬──────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │ Deterministic Gate  │
                       │                     │
                       │ APPROVE / HOLD      │
                       └──────────┬──────────┘
                                  │
                     ┌────────────┴────────────┐
                     │                         │
                     ▼                         ▼
             ┌──────────────┐          ┌──────────────┐
             │    GitHub    │          │ Freshservice │
             │              │          │              │
             │ docket/gate  │          │ Change       │
             │ commit status│          │ Record       │
             └──────────────┘          └──────┬───────┘
                                               │
                                               ▼
                                        ┌──────────────┐
                                        │ Human / CAB  │
                                        │   Approval   │
                                        └──────┬───────┘
                                               │
                                               ▼
                                        Approval Sync
                                               │
                                               ▼
                                            Docket

                    ┌─────────────────────────────┐
                    │ Anthropic Claude             │
                    │ Optional advisory brief     │
                    │ AFTER decision is sealed    │
                    └─────────────────────────────┘
```

### Decision flow

```text
Claude Code + GitHub + Agent Manifest
                 │
                 ▼
          Collect Evidence
                 │
                 ▼
        Correlate Evidence
                 │
                 ▼
       Four Risk Signals
                 │
                 ▼
          Gate + Hard Stops
                 │
          ┌──────┴──────┐
          ▼             ▼
       APPROVE         HOLD
          │             │
          └──────┬──────┘
                 ▼
          Record + Seal
                 │
          ┌──────┴─────────┐
          ▼                ▼
       GitHub          Freshservice
      gate status       Change Record
                            │
                            ▼
                       Human / CAB
                         Approval
```

# Docket v3

**Change governance for code written by AI agents.**

Companies approve changes through a Change Advisory Board (CAB). AI coding agents now write most of the code, so the board signs changes nobody fully read. Some changes to an AI agent — a new model version, an edited knowledge article, a widened permission — produce no code at all, so they get no record, no approval and no backout plan.

Docket collects evidence of how a change was really made, scores it with plain arithmetic, decides **APPROVE** or **HOLD**, and writes the result into Freshservice, where a human signs.

This repository implements [`RFC.md`](RFC.md) (RFC-0003). The RFC is the contract; this README is how to set it up and use it.

---

## Contents

1. [Five-minute start, no keys needed](#1-five-minute-start-no-keys-needed)
2. [API keys and tokens](#2-api-keys-and-tokens)
3. [Connecting the real systems](#3-connecting-the-real-systems)
4. [Using Docket](#4-using-docket)
5. [How it decides](#5-how-it-decides)
6. [Configuration](#6-configuration)
7. [Command and API reference](#7-command-and-api-reference)
8. [Troubleshooting](#8-troubleshooting)
9. [Is this production ready?](#9-is-this-production-ready)
10. [Repository layout](#10-repository-layout)

---

## 1. Five-minute start, no keys needed

Everything in this section runs offline. You need no API keys, no GitHub, no Freshservice and no Claude Code.

### Requirements

| | |
|---|---|
| Python | **3.11 or newer** (`python3 --version`) |
| make, curl | preinstalled on macOS and most Linux |
| A browser | for the UI |

### Starting from the zip

**1. Unzip and go into the folder.** Every command below runs from inside `docket/`.

```bash
unzip docket-v3.zip
cd docket
```

**2. Install.** This creates a private Python environment in `.venv/` and installs the dependencies into it. Your system Python is not touched.

```bash
make install
```

**3. Create your `.env`.** The zip ships **`.env.example`**, a template listing every setting Docket reads, each with a comment. `make env` copies it to `.env` and fills in a random ingest token:

```bash
make env
```

You can leave `.env` as it is for now. Every API key in it is optional, and the demo needs none of them. Open it later to add keys (section 2). `.env` holds secrets: never commit it or send it anywhere. `.env.example` is the safe one to share.

**4. Check the setup.**

```bash
make test         # 380 tests, all offline, a few seconds
make doctor       # checks your setup and says what to fix, in plain words
```

With no keys, `make doctor` shows warnings and skipped sections, and **0 failures**. That's expected.

**5. Start Docket.**

```bash
make serve        # http://localhost:8000 — stop with Ctrl-C
```

Open **<http://localhost:8000>** and press **Seed demo**. Four changes appear:

| Change | The story | Decision |
|---|---|---|
| Rework provisioning sync | AI wrote 604 of 812 lines, 340 outside the ticket's scope, approved in under two minutes, 31% test coverage on the AI lines, and the agent called `entitlement_svc.grant` | **HOLD** (hard stop HS1) |
| Round invoice tax | small AI-assisted change, in scope, reviewed for 22 minutes with comments, 95% coverage on AI lines | **APPROVE** |
| resolution-agent: model vendor-4.1 → vendor-4.2 | the agent's model changed. No code, no ticket, no review. A stored rehearsal shows 2 regressions in 50 cases | **HOLD**, 4 of 6 evidence links missing |
| Widen entitlement role lookup | a PR touching `entitlement/` with no agent session and no human-authored label | **HOLD** (hard stop HS3) |

Click any change. Then flip the **DOCKET ON / OFF** switch at the top: OFF is what a CAB sees today (CI passed, two approvals, "86% coverage"); ON is what Docket adds.

> The demo cases are labelled **SIMULATED** everywhere they appear. They are hand-made, not read from a real system, and Docket never lets you forget it.

### On Windows

`make` is not a Windows command. Use the PowerShell script instead — same verbs, same behaviour:

```powershell
.\docket.ps1 install      # creates .venv and installs the dependencies
.\docket.ps1 env          # creates .env with a random ingest token
.\docket.ps1 test         # 441 tests, offline
.\docket.ps1 doctor       # checks your setup and says what to fix
.\docket.ps1 serve        # http://localhost:8000   (or: serve 8099)
```

If PowerShell refuses to run it ("running scripts is disabled on this system"), allow it for
that window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

`.\docket.ps1` with no argument lists every command.

### Without make or PowerShell

```bash
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
cp .env.example .env                      # then set DOCKET_INGEST_TOKEN to any long random string
./.venv/bin/python -m pytest backend/tests -q
set -a; . ./.env; set +a                  # load .env into this shell
./.venv/bin/python -m docket.doctor
./.venv/bin/python -m uvicorn "docket.server:get_app" --factory --port 8000
```

---

## 2. API keys and tokens

**Docket's decision never calls an LLM.** Scoring and APPROVE/HOLD are plain arithmetic over collected evidence (RFC principles P4 and P10). No key is needed to decide anything, and turning off any integration never makes a change *easier* to approve.

Keys go in `.env`. `make serve` loads it. Nothing reads `.env` except the Makefile targets. If you start Docket some other way, export the variables yourself.

| Variable | Needed for | Without it | Where it comes from |
|---|---|---|---|
| `DOCKET_INGEST_TOKEN` | Claude Code sending telemetry and hooks to Docket | the ingest routes are **open to anyone who can reach the port** (a warning is logged; `/healthz` reports `ingest_authenticated: false`) | you make it up. `make env` generates one |
| `DOCKET_API_TOKEN` | protecting the routes that change state: run, seed, reset, threshold, manifest check, sync | those routes are open. Fine on a laptop, not on a network | you make it up. Paste it into the **API token** field in the UI |
| `GITHUB_TOKEN` | reading live pull requests, posting the `docket/gate` commit status, downloading CI coverage | only the seeded demo and replay mode work; `POST /run/{pr}` fails in live mode | GitHub → Settings → Developer settings → Personal access tokens (see [3.2](#32-github)) |
| `FRESHSERVICE_API_KEY` | filing changes in Freshservice | every change is written as JSON to `out/freshservice/` instead (dry run) | Freshservice → your profile → **Profile settings** → *Your API key* (see [3.3](#33-freshservice)) |
| `ANTHROPIC_API_KEY` | the **board brief**: 2–3 sentences plus three questions for the CAB, written by Claude *after* the decision | a plain template brief made from the four signal summaries. **The decision is identical either way** | <https://console.anthropic.com> → API keys |

Runtime settings, all optional:

| Variable | Default | Effect |
|---|---|---|
| `DOCKET_DB` | `./docket.sqlite` | the SQLite file, relative to the repo root |
| `DOCKET_MODE` | `live` | `replay` makes `POST /run/{pr}` use `demo/seed/` before GitHub. `make replay` sets it |
| `DOCKET_SIMULATE_COVERAGE` | off | `1` invents deterministic coverage when CI has none, labelled SIMULATED |
| `DOCKET_REPLAY_WRITES` | off | `1` lets replayed runs write to Freshservice and GitHub too |
| `DOCKET_CONFIG` | `./config.json` | use a different config file |

Secrets live only in the environment. They are never written to `config.json`, a log line, the database or a `Run`.

### After adding a key: `make doctor`

`make doctor` reads `.env` and `config.json` and checks each integration you've configured. It says in plain words what's wrong and how to fix it:

```
GitHub
  ✓ token is valid (acts as your-login)
  ✓ repository acme/provisioning-service is readable
  ✗ missing Pull requests: read: GitHub answered 403 when reading the PR, its reviews and comments
      → give the token `Pull requests: read` on acme/provisioning-service
Freshservice
  ✗ custom field `docket_decision` (for the decision) is not on the Change form
      → did you mean cf_docket_decision? then set freshservice.custom_fields.decision in config.json
```

- **It only reads.** It never posts a status, never creates a change, and never generates Claude output: the Anthropic key is checked through the free Models API.
- **It never prints a secret.** Even an error message that contains one is redacted.
- **It exits with code 1 if anything failed**, so you can use it in scripts.
- **`make doctor-offline`** checks only the local setup and contacts nothing.

Two things it cannot prove without doing them: GitHub *write* access for commit statuses, and your Freshservice approval rule. It says so, and the first real run settles both. A failure there is recorded on the run under `outputs.errors`.

---

## 3. Connecting the real systems

Each integration is independent. Add them in any order; Docket degrades gracefully for whatever is missing.

### 3.1 Claude Code (the coding agent)

This is how Docket sees how a change was *really* made: the prompts, the tool calls, and the exact text the agent wrote. It is installed in the **repository you want governed**, not in Docket. Full instructions are in [`agent-setup/README.md`](agent-setup/README.md). The short version, from the governed repo's root:

```bash
cp -R /path/to/docket/agent-setup/.claude .
cp /path/to/docket/agent-setup/prepare-commit-msg .git/hooks/
chmod +x .git/hooks/prepare-commit-msg .claude/hooks/docket_session_start.sh
cp /path/to/docket/agent-setup/docket-env.sh .
```

Edit `docket-env.sh`:
- `DOCKET_INGEST_TOKEN` must equal the value in Docket's `.env`.
- `DOCKET_URL` must point at Docket, if it isn't `http://localhost:8000`. If you change the URL, change it in `.claude/settings.json` too.

Then, **every time**:

```bash
source ./docket-env.sh      # BEFORE starting claude — it reads these once, at startup
claude
```

Needs Claude Code **v2.1.269 or newer** for the best join (older versions still work through the commit trailer), and `jq`, `curl` and `git` on the PATH.

**Check it works:** make one edit in a Claude Code session, then open <http://localhost:8000/sessions>. Your session should be listed with `events` and `edits` above zero. If not, run `claude --debug` and look for `[3P telemetry]` lines, and see [Troubleshooting](#8-troubleshooting).

### 3.2 GitHub

1. Create a token. A **fine-grained** token scoped to the governed repository needs exactly these permissions:

   | Permission | Access | Why |
   |---|---|---|
   | Contents | Read | commits and changed files |
   | Pull requests | Read | the PR, its reviews and review comments |
   | Issues | Read | the linked issue and its `Scope:` line, and the review-request timeline |
   | Commit statuses | **Read and write** | posting the `docket/gate` status |
   | Actions | Read | downloading the coverage artifact from CI |

   A classic token with the `repo` scope also works.

2. Put it in `.env` as `GITHUB_TOKEN=...`.
3. In `config.json`, set `github.default_repo` to your `owner/name`, so `POST /run/{pr}` works without a `?repo=` parameter.
4. Run `make doctor`. The GitHub section should be all ✓ apart from the note about commit-status write access.
5. Restart Docket. Enter a PR number in the UI's **Run** box.

**What the governed repo should have** — see [`demo/sample-repo/`](demo/sample-repo/) for a working example:

- **Issues with a `Scope:` line**, on its own line, for example `Scope: provisioning/**, tests/**`. Docket compares every AI-written line against it. A PR should reference its issue (`Closes #114`). Without a scope, the unattributed signal falls back to a weaker text match and says so.
- **CI that uploads coverage** as an artifact named `coverage-json` (the name is `github.coverage_artifact_name` in `config.json`):

  ```yaml
  - run: pytest --cov=. --cov-report=json:coverage.json
  - uses: actions/upload-artifact@v4
    with: { name: coverage-json, path: coverage.json }
  ```

  Alternatively a CI step can `POST /coverage/{head_sha}` with the same JSON and the ingest token.
- **Branch protection requiring `docket/gate`**, if you want a HOLD to actually block the merge. Docket posts `pending` for HOLD and `success` for APPROVE.

### 3.3 Freshservice

Freshservice has no API for requesting CAB approval on a change. So Docket sets a custom field, and a Workflow Automator rule that *you* create requests the approval. This setup is done by hand, once (RFC appendix C):

1. **Custom fields** on the Change form: `Docket decision` (dropdown: `APPROVE`, `HOLD`), `Docket score` (number), `Docket seal` (text), `Docket source URL` (text).
2. **A CAB group** with at least one member.
3. **A Workflow Automator rule** on Changes: when *Docket decision* is `HOLD` (on create or update), **Request for approval** from the CAB group.
4. **Create one change by hand through the API** and read the error messages. Every account differs, so note:
   - the required fields
   - the numeric codes for priority, impact, status, change type and risk
   - the **API names** of your four custom fields (they often carry a `cf_` prefix)
   - the `approval_status` values before and after approving

Then edit the `freshservice` block of `config.json`:

```jsonc
"freshservice": {
  "enabled": true,                                  // false = dry run to out/freshservice/
  "domain": "yourcompany.freshservice.com",
  "requester_id": 12345,                            // a real requester in your account
  "defaults":   { "priority": 1, "impact": 1, "status": 1, "change_type": 2 },
  "risk_codes": { "low": 1, "medium": 2, "high": 3, "very_high": 4 },
  "custom_fields": {                                // your account's API names
    "decision": "cf_docket_decision", "score": "cf_docket_score",
    "seal": "cf_docket_seal", "source_url": "cf_docket_source_url"
  },
  "approval_status_map": { "approved": [1], "rejected": [2] }
}
```

Put `FRESHSERVICE_API_KEY` in `.env` and run `make doctor`. It checks the key, the domain, the requester and all four custom field names, and suggests the right name when yours carries a prefix. Then restart. If Freshservice rejects a payload, the full response is recorded on the run under `outputs.errors` — the message names the offending field. **Test in a sandbox first.**

To pull an approval back from Freshservice (it then flips the GitHub status to `success` or `failure`):

```bash
curl -XPOST localhost:8000/changes/<change_key>/sync -H "Authorization: Bearer $DOCKET_API_TOKEN"
```

### 3.4 Anthropic (optional)

Put `ANTHROPIC_API_KEY` in `.env` and run `make doctor`, which confirms the key works and the model is available to it. The model is `brief.model` in `config.json` (default `claude-sonnet-5`). The brief appears in the UI and on the Freshservice note under the heading **"Advisory, written by AI. Not part of the decision."**

It is written *after* the decision is sealed and is never read by the scoring code. Only scrubbed prompt excerpts are sent — never full prompts, diffs or telemetry — and secrets matching AWS keys, bearer tokens, `sk-`/`gh*_` tokens, private keys and long hex or base64 runs are replaced with `[redacted]` first. If the call fails or times out (20 s), the template brief is used and the run carries on.

### 3.5 The Docket MCP server (optional)

Lets an assistant ask Docket questions. It is read-only, over the same database:

```bash
claude mcp add docket -- /absolute/path/to/docket/.venv/bin/python -m docket.mcp_server
```

Tools: `list_held`, `get_change`, `explain_signal`. Example: *"Why is change pr-acme-provisioning-service-4471 held?"*

---

## 4. Using Docket

### The UI

| Where | What you see |
|---|---|
| **Changes** (`#/changes`) | the queue. Decision, risk, score, missing evidence, **FILED BY DOCKET** for no-code changes, **SIMULATED** for demo data |
| **Run box** | type a PR number and press Run. The six evidence links reveal as the run progresses |
| **A change → Docket tab** | the six evidence links (intent, session, diff, review, verify, deploy), four signal cards (click one for its evidence), the verdict and any hard stops |
| **Diff tab** | every added line coloured by who wrote it (AI / mixed / human / unknown), a second gutter for test coverage, files outside the declared scope flagged. Click a hunk to see the prompt that produced it |
| **Sessions tab** | how the agent session was matched to the commits, its prompts, its tool calls (sensitive ones highlighted), who approved those calls, and the work it wrote but threw away |
| **Planning tab** | the rollout and backout plans. Docket shows why "revert the commit" is the wrong backout for an agent change |
| **Threshold slider** | drag to preview which changes go to the board; release to save. Hard-stopped changes stay held at any threshold |
| **Seal → Verify** | recomputes the run's SHA-256 and proves the evidence wasn't edited after the decision |
| **Agent manifest ledger** (`#/ledger`) | every version of the agent's bundle; **Check now** looks for a change |

### A no-code change, live

```bash
# with Docket running
curl -XPOST localhost:8000/manifest/check      # first time: records a baseline, files nothing
# edit demo/agent.manifest.json:  "model": "vendor-4.1"  ->  "model": "vendor-4.2"
curl -XPOST localhost:8000/manifest/check      # files a change, HOLD, with a real backout plan
```

Docket also polls the manifest every 30 seconds. Adding `"entitlement_svc.grant"` to `tools` gives the permission demo. See [`demo/README.md`](demo/README.md). Point `manifest.path` in `config.json` at your own agent's manifest to govern a real agent.

### A local branch, with no pull request

Docket can score a feature branch that only exists on your laptop: a real git
repo, a real Claude Code session, no GitHub PR.

```bash
curl -XPOST localhost:8000/run/local \
  -H 'content-type: application/json' \
  -d '{"path": "/abs/path/to/your/repo", "base": "main", "head": "HEAD"}'
# -> {"run_id": "run-...", "change_key": "local-acme-payments-9f21c0ab1d34"}
```

`base` defaults to `main` and `head` to `HEAD`. The run is identical to a PR run
from correlation onwards; only the source of the diff changes. The change key is
`local-{repo}-{head_sha[:12]}`, so re-running the same branch updates the same
change and a new commit starts a new one.

Use it when the change you want to govern has not been pushed, or when you are
demonstrating Docket without a GitHub account. A PR that exists is still better
evidence: use `POST /run/{pr}` for it.

Evidence a local branch does not have — the issue behind it, reviewers, CI — can
be declared in a `.docket.json` file at the repo root:

```json
{
  "repo": "acme/storefront-payments",
  "pr_number": 214,
  "title": "REQ-114: retry payment webhooks on provider rate limiting",
  "issue": {"number": 114, "title": "Webhooks fail under rate limiting",
            "body": "Scope: payments/**, tests/**",
            "url": "https://github.com/acme/storefront-payments/issues/114"},
  "reviews": [{"reviewer": "rdolan", "state": "APPROVED", "ask_to_approve_s": 25}],
  "review_comments": [{"reviewer": "rdolan", "path": "payments/webhooks.py",
                       "line": 62, "body": "what drops the entry?"}],
  "coverage": {"ci_status": "success",
               "files": {"payments/webhooks.py": {"executed": [1, 2], "missing": [3]}}}
}
```

`repo` renames the change (the key becomes `local-acme-storefront-payments-…`),
`ask_to_approve_s` is the review time the Docket record will report, and coverage
line lists may be written as `executed`/`missing` or coverage.py's
`executed_lines`/`missing_lines`.

Provenance is labelled **per section**, not per run. The commits, files and hunks
always come from git, so the diff stays `real` — that is the strongest evidence
Docket has and calling it simulated would flatten every signal to its degraded
floor. Only the sections the file propped up are marked `simulated`: `intent` for
a staged issue, `review` for staged reviews or comments, `verify` for staged
coverage. The change still shows **SIMULATED** in the queue when any section is.
Without the file, the run is real all through, with review and intent simply
missing — an honest finding rather than a gap Docket fills in for you.

### Replay mode

`make replay` runs Docket so that `POST /run/{pr}` looks in `demo/seed/` before it calls GitHub, which gives a demo that behaves identically every time. `make reset` clears runs and tickets but keeps collected sessions and the manifest ledger.

---

## 5. How it decides

Four planes, strictly separated. Only plane 4 writes anywhere, and plane 3 contains no I/O, no clock and no LLM. A test enforces that by parsing its imports.

```
PLANE 1 COLLECT    Claude Code telemetry + hooks, the PR, CI coverage, the agent manifest
PLANE 2 CORRELATE  match sessions to commits, attribute every added line, build the ChangeRecord
PLANE 3 DECIDE     four signals -> gate -> APPROVE/HOLD, evidence chain, backout plan
PLANE 4 RECORD     seal, store, Freshservice, GitHub status, board brief
```

**The four signals.** Each is 0–100, higher is riskier, and each opens into the evidence behind it.

| Signal | Asks |
|---|---|
| Unattributed change | how many AI-written lines fall outside the scope the ticket declared |
| Review depth | how long the fastest approver *could* have spent, against the size of the diff |
| Untested generation | what share of the **AI-written lines** the tests execute — not the file average |
| Blast radius | sensitive tools called, sensitive paths touched, bypass mode, how much was auto-approved |

**Missing evidence is never rewarded.** If a fact is absent — nobody reviewed, no ticket, no tests — the signal scores 100. If Docket *cannot see* — no telemetry arrived — the signal is marked not computable and the gate charges a penalty. An unmatched, undeclared code change scores at least 50 and is always held.

**Hard stops** fire regardless of the average, because 10, 10, 10 and 95 average to 31:

| | Fires when |
|---|---|
| HS1 | any signal scores 90 or more |
| HS2 | a no-code change has no earlier bundle on record to roll back to |
| HS3 | an unmatched code change touches a sensitive path. The `docket:human-authored` label does not switch this off |

**Matching sessions to commits.** Docket tries three methods, in order: the commit SHA the agent reported (`sha`), the `DocketSession-Id` trailer stamped at commit time (`trailer`), then a time window (`time`, only when exactly one session fits). A join is graded: `claimed` means the commit says so; `verified` means Docket found the session's own edits inside the diff.

**Human-written PRs.** Docket cannot tell a person from an agent with telemetry switched off. Label a genuinely human PR `docket:human-authored` and it is judged on what can be seen, instead of being penalised for what can't. The label is recorded as evidence, with a name attached.

---

## 6. Configuration

Everything anyone might argue about lives in [`config.json`](config.json). Nothing is hard-coded.

| Block | Holds |
|---|---|
| `gate` | threshold (45), weights, penalties, hard stops, risk bands, the human-declared label |
| `signals` | per-signal constants: seconds of reading per line, point values, the no-code base scores |
| `sensitive` | sensitive **paths** (`**/entitlement/**`, `**/*.tf`…), **tools** (`entitlement_svc.*`, `*.grant`…), **shell patterns** (`terraform apply`, `rm -rf`…) |
| `correlator`, `attributor` | the join and line-matching tolerances |
| `github` | `default_repo`, status context, coverage artifact name |
| `freshservice` | see [3.3](#33-freshservice) |
| `manifest` | path to the agent manifest, the rehearsal directory, the poll interval |
| `brief` | on/off, model, timeout, excerpt length |
| `server` | `public_base_url`, used in links written to Freshservice and GitHub |

Restart after editing. `settings.py` warns at startup if a config change would let an unmatched, undeclared change through.

---

## 7. Command and API reference

### make targets

| | |
|---|---|
| `make install` | venv and dependencies (Python 3.11+) |
| `make env` | create `.env` from `.env.example`, with a random ingest token |
| `make doctor` | check every configured integration and say what to fix (exit code 1 on failure) |
| `make doctor-offline` | the same, local checks only, contacts nothing |
| `make test` | the test suite, offline |
| `make serve` | run on port 8000 (`make serve PORT=9000` to change) |
| `make replay` | run in replay mode |
| `make demo` / `make reset` | seed / clear a running Docket |
| `make check` | health check |
| `make mcp` | MCP server on stdio |
| `make fixtures` | regenerate test fixtures and demo seeds |
| `make clean` | remove caches, dry-run output and the database |

### HTTP API

Read routes are open. Routes marked ✱ need `Authorization: Bearer $DOCKET_API_TOKEN` when that is set. Ingest routes need `DOCKET_INGEST_TOKEN`. Interactive docs are at <http://localhost:8000/docs>.

| | |
|---|---|
| `POST /v1/logs`, `/v1/metrics`, `/v1/traces` | OTLP telemetry from Claude Code (ingest token; JSON only) |
| `POST /hooks/claude-code` | Claude Code hook payloads (ingest token) |
| `POST /coverage/{head_sha}` | coverage pushed from CI (ingest token) |
| `POST /run/{pr}?repo=owner/name` ✱ | start a run → `{run_id, change_key}` |
| `POST /run/local` ✱ | score a local branch with no PR, body `{path, base, head}` → `{run_id, change_key}` |
| `GET /status/{run_id}` | progress of a run |
| `GET /changes` | the queue |
| `GET /changes/{key}` | the full latest run |
| `GET /changes/{key}/runs` | run history |
| `GET /changes/{key}/verify` | recompute the seal → `{match, seal}` |
| `POST /changes/{key}/sync` ✱ | pull the approval from Freshservice |
| `GET /config/gate` | gate config, with the saved threshold applied |
| `PUT /config/threshold` ✱ | `{"threshold": 0-100}`; re-gates every change |
| `POST /manifest/check` ✱ | run the manifest watcher now |
| `GET /manifest/ledger?agent_id=` | bundle history |
| `GET /sessions` | agent sessions received |
| `POST /demo/seed`, `POST /demo/reset` ✱ | load / clear the demo |
| `GET /healthz` | liveness, and whether ingest is authenticated |

---

## 8. Troubleshooting

**Start with `make doctor`.** It catches most setup problems and says how to fix each one. The table below covers what it can't see.

| Symptom | Cause and fix |
|---|---|
| `make install` says Python is too old | install Python 3.11+ and make sure `python3` points at it |
| Buttons fail with `401 … DOCKET_API_TOKEN` | the server has an API token. Paste it into the **API token** field in the run box |
| Nothing appears in `/sessions` | `docket-env.sh` must be sourced **before** `claude` starts; the ingest tokens must match; Docket must be reachable at `DOCKET_URL`. Run `claude --debug` and look for `[3P telemetry]` |
| Telemetry arrives as `415` | the exporter is sending protobuf. `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` is required |
| A session arrives but its edits don't | the `PostToolUse` hook isn't installed, or its URL in `.claude/settings.json` is wrong. Without edits, lines are labelled `unknown` and two signals become not computable |
| Session shows no prompts or tool names | `OTEL_LOG_USER_PROMPTS=1` and `OTEL_LOG_TOOL_DETAILS=1` are missing. Claude Code redacts both by default |
| A real PR shows `join: unmatched` | the session is older than 7 days, came from another repo or email, or the commit has no `DocketSession-Id` trailer. Install `prepare-commit-msg` |
| `POST /run/{pr}` ends in `error` | no `GITHUB_TOKEN`, or `github.default_repo` is still `owner/name`. `GET /status/{run_id}` has the message |
| Untested generation is always "not computable" | no coverage found for the head commit. Check the CI artifact name, or post coverage to `/coverage/{head_sha}` |
| Nothing reaches Freshservice | `freshservice.enabled` is false (look in `out/freshservice/`), or the account rejected the payload: see `outputs.errors` on the run |
| Unattributed change is "degraded" | the linked issue has no `Scope:` line on a line of its own. A markdown heading like `## Scope:` is not read |

---

## 9. Is this production ready?

**No.** Docket is built exactly to its RFC, which specifies a working system for a 24-hour, two-person build and a live demo. Every MUST and SHOULD item in the RFC is implemented and tested. That is not the same as ready to run a company's change process. Know these before relying on it:

**Deliberate scope limits (from the RFC).**
- Single tenant: one process, one SQLite file, no user accounts, no roles. There are two shared tokens and nothing else.
- Background work runs inside the web process. A restart loses a run in progress.
- Only Claude Code, GitHub.com and Freshservice are supported.

**Not yet proven against real services.** The Freshservice writer, the GitHub status writer, CI artifact download, approval sync and the Claude-written brief are implemented and tested against fakes. None has run against the real service. Section 20 of the RFC lists what must be confirmed on first contact. Expect to adjust `config.json`.

**Operational gaps.**
- No TLS: put Docket behind a reverse proxy.
- No database migrations or backup, and no rate limiting.
- No monitoring beyond `/healthz`.
- Set both `DOCKET_INGEST_TOKEN` and `DOCKET_API_TOKEN` before exposing the port.

**Stretch items, not built:** a pre-commit check the agent can call on itself, live behavioural rehearsal against a model, polling Freshservice knowledge articles, exporting a real run as a seed, and background approval polling.

**Inherent limits.**
- Docket sees only what emits telemetry. An agent with telemetry off produces an UNMATCHED change. It is held when it touches a sensitive path, but it is otherwise judged on less evidence.
- Attribution is text matching, so heavy reformatting turns `ai` lines into `mixed` or `human`.
- "Review depth" is an upper bound on reading time, not a measurement: GitHub exposes neither.

A sensible path to production: run it **in shadow**, posting statuses without branch protection. Compare its HOLDs with your board's actual decisions for a few weeks, and tune `config.json` before letting it block anything.

---

## 10. Repository layout

```
README.md            this file
RFC.md               the specification; section 0 is binding for anyone changing the code
CLAUDE.md            instructions for AI agents working on this repo
config.json          every weight, threshold and sensitivity list
.env.example         template for .env: every environment variable, documented
Makefile             the commands in section 7
backend/src/docket/
  models/            the objects that cross plane boundaries
  collectors/        PLANE 1   OTLP, Claude Code adapter, hooks, GitHub, CI coverage, manifest watcher
  correlate/         PLANE 2   correlator, line attributor, record builder
  decide/            PLANE 3   signals, gate, evidence chain, backout planner (pure)
  record/            PLANE 4   store, seal, Freshservice, GitHub status, board brief
  pipeline.py        one run across the planes
  doctor.py          `make doctor`: setup checks
  server.py          the HTTP API
  mcp_server.py      the read-only MCP server
backend/tests/       380 tests, all offline; fixtures/ generates the demo seeds
frontend/index.html  the whole UI: one file, no build step
agent-setup/         what to install in a governed repository
demo/                agent manifest, rehearsal data, seeds, a sample governed repo, an MCP stub
```
