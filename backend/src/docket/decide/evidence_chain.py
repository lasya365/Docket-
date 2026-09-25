"""The Evidence Chain (RFC section 9.7).

Six links, always in the order Intent, Session, Diff, Review, Verify, Deploy. A link
is present or MISSING, nothing in between (P7).

Pure: standard library and docket.models only. No clock, no I/O, no LLM.

Assumptions written down as RFC section 0 rule 2 asks:
  * The pull-request number is read off `change_key` ("pr-{owner}-{repo}-{n}"), the
    only place a ChangeRecord carries it; a key of another shape gives "?".
  * The AI-line coverage in the Verify link is taken from the untested signal's
    evidence (`ai_coverage_pct`) rather than recomputed, so the card and the chain
    can never disagree.
  * `evidence_refs` point at the signal most related to the link: intent and diff to
    unattributed, session to blast_radius, review to review_depth, verify to
    untested. Deploy has no signal, so it has no refs.
"""

from __future__ import annotations

from docket.models.change_record import ChangeRecord
from docket.models.signals import ChainLink, EvidenceChain, SignalResult


def _pr_number(record: ChangeRecord) -> str:
    tail = record.change_key.rsplit("-", 1)[-1]
    return tail if record.change_key.startswith("pr-") and tail.isdigit() else "?"


def _refs(signals: list[SignalResult], name: str) -> list[str]:
    for signal in signals:
        if signal.signal == name:
            return [item.id for item in signal.evidence]
    return []


def _signal(signals: list[SignalResult], name: str) -> SignalResult | None:
    for signal in signals:
        if signal.signal == name:
            return signal
    return None


def _format_duration(seconds: int | float) -> str:
    total = int(seconds)
    minutes, secs = divmod(max(total, 0), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _ai_coverage_pct(signals: list[SignalResult]) -> float | None:
    signal = _signal(signals, "untested")
    if signal is None:
        return None
    for item in signal.evidence:
        value = item.data.get("ai_coverage_pct")
        if value is not None and "ai_executable" in item.data:
            return float(value)
    return None


def _intent_link(record: ChangeRecord, signals: list[SignalResult]) -> ChainLink:
    intent = record.intent
    refs = _refs(signals, "unattributed")
    if intent.present:
        text = (intent.text or "").strip().replace("\n", " ")
        return ChainLink(
            name="intent",
            present=True,
            summary=f'{intent.ref or intent.source or "intent"} · "{text[:60]}"',
            detail=f"{intent.source or 'unknown source'} {intent.url or ''}".strip(),
            evidence_refs=refs,
        )
    return ChainLink(
        name="intent",
        present=False,
        summary="none: no requirement exists",
        detail="No ticket, issue or change reference asked for this change.",
        evidence_refs=refs,
    )


def _session_link(record: ChangeRecord, signals: list[SignalResult]) -> ChainLink:
    refs = _refs(signals, "blast_radius")
    if record.kind != "code":
        manifest = record.manifest
        if not manifest.present:
            return ChainLink(
                name="session",
                present=False,
                summary="none: no agent bundle on record",
                evidence_refs=refs,
            )
        key = manifest.changed_keys[0] if manifest.changed_keys else "bundle"
        before = _bundle_value(manifest.before, key)
        after = _bundle_value(manifest.after, key)
        return ChainLink(
            name="session",
            present=True,
            summary=f"manifest · {key}: {before} → {after}",
            detail=(
                f"{manifest.agent_id or 'agent'} bundle "
                f"#{manifest.before.sequence if manifest.before else '?'} → "
                f"#{manifest.after.sequence if manifest.after else '?'}"
            ),
            evidence_refs=refs,
        )

    present_sessions = [s for s in record.sessions if s.present]
    ghosts = [s for s in record.sessions if not s.present]
    ghost_text = (
        f" {len(ghosts)} ghost session(s) were seen but could not be joined: "
        f"{', '.join(g.session_id for g in ghosts)}."
        if ghosts
        else ""
    )
    if present_sessions:
        agent = present_sessions[0].agent or "agent"
        turns = sum(s.turns for s in present_sessions)
        retries = sum(s.retries for s in present_sessions)
        files = sorted({p for s in present_sessions for p in s.files_written})
        return ChainLink(
            name="session",
            present=True,
            summary=(
                f"{agent} · {turns} turns · {retries} retries · {len(files)} files · "
                f"join {record.join.confidence}"
            ),
            detail=(
                f"{len(present_sessions)} session(s), joined by "
                f"{record.join.method}.{ghost_text}"
            ).strip(),
            evidence_refs=refs,
        )
    summary = (
        "declared human-authored"
        if record.join.human_declared
        else "UNMATCHED: no agent session found"
    )
    return ChainLink(
        name="session",
        present=False,
        summary=summary,
        detail=(
            "A person put their name to this change; Docket saw no agent telemetry."
            if record.join.human_declared
            else "No telemetry could be joined to this change."
        )
        + ghost_text,
        evidence_refs=refs,
    )


def _bundle_value(snapshot, key: str) -> str:
    if snapshot is None:
        return "none"
    manifest = snapshot.manifest
    if key == "model":
        return manifest.model
    if key == "prompt":
        return manifest.prompt.sha256[:12] or (manifest.prompt.path or "none")
    if key == "tools":
        return ", ".join(manifest.tools) or "none"
    if key == "permissions":
        return ", ".join(manifest.permissions) or "none"
    if key == "knowledge":
        return ", ".join(f"{k.id} r{k.revision}" for k in manifest.knowledge) or "none"
    if key == "workflows":
        return ", ".join(manifest.workflows) or "none"
    return snapshot.manifest_hash[:12]


def _diff_link(record: ChangeRecord, signals: list[SignalResult]) -> ChainLink:
    refs = _refs(signals, "unattributed")
    if not record.diff.present:
        return ChainLink(
            name="diff",
            present=False,
            summary="none: nothing to review",
            detail="This change produced no code diff.",
            evidence_refs=refs,
        )
    totals = record.diff.totals
    ai = totals.get("ai", 0) + totals.get("mixed", 0)
    return ChainLink(
        name="diff",
        present=True,
        summary=(
            f"PR #{_pr_number(record)} · {record.diff.lines_added} lines · "
            f"{ai} AI-authored"
        ),
        detail=f"{len(record.diff.files)} changed files.",
        evidence_refs=refs,
    )


def _review_link(record: ChangeRecord, signals: list[SignalResult]) -> ChainLink:
    refs = _refs(signals, "review_depth")
    review = record.review
    if review.present and review.approvers:
        fastest = min(a.ask_to_approve_s for a in review.approvers)
        return ChainLink(
            name="review",
            present=True,
            summary=f"{len(review.approvers)} approvals · fastest {_format_duration(fastest)}",
            detail=(
                f"{review.comments} comments on {len(review.files_commented)} of "
                f"{review.files_changed} files"
                + (", every approval against an older commit" if review.stale_approval else "")
            ),
            evidence_refs=refs,
        )
    return ChainLink(
        name="review",
        present=False,
        summary="none: no reviewer, no approval",
        detail=(
            "A pull request exists but nobody approved it."
            if review.present
            else "There is no pull request to review."
        ),
        evidence_refs=refs,
    )


def _verify_link(record: ChangeRecord, signals: list[SignalResult]) -> ChainLink:
    refs = _refs(signals, "untested")
    verify = record.verify
    if not verify.present:
        return ChainLink(
            name="verify",
            present=False,
            summary="none: nothing verified",
            detail="No CI result, no coverage and no rehearsal reached Docket.",
            evidence_refs=refs,
        )
    if verify.rehearsal is not None:
        reh = verify.rehearsal
        return ChainLink(
            name="verify",
            present=True,
            summary=f"rehearsed · {reh.cases} cases · {reh.regressed} regressed",
            detail=(
                f"{reh.identical} identical, {reh.changed_acceptable} changed "
                f"acceptably, provenance {reh.provenance}"
            ),
            evidence_refs=refs,
        )
    pct = _ai_coverage_pct(signals)
    cov = "no" if pct is None else f"{pct:.0f}%"
    return ChainLink(
        name="verify",
        present=True,
        summary=f"CI {verify.ci_status} · {cov} on AI lines",
        detail=f"tests authored by {verify.tests_authored_by}",
        evidence_refs=refs,
    )


def _deploy_link(record: ChangeRecord) -> ChainLink:
    deploy = record.deploy
    if deploy.present:
        return ChainLink(
            name="deploy",
            present=True,
            summary=f"{deploy.target or 'unnamed target'} · window {deploy.window or 'none'}",
            detail="",
            evidence_refs=[],
        )
    extra = ""
    if record.kind != "code":
        workflows = (
            record.manifest.after.manifest.workflows if record.manifest.after else []
        )
        extra = f"; already live across {len(workflows)} workflows"
    return ChainLink(
        name="deploy",
        present=False,
        summary=f"no planned window{extra}",
        detail="",
        evidence_refs=[],
    )


def build_chain(record: ChangeRecord, signals: list[SignalResult]) -> EvidenceChain:
    """RFC 9.7. Always six links, always in order."""
    links = [
        _intent_link(record, signals),
        _session_link(record, signals),
        _diff_link(record, signals),
        _review_link(record, signals),
        _verify_link(record, signals),
        _deploy_link(record),
    ]
    return EvidenceChain(
        links=links,
        missing_count=sum(1 for link in links if not link.present),
    )
