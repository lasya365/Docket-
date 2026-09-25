# sample-repo — the service Docket governs in the demo

This is **not** part of Docket. It is a deliberately tiny Python service that
stands in for the repository a team actually works in, so the demo has something
real for Claude Code to edit, for CI to test, and for Docket to gate.

In a real setup this would be its own Git repository, opened in its own Claude
Code session. It is kept inside `demo/` here only so the demo is self-contained.

## What is in it

| Path | Why it exists |
|---|---|
| `provisioning/seats.py` | Ordinary, non-sensitive code. Edits here score low. |
| `entitlement/roles.py` | Matches `**/entitlement/**` in Docket's `config.json` `sensitive.paths`, so edits here raise blast radius and can fire hard stop HS3. |
| `tests/` | Real passing pytest tests, so `pytest --cov` produces a real `coverage.json` for the untested-generation signal. |
| `requirements.txt` | Installed by CI before pytest. |
| `.github/workflows/ci.yml` | The CI job from RFC appendix D. It uploads `coverage.json` as the `coverage-json` artifact, which is the name Docket's `github.coverage_artifact_name` looks for. |
| `.github/ISSUE_TEMPLATE/change.md` | Carries the `Scope:` line Docket reads as declared intent. |

## Running the tests

```bash
cd demo/sample-repo
python -m pytest -q
python -m pytest --cov=. --cov-report=json:coverage.json   # needs pytest-cov
```

`coverage.json` is a build output; do not commit it.

## The agent-setup files go here

The files in RFC appendix A — `docket-env.sh`, `.claude/settings.json` and its
hooks, and the `prepare-commit-msg` git hook that stamps the `DocketSession-Id`
trailer — live in the Docket repo under [`agent-setup/`](../../agent-setup/) and
are **installed into this repo** before the demo. They are owned by another part
of the build; this README only points at them, it does not copy them. Without
them a session in this repo produces no telemetry and no commit trailer, so every
change here would land unmatched.

The `entitlement_svc` MCP stub ([`demo/entitlement_mcp_stub.py`](../entitlement_mcp_stub.py))
is registered in *this* repo's Claude Code, so that a session can really call
`entitlement_svc.grant` and the call really shows up in telemetry.

## Opening the demo pull request

GitHub does not let an author approve their own pull request. One teammate opens
the PR from this repo, the other approves it — otherwise the review link in
Docket's evidence chain stays missing.
