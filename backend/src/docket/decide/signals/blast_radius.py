"""Signal 4 of 4: blast radius (RFC section 9.4).

Pure: standard library, pathspec and docket.models only. No clock, no I/O.

Assumptions written down as RFC section 0 rule 2 asks:
  * "each distinct tool call whose display_name matches sensitive.tools" is read as
    one charge per distinct `display_name`, the same way the path and bash rows charge
    once per distinct pattern. Charging per invocation would make the score depend on
    how chatty a session was rather than on what it reached.
  * The auto-approval ratio is summed over every present session's `approvals_by`,
    because a change is the work of all its sessions.
  * A no-code change is `degraded` when there is no previous snapshot to diff against
    (the added-tool row cannot be evaluated), otherwise `computed`.
"""

from __future__ import annotations

import fnmatch
import re

import pathspec

from docket.models.change_record import ChangeRecord
from docket.models.config import BlastRadiusConfig, SensitiveConfig
from docket.models.manifest import ManifestSnapshot
from docket.models.signals import EvidenceItem, SignalResult

SIGNAL = "blast_radius"
LABEL = "Blast radius"
BYPASS_MODE = "bypassPermissions"


def _add(
    evidence: list[EvidenceItem],
    kind: str,
    text: str,
    data: dict,
    source: str,
    provenance: str,
) -> EvidenceItem:
    item = EvidenceItem(
        id=f"ev:{SIGNAL}:{len(evidence) + 1}",
        kind=kind,
        text=text,
        data=data,
        source=source,  # type: ignore[arg-type]
        provenance=provenance,  # type: ignore[arg-type]
    )
    evidence.append(item)
    return item


def _result(
    score: float,
    status: str,
    headline: str,
    summary: str,
    evidence: list[EvidenceItem],
) -> SignalResult:
    return SignalResult(
        signal=SIGNAL,
        label=LABEL,
        score=round(float(min(max(score, 0.0), 100.0)), 2),
        status=status,  # type: ignore[arg-type]
        headline=headline,
        summary=summary,
        evidence=evidence,
    )


def _snapshot_values(snapshot: ManifestSnapshot | None) -> tuple[list[str], list[str]]:
    if snapshot is None:
        return [], []
    return list(snapshot.manifest.tools), list(snapshot.manifest.permissions)


def _non_code(
    record: ChangeRecord, cfg: BlastRadiusConfig, sensitive: SensitiveConfig
) -> SignalResult:
    evidence: list[EvidenceItem] = []
    manifest = record.manifest
    base = float(cfg.non_code_base.get(record.kind, 0.0))
    score = base

    _add(
        evidence,
        "manifest",
        f"A {record.kind.replace('_', ' ')} change to an agent bundle starts at "
        f"{base:.0f} points: it takes effect everywhere the agent runs at once.",
        {"kind": record.kind, "non_code_base": base, "changed_keys": list(manifest.changed_keys)},
        "manifest",
        "real",
    )

    after_tools, after_perms = _snapshot_values(manifest.after)
    before_tools, before_perms = _snapshot_values(manifest.before)
    added = [t for t in after_tools if t not in before_tools]
    added += [p for p in after_perms if p not in before_perms]

    hits: list[str] = []
    for item in added:
        for pattern in sensitive.tools:
            if fnmatch.fnmatch(item, pattern):
                hits.append(item)
                _add(
                    evidence,
                    "manifest",
                    f"The new bundle adds {item}, which the sensitive list covers "
                    f"({pattern}).",
                    {
                        "added": item,
                        "pattern": pattern,
                        "points": cfg.points.sensitive_tool,
                    },
                    "manifest",
                    "real",
                )
                break
    score += cfg.points.sensitive_tool * len(hits)

    workflows = list(manifest.after.manifest.workflows) if manifest.after else []
    _add(
        evidence,
        "manifest",
        f"The agent runs in {len(workflows)} workflows, all of which change at once: "
        f"{', '.join(workflows) if workflows else 'none on record'}.",
        {"workflows": workflows, "workflow_count": len(workflows)},
        "manifest",
        "real",
    )

    status = "computed" if (manifest.present and manifest.after and manifest.before) else "degraded"
    return _result(
        score,
        status,
        f"{len(workflows)} workflows",
        f"Changing {', '.join(manifest.changed_keys) or 'the bundle'} reaches "
        f"{len(workflows)} workflows at once, with no canary possible"
        + (f", and adds {len(hits)} sensitive capability/capabilities." if hits else "."),
        evidence,
    )


def compute(
    record: ChangeRecord, cfg: BlastRadiusConfig, sensitive: SensitiveConfig
) -> SignalResult:
    """RFC 9.4. Returns a SignalResult; never raises on a missing section."""
    if record.kind != "code":
        return _non_code(record, cfg, sensitive)

    evidence: list[EvidenceItem] = []
    points = cfg.points
    score = 0.0
    sessions = [s for s in record.sessions if s.present]

    # --- sensitive tool calls ----------------------------------------------
    seen: list[tuple[str, str, str]] = []   # (display_name, pattern, session_id)
    seen_names: set[str] = set()
    for session in sessions:
        for call in session.tools_used:
            if call.display_name in seen_names:
                continue
            for pattern in sensitive.tools:
                if fnmatch.fnmatch(call.display_name, pattern):
                    seen_names.add(call.display_name)
                    seen.append((call.display_name, pattern, session.session_id))
                    break
    for display_name, pattern, session_id in seen:
        score += points.sensitive_tool
        _add(
            evidence,
            "tool_call",
            f"The agent called {display_name}, which the sensitive tool list covers "
            f"({pattern}).",
            {
                "display_name": display_name,
                "pattern": pattern,
                "session_id": session_id,
                "points": points.sensitive_tool,
            },
            "session",
            "real",
        )

    # --- sensitive paths ----------------------------------------------------
    changed_paths = [f.path for f in record.diff.files]
    path_hits: list[tuple[str, list[str]]] = []
    for pattern in sensitive.paths:
        spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
        matched = [p for p in changed_paths if spec.match_file(p)]
        if matched:
            path_hits.append((pattern, matched))
    path_points = min(points.sensitive_path * len(path_hits), points.sensitive_path_cap)
    score += path_points
    for pattern, matched in path_hits:
        _add(
            evidence,
            "file",
            f"{len(matched)} changed file(s) sit under the sensitive path {pattern}: "
            f"{', '.join(sorted(matched))}.",
            {
                "pattern": pattern,
                "paths": sorted(matched),
                "points": points.sensitive_path,
                "cap": points.sensitive_path_cap,
            },
            "github",
            record.diff.provenance,
        )

    # --- sensitive shell commands ------------------------------------------
    bash_hits: list[tuple[str, str]] = []
    for pattern in sensitive.bash_patterns:
        for session in sessions:
            hit = None
            for call in session.tools_used:
                if call.bash_command and re.search(pattern, call.bash_command):
                    hit = call.bash_command
                    break
            if hit is not None:
                bash_hits.append((pattern, hit))
                break
    bash_points = min(points.sensitive_bash * len(bash_hits), points.sensitive_bash_cap)
    score += bash_points
    for pattern, command in bash_hits:
        _add(
            evidence,
            "tool_call",
            f"The agent ran a shell command matching the sensitive pattern "
            f"{pattern!r}: {command}",
            {
                "pattern": pattern,
                "bash_command": command,
                "points": points.sensitive_bash,
                "cap": points.sensitive_bash_cap,
            },
            "session",
            "real",
        )

    # --- permission posture --------------------------------------------------
    bypass_sessions = [s.session_id for s in sessions if BYPASS_MODE in s.permission_modes]
    if bypass_sessions:
        score += points.bypass_mode
        _add(
            evidence,
            "tool_call",
            f"The agent ran in {BYPASS_MODE} mode during this change.",
            {
                "sessions": bypass_sessions,
                "mode": BYPASS_MODE,
                "points": points.bypass_mode,
            },
            "session",
            "real",
        )

    sandbox_calls = sum(s.sandbox_disabled_calls for s in sessions)
    if sandbox_calls > 0:
        score += points.sandbox_disabled
        _add(
            evidence,
            "tool_call",
            f"{sandbox_calls} tool call(s) ran with the sandbox disabled.",
            {"sandbox_disabled_calls": sandbox_calls, "points": points.sandbox_disabled},
            "session",
            "real",
        )

    rule = sum(s.approvals_by.get("rule", 0) for s in sessions)
    hook = sum(s.approvals_by.get("hook", 0) for s in sessions)
    human = sum(s.approvals_by.get("human", 0) for s in sessions)
    total_calls = rule + hook + human
    ratio = (rule / total_calls) if total_calls else 0.0
    if total_calls >= cfg.auto_approved_min_calls and ratio >= cfg.auto_approved_ratio:
        score += points.mostly_auto_approved
        _add(
            evidence,
            "tool_call",
            f"{rule} of {total_calls} tool calls were approved by a standing rule "
            f"rather than by a person.",
            {
                "rule": rule,
                "hook": hook,
                "human": human,
                "total_calls": total_calls,
                "ratio": round(ratio, 4),
                "auto_approved_ratio": cfg.auto_approved_ratio,
                "auto_approved_min_calls": cfg.auto_approved_min_calls,
                "points": points.mostly_auto_approved,
            },
            "session",
            "real",
        )

    has_tool_detail = any(s.detail is not None and s.detail.tool_details for s in sessions)
    status = "computed" if has_tool_detail else "degraded"
    if not has_tool_detail:
        _add(
            evidence,
            "note",
            "No session reported tool details, so only the changed paths could be "
            "judged.",
            {
                "sessions_present": len(sessions),
                "tool_details": False,
                "path_points": path_points,
            },
            "session",
            "real",
        )
    if record.diff.provenance == "simulated" or any(
        s.provenance == "simulated" for s in sessions
    ):
        status = "degraded"

    found = len(seen) + len(path_hits) + len(bash_hits)
    return _result(
        score,
        status,
        f"{found} sensitive",
        f"{found} sensitive thing(s) were reached: {len(seen)} tool(s), "
        f"{len(path_hits)} path pattern(s), {len(bash_hits)} shell pattern(s).",
        evidence,
    )
