"""Signal 1 of 4: unattributed change (RFC section 9.2).

Pure: standard library, pathspec and docket.models only. No clock, no I/O.

Assumptions written down as RFC section 0 rule 2 asks:
  * "every added line is unknown" is read off `record.diff.attribution_by_line`.
    A diff that reports added lines but carries no attribution at all is the same
    finding (Docket cannot see), so it is `not_computable` as well.
  * The turn that wrote most of an out-of-scope file is taken from
    `diff.hunk_attribution`, weighting each hunk's `prompt_id` by the AI lines it
    holds. Ties go to the first hunk, so the output is byte-identical every run.
  * Sections actually read: `intent` and `diff`. If either carries
    provenance="simulated" the signal is `degraded` (RFC 9.0).
"""

from __future__ import annotations

import pathspec

from docket.models.change_record import ChangeRecord
from docket.models.config import UnattributedConfig
from docket.models.signals import EvidenceItem, SignalResult

SIGNAL = "unattributed"
AI_LABELS = ("ai", "mixed")
LABEL_CODE = "Unattributed change"
LABEL_NON_CODE = "No stated intent"


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


def _intent_source(record: ChangeRecord) -> str:
    if record.intent.source == "github-issue":
        return "github"
    if record.intent.source == "manifest-change-ref":
        return "manifest"
    return "config"


def _ai_lines_by_path(record: ChangeRecord) -> dict[str, int]:
    out: dict[str, int] = {}
    for path, per_line in record.diff.attribution_by_line.items():
        out[path] = sum(1 for label in per_line.values() if label in AI_LABELS)
    return out


def _all_unknown(record: ChangeRecord) -> bool:
    labels = [
        label
        for per_line in record.diff.attribution_by_line.values()
        for label in per_line.values()
    ]
    if not labels:
        # A diff with added lines but no attribution at all: Docket cannot see.
        return record.diff.lines_added > 0
    return all(label == "unknown" for label in labels)


def _top_prompt_id(record: ChangeRecord, path: str) -> str | None:
    weights: dict[str, int] = {}
    order: list[str] = []
    for hunk in record.diff.hunk_attribution:
        if not hunk.hunk_id.startswith(f"{path}#"):
            continue
        if hunk.prompt_id is None:
            continue
        ai_count = sum(hunk.counts.get(label, 0) for label in AI_LABELS)
        if hunk.prompt_id not in weights:
            weights[hunk.prompt_id] = 0
            order.append(hunk.prompt_id)
        weights[hunk.prompt_id] += ai_count
    if not weights:
        return None
    return max(order, key=lambda pid: weights[pid])


def _degraded_by_provenance(record: ChangeRecord) -> bool:
    return "simulated" in (record.intent.provenance, record.diff.provenance)


def _result(
    label: str,
    score: float,
    status: str,
    headline: str,
    summary: str,
    evidence: list[EvidenceItem],
) -> SignalResult:
    return SignalResult(
        signal=SIGNAL,
        label=label,
        score=round(float(score), 2),
        status=status,  # type: ignore[arg-type]
        headline=headline,
        summary=summary,
        evidence=evidence,
    )


def compute(record: ChangeRecord, cfg: UnattributedConfig) -> SignalResult:
    """RFC 9.2. Returns a SignalResult; never raises on a missing section."""
    is_code = record.kind == "code"
    label = LABEL_CODE if is_code else LABEL_NON_CODE
    evidence: list[EvidenceItem] = []
    intent = record.intent

    # --- no-code kinds -----------------------------------------------------
    if not is_code:
        if intent.present:
            _add(
                evidence,
                "note",
                f"A ticket asked for this change: {intent.ref or 'reference on record'}.",
                {"ref": intent.ref, "url": intent.url, "source": intent.source},
                _intent_source(record),
                intent.provenance,
            )
            status = "degraded" if intent.provenance == "simulated" else "computed"
            return _result(
                label,
                0.0,
                status,
                intent.ref or "declared",
                f"The change cites {intent.ref or 'a ticket'} as the reason it was made.",
                evidence,
            )
        _add(
            evidence,
            "note",
            "No ticket or requirement asked for this change.",
            {"intent_present": False, "kind": record.kind},
            _intent_source(record),
            intent.provenance,
        )
        return _result(
            label,
            100.0,
            "computed",
            "no intent",
            "Nothing on record asked for this change to be made.",
            evidence,
        )

    # --- code: Docket cannot see -------------------------------------------
    if _all_unknown(record):
        unmatched = record.join.confidence == "unmatched"
        why = (
            "No agent session could be matched to this pull request, so no added line "
            "can be attributed."
            if unmatched
            else "Edits were not captured for this pull request, so no added line can be "
            "attributed."
        )
        _add(
            evidence,
            "note",
            why,
            {
                "join_confidence": record.join.confidence,
                "join_method": record.join.method,
                "lines_added": record.diff.lines_added,
                "human_declared": record.join.human_declared,
            },
            "github",
            record.diff.provenance,
        )
        return _result(
            label,
            0.0,
            "not_computable",
            "unknown",
            "Docket cannot say who wrote these lines, so it cannot say what was "
            "unasked for.",
            evidence,
        )

    ai_by_path = _ai_lines_by_path(record)
    ai_lines = sum(ai_by_path.values())

    # --- code: no AI lines at all ------------------------------------------
    if ai_lines == 0:
        _add(
            evidence,
            "note",
            "No added line was written by an agent.",
            {
                "ai_lines": 0,
                "lines_added": record.diff.lines_added,
                "totals": dict(record.diff.totals),
            },
            "github",
            record.diff.provenance,
        )
        status = "degraded" if _degraded_by_provenance(record) else "computed"
        return _result(
            label,
            0.0,
            status,
            f"0 / {record.diff.lines_added}",
            "Every added line was written by a person.",
            evidence,
        )

    # --- code: nothing was asked for ---------------------------------------
    if not intent.present:
        _add(
            evidence,
            "note",
            "No ticket or requirement asked for this change, so every agent-written "
            "line is unattributed.",
            {"ai_lines": ai_lines, "out_lines": ai_lines, "intent_present": False},
            "github",
            record.diff.provenance,
        )
        for path in sorted(ai_by_path):
            if ai_by_path[path] == 0:
                continue
            _add(
                evidence,
                "file",
                f"{path}: {ai_by_path[path]} agent-written lines with nothing asking "
                f"for them.",
                {
                    "path": path,
                    "ai_lines": ai_by_path[path],
                    "in_scope": False,
                    "prompt_id": _top_prompt_id(record, path),
                },
                "github",
                record.diff.provenance,
            )
        status = "degraded" if _degraded_by_provenance(record) else "computed"
        return _result(
            label,
            100.0,
            status,
            f"{ai_lines} / {ai_lines}",
            f"All {ai_lines} agent-written lines sit outside any stated requirement: "
            f"no ticket was linked to this change.",
            evidence,
        )

    # --- code: formula A or B ----------------------------------------------
    declared_scope = list(intent.scope_paths)
    always = list(cfg.always_in_scope)
    if declared_scope:
        formula = "A"
        status = "computed"
        spec = pathspec.PathSpec.from_lines("gitwildmatch", declared_scope + always)

        def in_scope(path: str) -> bool:
            return spec.match_file(path)

    else:
        formula = "B"
        status = "degraded"
        text = (intent.text or "").lower()
        always_spec = pathspec.PathSpec.from_lines("gitwildmatch", always)

        def in_scope(path: str) -> bool:
            lowered = path.lower()
            basename = lowered.rsplit("/", 1)[-1]
            if lowered in text or (basename and basename in text):
                return True
            return always_spec.match_file(path)

    scope_by_path = {path: in_scope(path) for path in ai_by_path}
    out_lines = sum(n for path, n in ai_by_path.items() if not scope_by_path[path])
    score = 100.0 * out_lines / ai_lines

    if _degraded_by_provenance(record):
        status = "degraded"

    scope_text = (
        f"The ticket {intent.ref or ''} declares its scope as: "
        f"{', '.join(declared_scope)}.".replace("  ", " ")
        if formula == "A"
        else (
            "The ticket declares no scope, so file names were matched against the "
            "ticket text instead."
        )
    )
    _add(
        evidence,
        "note",
        scope_text,
        {
            "formula": formula,
            "scope_paths": declared_scope,
            "always_in_scope": always,
            "ai_lines": ai_lines,
            "out_lines": out_lines,
            "ref": intent.ref,
        },
        _intent_source(record),
        intent.provenance,
    )

    out_paths = sorted(
        (p for p in ai_by_path if not scope_by_path[p] and ai_by_path[p] > 0),
        key=lambda p: (-ai_by_path[p], p),
    )
    for path in out_paths:
        prompt_id = _top_prompt_id(record, path)
        turn = f" Most of it came from turn {prompt_id}." if prompt_id else ""
        _add(
            evidence,
            "file",
            f"{path} is outside the declared scope and carries "
            f"{ai_by_path[path]} agent-written lines.{turn}",
            {
                "path": path,
                "ai_lines": ai_by_path[path],
                "in_scope": False,
                "prompt_id": prompt_id,
            },
            "github",
            record.diff.provenance,
        )

    return _result(
        label,
        score,
        status,
        f"{out_lines} / {ai_lines}",
        f"{out_lines} of {ai_lines} agent-written lines are in files the stated scope "
        f"does not cover.",
        evidence,
    )
