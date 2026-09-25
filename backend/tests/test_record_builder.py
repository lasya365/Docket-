"""RFC 8.3. The prize is `test_plane_two_reproduces_the_golden_record`: the real
correlate -> attribute -> build_code path must reproduce, from `fragments(name)`,
everything the decide plane reads out of `record(name)`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from docket.correlate.correlator import correlate
from docket.correlate.line_attributor import attribute
from docket.correlate.record_builder import (
    build_code,
    build_non_code,
    change_key_for_manifest,
    change_key_for_pr,
    scrub,
)
from docket.correlate.types import AttributionResult, JoinResult
from docket.models.fragments import (
    ChangedFile,
    CommitRef,
    CoverageFragment,
    DetailLevel,
    PermissionModeChange,
    RepoFragment,
    Review,
    ReviewComment,
    ReviewRequest,
    SessionFragment,
    ToolCall,
)
from fixtures.build_fixtures import fragments, record

T0 = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)
SHA = "a91f3c7d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8091"
OLD_SHA = "0011223344556677889900112233445566778899"


def run_plane_two(name: str, cfg):
    """The real path the pipeline will take."""
    parts = fragments(name)
    repo_fragment = parts["repo_fragment"]
    joins = correlate(repo_fragment, parts["sessions"], cfg.correlator)
    attribution = attribute(repo_fragment, joins, cfg.attributor)
    return build_code(repo_fragment, joins, attribution, parts["coverage"], cfg), joins, attribution


# ===========================================================================
# the cross-check
# ===========================================================================

@pytest.mark.parametrize("name", ["held", "cleared"])
def test_plane_two_reproduces_the_golden_record(name, cfg):
    built, _joins, _attr = run_plane_two(name, cfg)
    expected = record(name)

    # identity
    assert built.change_key == expected.change_key
    assert built.kind == expected.kind == "code"
    assert built.title == expected.title
    assert built.source_url == expected.source_url
    assert built.created_at == expected.created_at

    # diff: every number the signals read
    assert built.diff.lines_added == expected.diff.lines_added
    assert built.diff.totals == expected.diff.totals
    assert built.diff.attribution_by_line == expected.diff.attribution_by_line
    assert built.diff.present is True

    # review
    assert len(built.review.approvers) == len(expected.review.approvers)
    for got, want in zip(built.review.approvers, expected.review.approvers):
        assert got.reviewer == want.reviewer
        assert got.ask_to_approve_s == want.ask_to_approve_s
        assert got.approved_commit == want.approved_commit
        assert got.approved_at == want.approved_at
    assert built.review.files_changed == expected.review.files_changed
    assert built.review.files_commented == expected.review.files_commented
    assert built.review.comments == expected.review.comments
    assert built.review.stale_approval == expected.review.stale_approval

    # intent
    assert built.intent.present == expected.intent.present
    assert built.intent.scope_paths == expected.intent.scope_paths
    assert built.intent.text == expected.intent.text
    assert built.intent.ref == expected.intent.ref

    # verify
    assert built.verify.present == expected.verify.present
    assert built.verify.ci_status == expected.verify.ci_status
    assert built.verify.coverage_by_line == expected.verify.coverage_by_line
    assert built.verify.tests_authored_by == expected.verify.tests_authored_by

    # join
    assert built.join.method == expected.join.method == "sha"
    assert built.join.confidence == expected.join.confidence == "verified"
    assert built.join.human_declared == expected.join.human_declared

    # hunk labels (the counts, not the crediting, which the RFC defines differently
    # from the fixture's shortcut: see test_hunk_prompt_id_is_the_credited_turn)
    got_hunks = {h.hunk_id: (h.label, h.counts) for h in built.diff.hunk_attribution}
    want_hunks = {h.hunk_id: (h.label, h.counts) for h in expected.diff.hunk_attribution}
    assert got_hunks == want_hunks

    # sessions
    assert len(built.sessions) == len(expected.sessions)
    for got, want in zip(built.sessions, expected.sessions):
        assert got.session_id == want.session_id
        assert got.present == want.present
        assert got.turns == want.turns
        assert got.retries == want.retries
        assert got.lines_written == want.lines_written
        assert got.lines_discarded == want.lines_discarded
        assert got.files_written == want.files_written
        assert got.files_discarded == want.files_discarded
        assert got.approvals_by == want.approvals_by
        assert got.permission_modes == want.permission_modes
        assert got.prompt_excerpts == want.prompt_excerpts
        assert got.join_method == want.join_method
        assert got.join_confidence == want.join_confidence
        assert got.model == want.model


def test_held_reports_its_discarded_work(cfg):
    """12 lines the agent wrote never reached the diff, and a whole file was dropped."""
    built, _joins, attribution = run_plane_two("held", cfg)
    session = built.sessions[0]

    assert session.lines_discarded == 12
    assert "provisioning/legacy_sync.py" in session.files_discarded
    assert attribution.discarded_by_session[session.session_id].lines == 12
    # the discarded lines sat in a PR file the session also wrote into
    assert "provisioning/sync.py" not in session.files_discarded


def test_cleared_discards_nothing(cfg):
    built, _joins, _attr = run_plane_two("cleared", cfg)
    assert built.sessions[0].lines_discarded == 0
    assert built.sessions[0].files_discarded == []


def test_unmatched_fixture_is_unmatched_and_unknown(cfg):
    """No telemetry arrived, so nothing may be called human (RFC 0 rule 6)."""
    built, joins, _attr = run_plane_two("unmatched", cfg)

    assert [j for j in joins if j.is_join] == []
    assert built.sessions == []
    # UNMATCHED must say WHY, not just that it is unmatched (RFC P9).
    assert built.join.notes, "an unmatched change has to explain which link is broken"
    assert any("no agent session reached docket" in n.lower() for n in built.join.notes)
    assert (built.join.method, built.join.confidence) == ("none", "unmatched")
    assert built.diff.totals["unknown"] == built.diff.lines_added == 60
    assert built.diff.totals["human"] == 0


def test_the_golden_totals_are_the_rfc_numbers(cfg):
    held, _j, _a = run_plane_two("held", cfg)
    cleared, _j2, _a2 = run_plane_two("cleared", cfg)

    assert held.diff.totals == {"ai": 564, "mixed": 40, "human": 208, "unknown": 0}
    assert held.diff.lines_added == 812
    assert held.diff.totals["ai"] + held.diff.totals["mixed"] == 604      # RFC 15.1
    assert cleared.diff.totals == {"ai": 46, "mixed": 4, "human": 14, "unknown": 0}
    assert cleared.diff.lines_added == 64
    assert cleared.diff.totals["ai"] + cleared.diff.totals["mixed"] == 50


def test_build_code_is_deterministic(cfg):
    first, _j, _a = run_plane_two("held", cfg)
    second, _j2, _a2 = run_plane_two("held", cfg)
    assert first.model_dump_json() == second.model_dump_json()


# ===========================================================================
# field-by-field rules (RFC 8.3)
# ===========================================================================

def test_change_key_shapes():
    assert change_key_for_pr("acme/provisioning-service", 4471) == "pr-acme-provisioning-service-4471"
    assert change_key_for_pr("ACME/Some_Repo.js", 12) == "pr-acme-some-repo-js-12"
    assert change_key_for_manifest("resolution-agent", "c" * 64) == "mf-resolution-agent-cccccccccccc"


def test_hunk_prompt_id_is_the_credited_turn(cfg):
    """RFC 8.2 step 7: `prompt_id` is the turn credited with the most lines in the hunk."""
    built, _j, _a = run_plane_two("held", cfg)
    backoff = [h for h in built.diff.hunk_attribution if h.hunk_id == "provisioning/backoff.py#0"][0]

    assert backoff.session_id == "held-session-1"
    assert backoff.prompt_id == "held-session-1-p2"      # the turn whose edit wrote that file


# --- the scrub (RFC 17.3) --------------------------------------------------

@pytest.mark.parametrize(
    "secret",
    [
        "AKIAABCDEFGHIJKLMNOP",
        "Bearer abcdefghijklmnopqrstuvwxyz0123",
        "bearer abcdefghijklmnopqrstuvwxyz0123",
        "sk-abcdefghijklmnopqrstuvwxyz",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "-----BEGIN RSA PRIVATE KEY-----",
        "a" * 40,
        "deadbeef" * 6,
    ],
)
def test_scrub_redacts_every_rfc_pattern(secret):
    out = scrub(f"deploy using {secret} please")
    assert secret not in out
    assert "[redacted]" in out
    assert out.startswith("deploy using ") and out.endswith(" please")


def test_scrub_leaves_ordinary_prose_alone():
    text = "Fix the provisioning sync so retries do not drop entitlements. See issue 114."
    assert scrub(text) == text
    assert scrub("") == ""


def test_prompt_excerpts_are_scrubbed_and_truncated(cfg):
    repo_fragment, coverage = _tiny_pr()
    session = _tiny_session()
    session.turns[0].prompt_text = "token ghp_" + "a" * 36 + " then " + "retry the sync loop " * 40
    joins = [JoinResult(session_id="s1", session=session, method="sha", confidence="claimed")]

    built = build_code(repo_fragment, joins, AttributionResult(), coverage, cfg)
    excerpt = built.sessions[0].prompt_excerpts[0]

    assert "ghp_" not in excerpt
    assert "[redacted]" in excerpt
    assert len(excerpt) == cfg.brief.prompt_excerpt_chars


def test_a_redacted_prompt_becomes_an_empty_excerpt(cfg):
    repo_fragment, coverage = _tiny_pr()
    session = _tiny_session()
    session.turns[0].prompt_text = None
    joins = [JoinResult(session_id="s1", session=session, method="sha", confidence="claimed")]

    built = build_code(repo_fragment, joins, AttributionResult(), coverage, cfg)
    assert built.sessions[0].prompt_excerpts == [""]


# --- approvals, modes, ghosts ---------------------------------------------

def test_approvals_by_maps_every_decision_source(cfg):
    repo_fragment, coverage = _tiny_pr()
    session = _tiny_session()
    session.tool_calls = [
        _call("c1", "config"), _call("c2", "config"),
        _call("c3", "hook"),
        _call("c4", "user_permanent"), _call("c5", "user_temporary"),
        _call("c6", None),
    ]
    joins = [JoinResult(session_id="s1", session=session, method="sha", confidence="claimed")]

    built = build_code(repo_fragment, joins, AttributionResult(), coverage, cfg)
    assert built.sessions[0].approvals_by == {"rule": 2, "hook": 1, "human": 2}


def test_permission_modes_are_in_order_and_deduplicated(cfg):
    repo_fragment, coverage = _tiny_pr()
    session = _tiny_session()
    session.permission_mode_changes = [
        PermissionModeChange(from_mode="default", to_mode="acceptEdits", trigger="user", timestamp=T0),
        PermissionModeChange(from_mode="acceptEdits", to_mode="bypassPermissions", trigger="user", timestamp=T0),
        PermissionModeChange(from_mode="bypassPermissions", to_mode="default", trigger="user", timestamp=T0),
    ]
    joins = [JoinResult(session_id="s1", session=session, method="sha", confidence="claimed")]

    built = build_code(repo_fragment, joins, AttributionResult(), coverage, cfg)
    assert built.sessions[0].permission_modes == ["default", "acceptEdits", "bypassPermissions"]


def test_a_ghost_session_carries_only_its_join(cfg):
    repo_fragment, coverage = _tiny_pr()
    ghost = JoinResult(
        session_id="gone", session=None, method="trailer", confidence="claimed",
        note="commit names session gone but Docket never received its telemetry",
    )

    built = build_code(repo_fragment, [ghost], AttributionResult(), coverage, cfg)
    section = built.sessions[0]

    assert section.present is False
    assert section.session_id == "gone"
    assert (section.join_method, section.join_confidence) == ("trailer", "claimed")
    assert section.tools_used == [] and section.prompt_excerpts == [] and section.detail is None
    assert ghost.note in built.join.notes


def test_join_takes_the_best_confidence(cfg):
    repo_fragment, coverage = _tiny_pr()
    session = _tiny_session()
    joins = [
        JoinResult(session_id="gone", session=None, method="trailer", confidence="claimed"),
        JoinResult(session_id="s1", session=session, method="time", confidence="inferred"),
    ]
    attribution = AttributionResult(matched_lines_by_session={"s1": 50})

    built = build_code(repo_fragment, joins, attribution, coverage, cfg)

    # RFC 8.2 step 9 raises the time join to verified, which then wins the section.
    assert (built.join.method, built.join.confidence) == ("time", "verified")
    assert built.sessions[1].join_confidence == "verified"


def test_a_note_only_join_makes_no_session(cfg):
    repo_fragment, coverage = _tiny_pr()
    joins = [JoinResult.note_only("ambiguous: 2 sessions overlap")]

    built = build_code(repo_fragment, joins, AttributionResult(), coverage, cfg)

    assert built.sessions == []
    assert (built.join.method, built.join.confidence) == ("none", "unmatched")
    assert built.join.notes == ["ambiguous: 2 sessions overlap"]


def test_human_declared_label(cfg):
    repo_fragment, coverage = _tiny_pr()
    repo_fragment.labels = [cfg.gate.human_declared_label]

    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)
    assert built.join.human_declared is True


# --- review ----------------------------------------------------------------

def test_only_the_latest_approval_per_reviewer_counts(cfg):
    repo_fragment, coverage = _tiny_pr()
    repo_fragment.reviews = [
        Review(reviewer="rdolan", state="CHANGES_REQUESTED", submitted_at=T0 + timedelta(minutes=10), commit_id=OLD_SHA),
        Review(reviewer="rdolan", state="APPROVED", submitted_at=T0 + timedelta(minutes=20), commit_id=OLD_SHA),
        Review(reviewer="rdolan", state="APPROVED", submitted_at=T0 + timedelta(minutes=40), commit_id=SHA),
    ]
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)

    assert [(a.reviewer, a.approved_commit) for a in built.review.approvers] == [("rdolan", SHA)]
    assert built.review.comments == 1                   # the CHANGES_REQUESTED review
    assert built.review.stale_approval is False


def test_stale_approval_when_every_approval_is_against_an_older_commit(cfg):
    repo_fragment, coverage = _tiny_pr()
    repo_fragment.reviews = [
        Review(reviewer="rdolan", state="APPROVED", submitted_at=T0 + timedelta(minutes=20), commit_id=OLD_SHA),
    ]
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)
    assert built.review.stale_approval is True


def test_no_approvers_is_not_stale(cfg):
    repo_fragment, coverage = _tiny_pr()
    repo_fragment.reviews = []
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)
    assert built.review.stale_approval is False
    assert built.review.approvers == []


def test_ask_to_approve_uses_the_latest_of_request_and_commit(cfg):
    """The reviewer cannot have spent longer than the time since it last changed."""
    repo_fragment, coverage = _tiny_pr()
    approved_at = T0 + timedelta(minutes=60)
    repo_fragment.review_requests = [ReviewRequest(reviewer="rdolan", requested_at=T0 + timedelta(minutes=5))]
    repo_fragment.commits.append(
        CommitRef(sha=SHA, message="m", author_login="dev", author_email="dev@acme.example",
                  committed_at=T0 + timedelta(minutes=50))
    )
    repo_fragment.reviews = [
        Review(reviewer="rdolan", state="APPROVED", submitted_at=approved_at, commit_id=SHA)
    ]
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)

    assert built.review.approvers[0].ask_to_approve_s == 600      # the commit, not the request


def test_ask_to_approve_falls_back_to_pr_creation(cfg):
    repo_fragment, coverage = _tiny_pr()
    repo_fragment.review_requests = []
    repo_fragment.commits = []
    repo_fragment.reviews = [
        Review(reviewer="rdolan", state="APPROVED", submitted_at=T0 + timedelta(seconds=90), commit_id=SHA)
    ]
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)
    assert built.review.approvers[0].ask_to_approve_s == 90


def test_ask_to_approve_is_floored_at_zero(cfg):
    repo_fragment, coverage = _tiny_pr()
    repo_fragment.review_requests = []
    repo_fragment.commits = []
    repo_fragment.created_at = T0 + timedelta(minutes=5)
    repo_fragment.reviews = [
        Review(reviewer="rdolan", state="APPROVED", submitted_at=T0, commit_id=SHA)
    ]
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)
    assert built.review.approvers[0].ask_to_approve_s == 0


def test_files_commented_and_comment_count(cfg):
    repo_fragment, coverage = _tiny_pr()
    repo_fragment.review_comments = [
        ReviewComment(reviewer="rdolan", path="b.py", created_at=T0),
        ReviewComment(reviewer="rdolan", path="a.py", created_at=T0),
        ReviewComment(reviewer="mkeane", path="a.py", created_at=T0),
    ]
    repo_fragment.reviews = [
        Review(reviewer="mkeane", state="COMMENTED", submitted_at=T0, commit_id=SHA),
        Review(reviewer="rdolan", state="DISMISSED", submitted_at=T0, commit_id=SHA),
    ]
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)

    assert built.review.files_commented == ["a.py", "b.py"]
    assert built.review.comments == 4


# --- verify / deploy -------------------------------------------------------

def test_verify_present_without_coverage_but_with_ci(cfg):
    repo_fragment, _cov = _tiny_pr()
    coverage = CoverageFragment(head_sha=SHA, ci_status="failure", present=False, source="none", provenance="real")
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)

    assert built.verify.present is True
    assert built.verify.ci_status == "failure"
    assert built.verify.coverage_by_line == {}


def test_verify_absent_when_nothing_is_known(cfg):
    repo_fragment, _cov = _tiny_pr()
    coverage = CoverageFragment(head_sha=SHA, ci_status="unknown", present=False, source="none", provenance="real")
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)
    assert built.verify.present is False


def test_coverage_provenance_is_carried(cfg):
    """RFC 0 rule 7: simulated data stays labelled."""
    repo_fragment, _cov = _tiny_pr()
    coverage = CoverageFragment(head_sha=SHA, ci_status="success", present=True, source="simulated", provenance="simulated")
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)
    assert built.verify.provenance == "simulated"


@pytest.mark.parametrize(
    "labels,expected",
    [
        ({}, "none"),
        ({10: "unknown", 11: "unknown"}, "unknown"),
        ({10: "ai", 11: "ai", 12: "mixed", 13: "ai", 14: "human"}, "ai"),
        ({10: "human", 11: "human", 12: "human", 13: "human", 14: "ai"}, "human"),
        ({10: "ai", 11: "ai", 12: "human", 13: "human", 14: "human"}, "mixed"),
    ],
)
def test_tests_authored_by(cfg, labels, expected):
    repo_fragment, coverage = _tiny_pr()
    attribution = AttributionResult(
        attribution_by_line={"a.py": {10: "ai"}, "tests/test_a.py": labels}
    )
    built = build_code(repo_fragment, [], attribution, coverage, cfg)
    assert built.verify.tests_authored_by == expected


def test_deploy_follows_config(cfg):
    repo_fragment, coverage = _tiny_pr()
    assert build_code(repo_fragment, [], AttributionResult(), coverage, cfg).deploy.present is False

    with_target = cfg.model_copy(deep=True)
    with_target.deploy.default_target = "prod-eu"
    with_target.deploy.default_window = "Tue 02:00 UTC"
    deploy = build_code(repo_fragment, [], AttributionResult(), coverage, with_target).deploy
    assert (deploy.present, deploy.target, deploy.window) == (True, "prod-eu", "Tue 02:00 UTC")


def test_intent_absent_without_a_linked_issue(cfg):
    repo_fragment, coverage = _tiny_pr()
    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)
    assert built.intent.present is False
    assert built.intent.scope_paths == []


# ===========================================================================
# the no-code entry point
# ===========================================================================

def test_build_non_code_matches_the_golden_record(cfg):
    change = fragments("model-bump")["non_code_change"]
    built = build_non_code(change, cfg)
    expected = record("model-bump")

    assert built.change_key == expected.change_key == "mf-resolution-agent-cccccccccccc"
    assert built.kind == expected.kind == "model_version"
    assert built.title == expected.title
    assert built.created_at == expected.created_at
    assert built.source_url is None
    assert built.intent.present is False and built.intent.source == "manifest-change-ref"
    assert built.sessions == []
    assert (built.join.method, built.join.confidence) == ("none", "unmatched")
    assert built.diff.present is False and built.review.present is False
    assert built.deploy.present is False
    assert built.verify.present is True
    assert built.verify.rehearsal == change.rehearsal
    assert built.verify.provenance == "simulated"
    assert built.manifest.present is True
    assert built.manifest.changed_keys == ["model"]
    assert built.manifest.previous_available is True
    assert built.manifest.before == change.before and built.manifest.after == change.after


def test_build_non_code_without_a_previous_manifest(cfg):
    change = fragments("model-bump")["non_code_change"]
    first_sighting = change.model_copy(update={"before": None, "rehearsal": None})

    built = build_non_code(first_sighting, cfg)

    assert built.manifest.previous_available is False
    assert built.verify.present is False
    assert "? → vendor-4.2" in built.title


def test_build_non_code_intent_from_change_ref(cfg):
    change = fragments("model-bump")["non_code_change"]
    after = change.after.model_copy(deep=True)
    after.manifest.change_ref = "CHG-4412"
    built = build_non_code(change.model_copy(update={"after": after}), cfg)

    assert built.intent.present is True
    assert built.intent.source == "manifest-change-ref"
    assert built.intent.ref == "CHG-4412"


# ===========================================================================
# helpers
# ===========================================================================

def _call(tid: str, source: str | None) -> ToolCall:
    return ToolCall(
        tool_use_id=tid, prompt_id="p1", name="Edit", display_name="Edit",
        success=True, decision_source=source, timestamp=T0,
    )


def _tiny_pr() -> tuple[RepoFragment, CoverageFragment]:
    repo_fragment = RepoFragment(
        repo="acme/provisioning-service",
        pr_number=7,
        title="Tiny",
        body="b",
        url="https://github.com/acme/provisioning-service/pull/7",
        author_login="dev",
        base_ref="main",
        head_ref="feature",
        head_sha=SHA,
        created_at=T0,
        merged=False,
        commits=[
            CommitRef(sha=OLD_SHA, message="m", author_login="dev",
                      author_email="dev@acme.example", committed_at=T0)
        ],
        files=[
            ChangedFile(path="a.py", status="modified", additions=1, deletions=0, patch_present=True),
            ChangedFile(path="tests/test_a.py", status="added", additions=1, deletions=0, patch_present=True),
        ],
        reviews=[],
        review_requests=[],
        review_comments=[],
        linked_issue=None,
    )
    coverage = CoverageFragment(
        head_sha=SHA, ci_status="success", present=True, source="github-artifact", provenance="real"
    )
    return repo_fragment, coverage


def _tiny_session() -> SessionFragment:
    from docket.models.fragments import Turn

    return SessionFragment(
        session_id="s1",
        models=["claude-sonnet-5"],
        repo_root="/home/dev/repo",
        started_at=T0,
        ended_at=T0 + timedelta(hours=1),
        turns=[Turn(prompt_id="p1", started_at=T0, prompt_text="do the thing", prompt_length=12)],
        detail=DetailLevel(prompts=True, tool_details=True, edits=True),
    )


# ===========================================================================
# provenance (P8: honest labelling lives in the schema)
# ===========================================================================

def test_provenance_is_propagated_into_every_section(cfg):
    """A replayed `demo/seed/` change must never claim to be real (RFC 0 rule 7,
    RFC section 14). The UI reads these flags to draw the "simulated" badge."""
    repo_fragment, _cov = _tiny_pr()
    repo_fragment = repo_fragment.model_copy(update={"provenance": "simulated"})
    repo_fragment.linked_issue = _tiny_issue()
    coverage = CoverageFragment(
        head_sha=SHA, ci_status="success", present=True, source="simulated", provenance="simulated"
    )
    session = _tiny_session().model_copy(update={"provenance": "simulated"})
    joins = [JoinResult(session_id="s1", session=session, method="sha", confidence="claimed")]

    with_target = cfg.model_copy(deep=True)
    with_target.deploy.default_target = "prod-eu"

    built = build_code(repo_fragment, joins, AttributionResult(), coverage, with_target)

    assert built.intent.provenance == "simulated"
    assert built.diff.provenance == "simulated"
    assert built.review.provenance == "simulated"
    assert built.verify.provenance == "simulated"
    assert built.sessions[0].provenance == "simulated"
    # deploy comes from config, not from a fragment, so it is real
    assert built.deploy.present is True
    assert built.deploy.provenance == "real"


def test_intent_provenance_is_carried_even_when_absent(cfg):
    repo_fragment, coverage = _tiny_pr()
    repo_fragment = repo_fragment.model_copy(update={"provenance": "simulated"})

    built = build_code(repo_fragment, [], AttributionResult(), coverage, cfg)

    assert built.intent.present is False
    assert built.intent.provenance == "simulated"


def test_a_ghost_session_stays_real_in_a_simulated_pr(cfg):
    """A ghost is a real finding about a real commit: it has no fragment to inherit from."""
    repo_fragment, coverage = _tiny_pr()
    repo_fragment = repo_fragment.model_copy(update={"provenance": "simulated"})
    ghost = JoinResult(session_id="gone", session=None, method="trailer", confidence="claimed")

    built = build_code(repo_fragment, [ghost], AttributionResult(), coverage, cfg)

    assert built.sessions[0].present is False
    assert built.sessions[0].provenance == "real"


def test_each_session_keeps_its_own_provenance(cfg):
    repo_fragment, coverage = _tiny_pr()
    real = _tiny_session()
    simulated = _tiny_session().model_copy(update={"session_id": "s2", "provenance": "simulated"})
    joins = [
        JoinResult(session_id="s1", session=real, method="sha", confidence="claimed"),
        JoinResult(session_id="s2", session=simulated, method="sha", confidence="claimed"),
    ]

    built = build_code(repo_fragment, joins, AttributionResult(), coverage, cfg)

    assert [s.provenance for s in built.sessions] == ["real", "simulated"]


@pytest.mark.parametrize("name", ["held", "cleared", "unmatched"])
def test_the_real_fixtures_stay_real(name, cfg):
    """RFC 15.2 reads the raw scores, so no golden fixture may be degraded."""
    built, _joins, _attr = run_plane_two(name, cfg)

    assert built.intent.provenance == "real"
    assert built.diff.provenance == "real"
    assert built.review.provenance == "real"
    assert built.verify.provenance == "real"
    assert all(s.provenance == "real" for s in built.sessions)


def test_non_code_provenance_is_propagated(cfg):
    change = fragments("model-bump")["non_code_change"]
    simulated = change.model_copy(update={"provenance": "simulated"})

    built = build_non_code(simulated, cfg)

    assert built.intent.provenance == "simulated"
    assert built.diff.provenance == "simulated"
    assert built.review.provenance == "simulated"
    # verify follows the rehearsal, which is the thing that was simulated
    assert built.verify.provenance == change.rehearsal.provenance == "simulated"


def _tiny_issue():
    from docket.models.fragments import LinkedIssue

    return LinkedIssue(
        number=9, title="T", body="B\nScope: a.py\n",
        url="https://github.com/acme/provisioning-service/issues/9",
        scope_paths=["a.py"],
    )
