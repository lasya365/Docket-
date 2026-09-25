"""The Backout Planner (RFC section 9.8).

Pure: standard library and docket.models only. No clock, no I/O, no LLM.

Assumptions written down as RFC section 0 rule 2 asks:
  * A ChangeRecord carries no head SHA and no base ref of its own, so the short SHA
    in `naive_plan` is taken from the first available of a session's matched commits
    or an approver's approved commit, and the rollout line says "the base branch"
    when no base ref is on record.
  * A backout step reads after → before: `from_value` is what is live now, `to_value`
    is what the board would restore. For list-shaped keys (tools, permissions,
    knowledge) the values are the whole list, and the text line also spells out the
    delta, which is what RFC 9.8's `−entitlement.grant` example shows.
  * The pull-request number is read off `change_key` ("pr-{owner}-{repo}-{n}").
"""

from __future__ import annotations

from docket.models.change_record import ChangeRecord
from docket.models.manifest import ManifestSnapshot
from docket.models.signals import BackoutPlan, BackoutStep

LIST_KEYS = ("tools", "permissions", "knowledge", "workflows")


def _pr_number(record: ChangeRecord) -> str:
    tail = record.change_key.rsplit("-", 1)[-1]
    return tail if record.change_key.startswith("pr-") and tail.isdigit() else "?"


def _head_sha(record: ChangeRecord) -> str:
    for session in record.sessions:
        if session.matched_commits:
            return session.matched_commits[-1]
    for approver in record.review.approvers:
        if approver.approved_commit:
            return approver.approved_commit
    return ""


def _values(snapshot: ManifestSnapshot | None, key: str) -> list[str]:
    if snapshot is None:
        return []
    manifest = snapshot.manifest
    if key == "tools":
        return list(manifest.tools)
    if key == "permissions":
        return list(manifest.permissions)
    if key == "knowledge":
        return [f"{k.id} r{k.revision}" for k in manifest.knowledge]
    if key == "workflows":
        return list(manifest.workflows)
    return []


def _scalar(snapshot: ManifestSnapshot | None, key: str) -> str:
    if snapshot is None:
        return "none"
    manifest = snapshot.manifest
    if key == "model":
        return manifest.model
    if key == "prompt":
        return manifest.prompt.sha256[:12] or (manifest.prompt.path or "none")
    return snapshot.manifest_hash[:12]


def _render(snapshot: ManifestSnapshot | None, key: str) -> str:
    if key in LIST_KEYS:
        return ", ".join(_values(snapshot, key)) or "none"
    return _scalar(snapshot, key)


def _delta(before: ManifestSnapshot | None, after: ManifestSnapshot | None, key: str) -> str:
    if key not in LIST_KEYS:
        return ""
    before_values = _values(before, key)
    after_values = _values(after, key)
    parts = [f"-{v}" for v in after_values if v not in before_values]
    parts += [f"+{v}" for v in before_values if v not in after_values]
    return ", ".join(parts)


def _non_code_plan(record: ChangeRecord) -> BackoutPlan:
    manifest = record.manifest
    before, after = manifest.before, manifest.after

    if before is None or after is None:
        text = (
            "No earlier bundle is on record. This is the first time Docket has seen "
            "this agent."
        )
        return BackoutPlan(
            available=False,
            summary="No backout is possible: there is no earlier bundle to restore.",
            steps=[],
            naive_plan=None,
            why_naive_fails=None,
            text=text,
        )

    keys = list(manifest.changed_keys) or ["model"]
    steps = [
        BackoutStep(key=key, from_value=_render(after, key), to_value=_render(before, key))
        for key in keys
    ]
    summary = f"Restore agent bundle #{after.sequence} → #{before.sequence}"
    lines = [summary]
    for step in steps:
        delta = _delta(before, after, step.key)
        suffix = f"  ({delta})" if delta else ""
        lines.append(f"{step.key}: {step.from_value} → {step.to_value}{suffix}")
    lines.append(
        f"Agent {manifest.agent_id or 'unknown'}: apply bundle #{before.sequence} "
        f"({before.manifest_hash[:12]}) and restart every workflow that uses it."
    )
    return BackoutPlan(
        available=True,
        summary=summary,
        steps=steps,
        naive_plan=None,
        why_naive_fails=None,
        text="\n".join(lines),
    )


def _code_plan(record: ChangeRecord) -> BackoutPlan:
    number = _pr_number(record)
    sha = _head_sha(record)
    short = sha[:7] if sha else "unknown sha"
    naive = f"Revert PR #{number} ({short})"
    step = BackoutStep(
        key="code",
        from_value=f"PR #{number} at {short}",
        to_value="the branch as it stood before the merge",
    )
    summary = f"Revert PR #{number} and restore the previous state of the branch."

    why = None
    present_sessions = [s for s in record.sessions if s.present]
    if present_sessions:
        session = present_sessions[0]
        why = (
            "Reverting the commit does not change the agent that wrote it: "
            f"{session.agent or 'unknown agent'}, model {session.model or 'unknown'}. "
            "If the agent's bundle changed recently, restore that as well."
        )

    lines = [summary, f"code: {step.from_value} → {step.to_value}", naive]
    if why:
        lines.append(why)
    return BackoutPlan(
        available=True,
        summary=summary,
        steps=[step],
        naive_plan=naive,
        why_naive_fails=why,
        text="\n".join(lines),
    )


def plan_backout(record: ChangeRecord) -> BackoutPlan:
    """RFC 9.8. Never raises on a missing section."""
    if record.kind == "code":
        return _code_plan(record)
    return _non_code_plan(record)


def rollout_text(record: ChangeRecord) -> str:
    """RFC 9.8. The plain-text rollout plan for the Freshservice field."""
    deploy = record.deploy
    if record.kind == "code":
        text = f"Merge PR #{_pr_number(record)} into the base branch after CAB approval."
        if deploy.present:
            if deploy.target:
                text += f" Target: {deploy.target}."
            if deploy.window:
                text += f" Window: {deploy.window}."
        return text

    manifest = record.manifest
    sequence = manifest.after.sequence if manifest.after else "?"
    workflows = list(manifest.after.manifest.workflows) if manifest.after else []
    text = (
        f"Apply bundle #{sequence} to {manifest.agent_id or 'the agent'}. "
        f"Affects: {', '.join(workflows) if workflows else 'no workflow on record'}."
    )
    if deploy.present:
        if deploy.target:
            text += f" Target: {deploy.target}."
        if deploy.window:
            text += f" Window: {deploy.window}."
    return text
