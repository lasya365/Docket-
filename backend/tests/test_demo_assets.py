"""The demo assets have to work, not just exist (RFC Appendix D, section 14).

These are cheap guards against the two ways demo scaffolding rots: a file the
collectors cannot parse, and a rehearsal file whose name no longer matches the
manifest hash it belongs to.

They read `demo/agent.manifest.baseline.json`, never `demo/agent.manifest.json`.
The second one is the demo's INPUT: the documented no-code demos edit it in place
(model 4.1 -> 4.2, article revision 7 -> 8), so pinning tests to it made a normal
demo run turn the suite red. The baseline is what the shipped rehearsal corpora
are named after, and what `run.sh --reset` restores.
"""

from __future__ import annotations

import json
import os

from docket.collectors.github import parse_scope_paths
from docket.collectors.watchers.manifest import (
    changed_keys,
    kind_for,
    load_manifest,
    manifest_hash,
)
from docket.models.manifest import RehearsalFragment


def test_issue_template_scope_line_is_parseable(repo_root):
    """A `## Scope:` heading does not match the collector's regex; a bare line does."""
    body = (repo_root / "demo" / "sample-repo" / ".github" / "ISSUE_TEMPLATE" / "change.md").read_text()
    scope = parse_scope_paths(body)
    assert scope, "the sample issue template must yield a scope the Repo Collector can read"
    assert "provisioning/**" in scope


def test_scope_paths_actually_match_the_sample_repo(repo_root):
    import pathspec

    body = (repo_root / "demo" / "sample-repo" / ".github" / "ISSUE_TEMPLATE" / "change.md").read_text()
    spec = pathspec.PathSpec.from_lines("gitwildmatch", parse_scope_paths(body))
    assert spec.match_file("provisioning/seats.py")
    assert spec.match_file("entitlement/roles.py")
    assert not spec.match_file("billing/invoice.py")


def test_demo_manifest_loads_and_hashes(repo_root):
    manifest, digest = load_manifest(repo_root / "demo" / "agent.manifest.baseline.json")
    assert manifest.agent_id == "resolution-agent"
    assert manifest.model == "vendor-4.1"
    assert manifest.workflows, "blast radius reads the workflow list"
    assert len(manifest.workflows) == 3, "S3 blast radius is told on stage as '3 flows'"
    assert len(digest) == 64
    # the prompt file must exist, or the watcher cannot hash the bundle
    assert manifest.prompt.sha256 and manifest.prompt.sha256 != ""


def test_demo_manifest_pins_the_two_knowledge_articles_s4_needs(repo_root):
    """S4 bumps kb-2211 r7 -> r8; kb-1180 is the approved article it contradicts."""
    manifest, _ = load_manifest(repo_root / "demo" / "agent.manifest.baseline.json")
    pinned = {k.id: k.revision for k in manifest.knowledge}
    assert pinned == {"kb-2211": "7", "kb-1180": "3"}


def test_knowledge_article_bump_files_a_knowledge_change(repo_root):
    """The S4 edit has to travel the existing Manifest Watcher, not a new pipeline."""
    manifest, _ = load_manifest(repo_root / "demo" / "agent.manifest.baseline.json")
    after = manifest.model_copy(deep=True)
    for ref in after.knowledge:
        if ref.id == "kb-2211":
            ref.revision = "8"
    keys = changed_keys(manifest, after)
    assert keys == ["knowledge"]
    assert kind_for(keys) == "knowledge"
    assert manifest_hash(after) != manifest_hash(manifest)


def test_knowledge_articles_show_the_before_and_after_on_stage(repo_root):
    """S4 is readable without a Freshservice account: the revisions ship as files."""
    knowledge = repo_root / "demo" / "knowledge"
    r7 = (knowledge / "kb-2211.r7.md").read_text()
    r8 = (knowledge / "kb-2211.r8.md").read_text()
    kb1180 = (knowledge / "kb-1180.md").read_text()

    # the removed workaround is the whole point of the scenario
    assert "corp-remote-legacy" in r7, "revision 7 must still describe the workaround"
    assert "corp-remote-legacy" not in r8, "revision 8 removes the workaround"
    assert "corp-remote-legacy" in kb1180, "kb-1180 still describes it, and is still approved"
    assert "kb-1180" in r7 and "kb-1180" not in r8, "r8 drops the pointer to kb-1180"

    # the step order changes: r7 reads the certificate first, r8 checks the concentrator first
    assert r7.index("Read the certificate") < r7.index("Check the concentrator")
    assert r8.index("Check the concentrator") < r8.index("Read the certificate")


def test_retrieval_map_is_labelled_simulated_and_names_three_retrievers(repo_root):
    """RFC section 0, rule 7: a configured mapping is not a discovered one."""
    body = json.loads((repo_root / "demo" / "knowledge" / "retrieval-map.json").read_text())
    assert body["provenance"] == "simulated"
    assert body["simulated_because"], "the file must say why it is simulated"
    article = body["articles"]["kb-2211"]
    assert article["revision_before"] == "7" and article["revision_after"] == "8"
    assert len(article["retrieved_by"]) == 3, "the demo says '3 agents' out loud"
    assert {r["agent_id"] for r in article["retrieved_by"]} == {
        "resolution-agent",
        "onboarding-agent",
        "self-service-portal",
    }
    assert article["contradicts"][0]["article_id"] == "kb-1180"
    # every revision the map names must actually ship
    for entry in body["articles"].values():
        for path in entry["files"].values():
            assert (repo_root / path).is_file(), path


def test_rehearsal_files_are_named_after_the_bundle_they_belong_to(repo_root):
    """`{agent_id}-{hash12}.json` is how the watcher finds them (RFC 7.5 step 7)."""
    manifest, _ = load_manifest(repo_root / "demo" / "agent.manifest.baseline.json")
    rehearsal_dir = repo_root / "demo" / "rehearsal"
    files = sorted(rehearsal_dir.glob("*.json"))
    assert files, "the demo ships stored rehearsal results"

    # the model-bump demo: change model to vendor-4.2
    bumped = manifest.model_copy(deep=True, update={"model": "vendor-4.2"})
    # the permission demo: add the sensitive tool
    widened = manifest.model_copy(
        deep=True, update={"tools": [*manifest.tools, "entitlement_svc.grant"]}
    )

    names = {p.name for p in files}
    for variant, story in ((bumped, "model bump"), (widened, "permission widening")):
        expected = f"{variant.agent_id}-{manifest_hash(variant)[:12]}.json"
        assert expected in names, f"no rehearsal file for the {story} demo (looked for {expected})"

    for path in files:
        fragment = RehearsalFragment.model_validate_json(path.read_text())
        assert fragment.provenance == "simulated", "stored corpora are simulated and must say so (P8)"
        assert fragment.cases == fragment.identical + fragment.changed_acceptable + fragment.regressed
        assert len(fragment.regressions) == fragment.regressed
        # the hash inside the file has to agree with the name the watcher looks up
        assert path.name == f"{fragment.agent_id}-{fragment.manifest_hash[:12]}.json"


def test_model_bump_corpus_is_the_one_the_demo_narrates(repo_root):
    """S3 is told as 50 cases, 44 identical, 4 changed acceptably, 2 regressed."""
    manifest, _ = load_manifest(repo_root / "demo" / "agent.manifest.baseline.json")
    bumped = manifest.model_copy(deep=True, update={"model": "vendor-4.2"})
    path = (
        repo_root
        / "demo"
        / "rehearsal"
        / f"{bumped.agent_id}-{manifest_hash(bumped)[:12]}.json"
    )
    fragment = RehearsalFragment.model_validate_json(path.read_text())
    assert (fragment.cases, fragment.identical, fragment.changed_acceptable, fragment.regressed) == (
        50,
        44,
        4,
        2,
    )
    by_case = {r.case_id: r for r in fragment.regressions}
    assert set(by_case) == {"INC-4102", "INC-4188"}
    assert by_case["INC-4102"].before == "entitlement revoked by transfer"
    assert by_case["INC-4102"].after == "account lockout"
    assert by_case["INC-4188"].before == "VPN certificate expired"
    assert by_case["INC-4188"].after == "network outage"


def test_seed_folders_are_complete(repo_root):
    seeds = sorted(p for p in (repo_root / "demo" / "seed").iterdir() if p.is_dir())
    assert {p.name for p in seeds} == {"held", "cleared", "model-bump", "unmatched"}
    for seed in seeds:
        if (seed / "non_code_change.json").exists():
            continue
        assert (seed / "repo_fragment.json").exists(), seed
        body = json.loads((seed / "repo_fragment.json").read_text())
        assert body["provenance"] == "simulated", "hand-made seeds are labelled (RFC section 14)"


def test_sample_repo_ci_uploads_the_artifact_the_collector_looks_for(repo_root, settings):
    ci = (repo_root / "demo" / "sample-repo" / ".github" / "workflows" / "ci.yml").read_text()
    assert settings.config.github.coverage_artifact_name in ci
    assert "--cov-report=json:coverage.json" in ci


def test_agent_setup_files_are_installable(repo_root):
    setup = repo_root / "agent-setup"
    env = (setup / "docket-env.sh").read_text()
    for key in (
        "CLAUDE_CODE_ENABLE_TELEMETRY=1",
        "OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
        "OTEL_LOG_USER_PROMPTS=1",
        "OTEL_LOG_TOOL_DETAILS=1",
    ):
        assert key in env, f"{key} missing: without it Docket sees less than it needs"

    hooks = json.loads((setup / ".claude" / "settings.json").read_text())["hooks"]
    assert hooks["SessionStart"][0]["hooks"][0]["type"] == "command"
    post = hooks["PostToolUse"][0]
    assert post["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    assert post["hooks"][0]["type"] == "http"

    for script in ("prepare-commit-msg", ".claude/hooks/docket_session_start.sh"):
        path = setup / script
        assert path.exists(), script
        # Windows has no executable bit; there the shebang is what matters, and the
        # hook is invoked as `bash "<path>"` anyway. Check what is true on each.
        if os.name == "posix":
            assert path.stat().st_mode & 0o111, f"{script} must be executable"
        assert path.read_bytes().startswith(b"#!"), f"{script} must carry a shebang"


def test_the_session_start_hook_is_windows_safe(repo_root):
    """`args` switches Claude Code to exec form, which cannot spawn a .sh on Windows
    (EFTYPE ... uv_spawn). Shell form plus an explicit `bash` works everywhere."""
    hooks = json.loads((repo_root / "agent-setup" / ".claude" / "settings.json").read_text())["hooks"]
    start = hooks["SessionStart"][0]["hooks"][0]
    assert "args" not in start, "an args array forces exec form and breaks Windows"
    assert start["command"].startswith("bash "), "the interpreter must be explicit"
    assert "${CLAUDE_PROJECT_DIR}" in start["command"]
    assert '"' in start["command"], "the path must be quoted for spaces"


def test_shell_scripts_are_pinned_to_lf(repo_root):
    """CRLF makes bash look for an interpreter called `bash\\r`."""
    attrs = (repo_root / "agent-setup" / ".gitattributes").read_text()
    assert "*.sh text eol=lf" in attrs
    assert "prepare-commit-msg text eol=lf" in attrs
    for script in ("docket-env.sh", ".claude/hooks/docket_session_start.sh", "prepare-commit-msg"):
        raw = (repo_root / "agent-setup" / script).read_bytes()
        assert b"\r\n" not in raw, f"{script} already contains CRLF"


def test_windows_environment_scripts_ship_and_agree_with_the_bash_one(repo_root):
    setup = repo_root / "agent-setup"
    sh = (setup / "docket-env.sh").read_text()
    ps1 = (setup / "docket-env.ps1").read_text()
    cmd = (setup / "docket-env.cmd").read_text()
    for key in ("CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_EXPORTER_OTLP_PROTOCOL",
                "OTEL_LOG_USER_PROMPTS", "OTEL_LOG_TOOL_DETAILS", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        assert key in sh and key in ps1 and key in cmd, f"{key} missing from a platform script"
    assert "http/json" in ps1 and "http/json" in cmd
