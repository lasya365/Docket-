"""Record Builder: assemble the ChangeRecord plane 3 reads (RFC 8.3).

No I/O and no clock read: `created_at` comes from the fragment, never from now().
`config` is the plain DocketConfig object, passed in by the pipeline.

Assumptions written down as RFC section 0 rule 2 asks:
  * RFC 8.2 step 9 raises a join to `verified`, but `attribute()` only receives an
    AttributorConfig and `verify_min_lines` lives in the correlator block. So
    `build_code` applies it (via `line_attributor.raise_confidence`) before
    reading confidences; the function is public so the attributor's own tests can
    call it. Calling it twice is harmless.
  * "plus modes seen in hook payloads" (RFC 8.3): SessionFragment carries no hook
    payload beyond `permission_mode_changes`, so those are the only modes there
    are to read. The dedup-in-order rule is implemented as written.
  * "best method among the sessions" is read as the method of the join with the
    best confidence; ties break in the order sha > trailer > time > none.
  * `ask_to_approve_s` uses the reviewer's latest review request at or before the
    approval; with none, the PR's `created_at`, exactly as RFC 8.3 says.
  * A note-only JoinResult (see types.py) makes no SessionSection; its note is
    folded into `join.notes`.
"""

from __future__ import annotations

import re
from datetime import datetime

import pathspec

from docket.models.change_record import (
    ApproverInfo,
    ChangeRecord,
    DeploySection,
    DiffSection,
    IntentSection,
    JoinSection,
    ManifestSection,
    ReviewSection,
    SessionSection,
    VerifySection,
)
from docket.models.common import Attribution, JoinConfidence, JoinMethod
from docket.models.config import DocketConfig
from docket.models.fragments import CoverageFragment, RepoFragment, SessionFragment
from docket.models.manifest import NonCodeChange
from docket.correlate.line_attributor import raise_confidence
from docket.correlate.types import AttributionResult, DiscardInfo, JoinResult

# ---------------------------------------------------------------------------
# RFC 17.3: nothing leaves Docket before these patterns are replaced.
# ---------------------------------------------------------------------------

SCRUB_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"[A-Fa-f0-9]{40,}"),                    # a long hex run
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),            # a long base64 run
)

REDACTED = "[redacted]"


def scrub(text: str) -> str:
    """Replace every RFC 17.3 pattern with `[redacted]`. Prompts can contain secrets."""
    if not text:
        return ""
    for pattern in SCRUB_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_CONFIDENCE_ORDER: dict[JoinConfidence, int] = {
    "verified": 3, "claimed": 2, "inferred": 1, "unmatched": 0,
}
_METHOD_ORDER: dict[JoinMethod, int] = {"sha": 3, "trailer": 2, "time": 1, "none": 0}
_LABELS: tuple[Attribution, ...] = ("ai", "human", "mixed", "unknown")


def change_key_for_pr(repo: str, number: int) -> str:
    """RFC 8.3: `pr-{owner}-{repo}-{number}`, lowercase, non-alphanumerics -> `-`."""
    owner, _, name = repo.partition("/")
    return re.sub(r"[^a-z0-9]", "-", f"pr-{owner}-{name}-{number}".lower())


def change_key_for_manifest(agent_id: str, manifest_hash: str) -> str:
    return f"mf-{agent_id}-{manifest_hash[:12]}"


def _spec(globs: list[str]) -> pathspec.PathSpec:
    return pathspec.PathSpec.from_lines("gitwildmatch", globs)


def _permission_modes(session: SessionFragment) -> list[str]:
    modes: list[str] = []
    for i, change in enumerate(session.permission_mode_changes):
        if i == 0 and change.from_mode:
            modes.append(change.from_mode)
        if change.to_mode:
            modes.append(change.to_mode)
    out: list[str] = []
    for mode in modes:
        if mode not in out:
            out.append(mode)
    return out


def _approvals_by(session: SessionFragment) -> dict[str, int]:
    counts = {"rule": 0, "hook": 0, "human": 0}
    for call in session.tool_calls:
        source = call.decision_source
        if source == "config":
            counts["rule"] += 1
        elif source == "hook":
            counts["hook"] += 1
        elif source in ("user_permanent", "user_temporary"):
            counts["human"] += 1
    return counts


def _session_section(join: JoinResult, attribution: AttributionResult, config: DocketConfig) -> SessionSection:
    if join.session is None:            # a ghost (RFC 8.1)
        return SessionSection(
            present=False,
            session_id=join.session_id,
            join_method=join.method,
            join_confidence=join.confidence,
            matched_commits=list(join.matched_commits),
        )

    s = join.session
    discard = attribution.discarded_by_session.get(s.session_id, DiscardInfo())
    limit = config.brief.prompt_excerpt_chars
    return SessionSection(
        present=True,
        session_id=s.session_id,
        agent=s.agent,
        model=s.models[0] if s.models else None,
        turns=len(s.turns),
        retries=s.api_retries,
        files_written=list(attribution.files_written_by_session.get(s.session_id, [])),
        files_discarded=list(discard.files),
        lines_written=sum(len(e.written_lines) for e in s.edit_events),
        lines_discarded=discard.lines,
        tools_used=list(s.tool_calls),
        permission_modes=_permission_modes(s),
        approvals_by=_approvals_by(s),
        sandbox_disabled_calls=sum(1 for c in s.tool_calls if c.sandbox_disabled),
        prompt_excerpts=[scrub(t.prompt_text or "")[:limit] for t in s.turns],
        detail=s.detail,
        join_method=join.method,
        join_confidence=join.confidence,
        matched_commits=list(join.matched_commits),
        provenance=s.provenance,
    )


def _latest_approvals(repo_fragment: RepoFragment) -> list:
    """Each reviewer's latest APPROVED review, in order of first appearance."""
    order: list[str] = []
    latest: dict[str, object] = {}
    for review in repo_fragment.reviews:
        if review.state != "APPROVED":
            continue
        if review.reviewer not in latest:
            order.append(review.reviewer)
            latest[review.reviewer] = review
        elif review.submitted_at >= latest[review.reviewer].submitted_at:   # type: ignore[union-attr]
            latest[review.reviewer] = review
    return [latest[name] for name in order]


def _ask_to_approve_s(repo_fragment: RepoFragment, reviewer: str, approved_at: datetime) -> int:
    """RFC 8.3: an upper bound on reading time, floored at 0."""
    asked = [r.requested_at for r in repo_fragment.review_requests if r.reviewer == reviewer]
    before = [t for t in asked if t <= approved_at]
    baseline = max(before) if before else repo_fragment.created_at
    commits = [c.committed_at for c in repo_fragment.commits if c.committed_at <= approved_at]
    if commits:
        baseline = max(baseline, max(commits))
    return max(0, int((approved_at - baseline).total_seconds()))


def _review_section(
    repo_fragment: RepoFragment, section_provenance: dict[str, str] | None = None
) -> ReviewSection:
    approvals = _latest_approvals(repo_fragment)
    approvers = [
        ApproverInfo(
            reviewer=r.reviewer,
            approved_at=r.submitted_at,
            approved_commit=r.commit_id,
            ask_to_approve_s=_ask_to_approve_s(repo_fragment, r.reviewer, r.submitted_at),
        )
        for r in approvals
    ]
    stale = bool(approvers) and not any(a.approved_commit == repo_fragment.head_sha for a in approvers)
    engaged = sum(1 for r in repo_fragment.reviews if r.state in ("COMMENTED", "CHANGES_REQUESTED"))
    return ReviewSection(
        present=True,
        approvers=approvers,
        stale_approval=stale,
        files_changed=len(repo_fragment.files),
        files_commented=sorted({c.path for c in repo_fragment.review_comments}),
        comments=len(repo_fragment.review_comments) + engaged,
        provenance=_section_provenance(section_provenance, "review", repo_fragment.provenance),
    )


def _tests_authored_by(attribution: AttributionResult, config: DocketConfig) -> str:
    """RFC 8.3: the labels of added lines in files matching `signals.untested.test_globs`."""
    spec = _spec(config.signals.untested.test_globs)
    counts: dict[str, int] = {label: 0 for label in _LABELS}
    total = 0
    for path, lines in attribution.attribution_by_line.items():
        if not spec.match_file(path):
            continue
        for label in lines.values():
            counts[label] += 1
            total += 1
    if total == 0:
        return "none"
    if counts["unknown"] == total:
        return "unknown"
    if counts["ai"] + counts["mixed"] >= 0.8 * total:
        return "ai"
    if counts["human"] >= 0.8 * total:
        return "human"
    return "mixed"


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def _section_provenance(overrides: dict[str, str] | None, section: str, default: str) -> str:
    """One section's provenance, with an optional per-section override (RFC P8).

    A collector whose evidence is part real and part staged - a local git branch
    with a staged reviewer, say - cannot say so with the fragment's single
    `provenance` field. It names the staged sections instead, and the real ones
    keep their real label. An unknown value is ignored rather than trusted.
    """
    value = (overrides or {}).get(section)
    return value if value in ("real", "simulated") else default


def build_code(
    repo_fragment: RepoFragment,
    joins: list[JoinResult],
    attribution: AttributionResult,
    coverage: CoverageFragment,
    config: DocketConfig,
    section_provenance: dict[str, str] | None = None,
) -> ChangeRecord:
    # RFC 8.2 step 9, applied here because verify_min_lines lives in the correlator block.
    raise_confidence(joins, attribution, config.correlator.verify_min_lines)

    real_joins = [j for j in joins if j.is_join]
    sessions = [_session_section(j, attribution, config) for j in real_joins]

    notes = [j.note for j in joins if j.note]
    notes.extend(attribution.notes)

    if real_joins:
        best = max(
            real_joins,
            key=lambda j: (_CONFIDENCE_ORDER.get(j.confidence, 0), _METHOD_ORDER.get(j.method, 0)),
        )
        join = JoinSection(
            method=best.method,
            confidence=best.confidence,
            human_declared=config.gate.human_declared_label in repo_fragment.labels,
            notes=notes,
        )
    else:
        join = JoinSection(
            method="none",
            confidence="unmatched",
            human_declared=config.gate.human_declared_label in repo_fragment.labels,
            notes=notes,
        )

    intent_provenance = _section_provenance(section_provenance, "intent", repo_fragment.provenance)
    intent = IntentSection(present=False, provenance=intent_provenance)
    issue = repo_fragment.linked_issue
    if issue is not None:
        intent = IntentSection(
            present=True,
            source="github-issue",
            ref=f"#{issue.number}",
            url=issue.url,
            text=f"{issue.title}\n\n{issue.body}",
            scope_paths=list(issue.scope_paths),
            provenance=intent_provenance,
        )

    lines_added = sum(len(h.added) for f in repo_fragment.files for h in f.hunks)
    totals: dict[Attribution, int] = {label: attribution.totals.get(label, 0) for label in _LABELS}

    deploy_target = config.deploy.default_target
    deploy = DeploySection(
        present=deploy_target is not None,
        target=deploy_target,
        window=config.deploy.default_window if deploy_target is not None else None,
    )

    return ChangeRecord(
        change_key=change_key_for_pr(repo_fragment.repo, repo_fragment.pr_number),
        kind="code",
        title=repo_fragment.title,
        source_url=repo_fragment.url,
        created_at=repo_fragment.created_at,
        intent=intent,
        sessions=sessions,
        join=join,
        diff=DiffSection(
            present=True,
            files=list(repo_fragment.files),
            lines_added=lines_added,
            attribution_by_line=attribution.attribution_by_line,
            hunk_attribution=list(attribution.hunk_attribution),
            totals=totals,
            provenance=_section_provenance(section_provenance, "diff", repo_fragment.provenance),
        ),
        review=_review_section(repo_fragment, section_provenance),
        verify=VerifySection(
            present=coverage.present or coverage.ci_status != "unknown",
            ci_status=coverage.ci_status,
            coverage_by_line=dict(coverage.files),
            tests_authored_by=_tests_authored_by(attribution, config),
            rehearsal=None,
            provenance=_section_provenance(section_provenance, "verify", coverage.provenance),
        ),
        deploy=deploy,
        manifest=ManifestSection(present=False),
    )


def _non_code_title(change: NonCodeChange) -> str:
    parts: list[str] = []
    for key in change.changed_keys:
        after = getattr(change.after.manifest, key, None)
        before = getattr(change.before.manifest, key, None) if change.before else None
        if isinstance(after, str) or after is None:
            parts.append(f"{key} {before if isinstance(before, str) else '?'} → {after or '?'}")
        else:
            parts.append(f"{key} changed")
    return f"{change.agent_id}: " + ", ".join(parts) if parts else f"{change.agent_id}: manifest changed"


def build_non_code(change: NonCodeChange, config: DocketConfig) -> ChangeRecord:
    change_ref = change.after.manifest.change_ref
    rehearsal = change.rehearsal
    return ChangeRecord(
        change_key=change_key_for_manifest(change.agent_id, change.after.manifest_hash),
        kind=change.kind,
        title=_non_code_title(change),
        source_url=None,
        created_at=change.detected_at,
        intent=IntentSection(
            present=change_ref is not None,
            source="manifest-change-ref",
            ref=change_ref,
            text=change_ref,
            provenance=change.provenance,
        ),
        sessions=[],
        join=JoinSection(method="none", confidence="unmatched"),
        diff=DiffSection(present=False, provenance=change.provenance),
        review=ReviewSection(present=False, provenance=change.provenance),
        verify=VerifySection(
            present=rehearsal is not None,
            ci_status="unknown",
            rehearsal=rehearsal,
            provenance=rehearsal.provenance if rehearsal else change.provenance,
        ),
        # A no-code change has no planned window: it is already live (RFC 8.3).
        deploy=DeploySection(present=False),
        manifest=ManifestSection(
            present=True,
            agent_id=change.agent_id,
            changed_keys=list(change.changed_keys),
            before=change.before,
            after=change.after,
            previous_available=change.before is not None,
        ),
    )
