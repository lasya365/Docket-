"""Signal 3 of 4: untested generation (RFC section 9.3).

Pure: standard library, pathspec and docket.models only. No clock, no I/O.

Assumptions written down as RFC section 0 rule 2 asks:
  * "no coverage data" means `verify.coverage_by_line` is empty. A coverage report
    that exists but says nothing about a changed file still counts as data, and the
    per-line rules in RFC 9.3 then decide what that file contributes.
  * The file-average coverage the evidence must show next to the AI-line coverage is
    `executed / (executed + missing)` over the whole file as the coverage report
    gives it, which is the number a board sees on a coverage dashboard today.
  * Sections actually read: `verify` (and its rehearsal) and `diff`.
"""

from __future__ import annotations

import pathspec

from docket.models.change_record import ChangeRecord
from docket.models.config import RehearsalConfig, UntestedConfig
from docket.models.signals import EvidenceItem, SignalResult

SIGNAL = "untested"
AI_LABELS = ("ai", "mixed")
LABEL_CODE = "Untested generation"
LABEL_NON_CODE = "Unverified behaviour"


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


def _all_unknown(record: ChangeRecord) -> bool:
    labels = [
        label
        for per_line in record.diff.attribution_by_line.values()
        for label in per_line.values()
    ]
    if not labels:
        return record.diff.lines_added > 0
    return all(label == "unknown" for label in labels)


def _ai_lines(record: ChangeRecord) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for path, per_line in record.diff.attribution_by_line.items():
        nums = sorted(n for n, label in per_line.items() if label in AI_LABELS)
        if nums:
            out[path] = nums
    return out


def _rehearsal(record: ChangeRecord, cfg: RehearsalConfig, label: str) -> SignalResult:
    reh = record.verify.rehearsal
    assert reh is not None  # guarded by the caller
    evidence: list[EvidenceItem] = []
    cases = max(reh.cases, 1)
    regressed_term = cfg.regression_weight * 100.0 * reh.regressed / cases
    changed_term = cfg.changed_weight * 100.0 * reh.changed_acceptable / cases
    score = min(100.0, regressed_term + changed_term)

    _add(
        evidence,
        "manifest",
        f"The new bundle was replayed over {reh.cases} stored cases: {reh.identical} "
        f"identical, {reh.changed_acceptable} changed acceptably, {reh.regressed} "
        f"regressed.",
        {
            "cases": reh.cases,
            "identical": reh.identical,
            "changed_acceptable": reh.changed_acceptable,
            "regressed": reh.regressed,
            "regression_weight": cfg.regression_weight,
            "changed_weight": cfg.changed_weight,
            "regressed_term": round(regressed_term, 2),
            "changed_term": round(changed_term, 2),
        },
        "manifest",
        reh.provenance,
    )
    for reg in reh.regressions:
        _add(
            evidence,
            "manifest",
            f"Case {reg.case_id} used to answer {reg.before} and now answers "
            f"{reg.after}.",
            {"case_id": reg.case_id, "before": reg.before, "after": reg.after},
            "manifest",
            reh.provenance,
        )

    status = "degraded" if reh.provenance == "simulated" else "computed"
    return _result(
        label,
        score,
        status,
        f"{reh.regressed} regressed / {reh.cases}",
        f"{reh.regressed} of {reh.cases} rehearsal cases regressed and "
        f"{reh.changed_acceptable} changed acceptably.",
        evidence,
    )


def compute(
    record: ChangeRecord, cfg: UntestedConfig, rehearsal_cfg: RehearsalConfig
) -> SignalResult:
    """RFC 9.3. Returns a SignalResult; never raises on a missing section."""
    is_code = record.kind == "code"
    label = LABEL_CODE if is_code else LABEL_NON_CODE
    verify = record.verify
    evidence: list[EvidenceItem] = []

    # --- no-code kinds -----------------------------------------------------
    if not is_code:
        if verify.rehearsal is not None:
            return _rehearsal(record, rehearsal_cfg, label)
        _add(
            evidence,
            "note",
            "Nothing verified how the agent behaves after this change.",
            {"rehearsal": None, "kind": record.kind},
            "manifest",
            verify.provenance,
        )
        return _result(
            label,
            100.0,
            "computed",
            "not rehearsed",
            "No rehearsal, no test and no canary covered this bundle change.",
            evidence,
        )

    # --- code: Docket cannot see who wrote what ----------------------------
    if _all_unknown(record):
        _add(
            evidence,
            "note",
            "No added line can be attributed to an agent, so agent-written lines "
            "cannot be measured against coverage.",
            {
                "join_confidence": record.join.confidence,
                "lines_added": record.diff.lines_added,
            },
            "github",
            record.diff.provenance,
        )
        return _result(
            label,
            0.0,
            "not_computable",
            "unknown",
            "Docket cannot tell which lines an agent wrote, so it cannot say whether "
            "they were tested.",
            evidence,
        )

    ai_by_path = _ai_lines(record)
    ai_total = sum(len(v) for v in ai_by_path.values())
    if ai_total == 0:
        _add(
            evidence,
            "note",
            "No added line was written by an agent.",
            {"ai_lines": 0, "lines_added": record.diff.lines_added},
            "github",
            record.diff.provenance,
        )
        return _result(
            label,
            0.0,
            "computed",
            "0 AI lines",
            "Every added line was written by a person, so there is no agent-written "
            "code to cover.",
            evidence,
        )

    # --- code: Docket cannot see the coverage ------------------------------
    if not verify.present or not verify.coverage_by_line:
        _add(
            evidence,
            "coverage",
            "No line-level coverage was found for this commit.",
            {
                "verify_present": verify.present,
                "ci_status": verify.ci_status,
                "files_with_coverage": 0,
                "ai_lines": ai_total,
            },
            "ci",
            verify.provenance,
        )
        return _result(
            label,
            0.0,
            "not_computable",
            "no coverage",
            "No coverage report reached Docket for this commit, so agent-written "
            "lines cannot be checked.",
            evidence,
        )

    # --- code: the formula -------------------------------------------------
    code_spec = pathspec.PathSpec.from_lines("gitwildmatch", list(cfg.code_globs))
    total_executable = 0
    total_covered = 0
    per_file: list[dict] = []

    for path in sorted(ai_by_path):
        nums = ai_by_path[path]
        file_cov = verify.coverage_by_line.get(path)
        executable = 0
        covered = 0
        file_avg: float | None = None
        rest_avg: float | None = None
        if file_cov is not None:
            executed = set(file_cov.executed)
            missing = set(file_cov.missing)
            for n in nums:
                if n in executed or n in missing:
                    executable += 1
                    if n in executed:
                        covered += 1
            known = len(executed) + len(missing)
            file_avg = (100.0 * len(executed) / known) if known else None
            # the same file with the agent's new lines taken out: what the coverage
            # dashboard showed the board before this change landed
            ai_set = set(nums)
            rest_executed = len(executed - ai_set)
            rest_known = rest_executed + len(missing - ai_set)
            rest_avg = (100.0 * rest_executed / rest_known) if rest_known else None
        elif code_spec.match_file(path):
            # code the tests never loaded: executable, and not covered
            executable = len(nums)
        total_executable += executable
        total_covered += covered
        per_file.append(
            {
                "path": path,
                "ai_lines": len(nums),
                "ai_executable": executable,
                "ai_covered": covered,
                "ai_coverage_pct": round(100.0 * covered / executable, 2)
                if executable
                else None,
                "file_average_pct": round(file_avg, 2) if file_avg is not None else None,
                "rest_of_file_pct": round(rest_avg, 2) if rest_avg is not None else None,
                "in_coverage_report": file_cov is not None,
            }
        )

    ai_cov = (total_covered / total_executable) if total_executable else 1.0
    score = 0.0 if total_executable == 0 else 100.0 * (1.0 - ai_cov)
    pct = round(100.0 * ai_cov, 2)

    _add(
        evidence,
        "coverage",
        f"{total_covered} of {total_executable} agent-written executable lines are "
        f"covered by a test ({pct:.0f}%).",
        {
            "ai_covered": total_covered,
            "ai_executable": total_executable,
            "ai_lines": ai_total,
            "ai_coverage_pct": pct,
            "ci_status": verify.ci_status,
        },
        "ci",
        verify.provenance,
    )
    for row in per_file:
        if row["file_average_pct"] is None:
            text = (
                f"{row['path']}: {row['ai_lines']} agent-written lines and no entry in "
                f"the coverage report, so none of them is known to run under test."
            )
        else:
            ai_pct = row["ai_coverage_pct"]
            ai_text = "no executable" if ai_pct is None else f"{ai_pct:.0f}%"
            rest = row["rest_of_file_pct"]
            rest_text = (
                f"the rest of {row['path']} runs at {rest:.0f}%"
                if rest is not None
                else f"{row['path']} reports {row['file_average_pct']:.0f}% overall"
            )
            text = (
                f"{row['path']}: {rest_text} and the whole file averages "
                f"{row['file_average_pct']:.0f}%, but only {ai_text} of its "
                f"{row['ai_lines']} agent-written lines are covered "
                f"({row['ai_covered']} of {row['ai_executable']})."
            )
        _add(evidence, "coverage", text, dict(row), "ci", verify.provenance)

    _add(
        evidence,
        "note",
        f"The tests covering this change were authored by: {verify.tests_authored_by}.",
        {"tests_authored_by": verify.tests_authored_by, "ci_status": verify.ci_status},
        "ci",
        verify.provenance,
    )
    if verify.tests_authored_by == "ai" and ai_total > 0:
        _add(
            evidence,
            "note",
            "The same agent wrote the code and the tests that cover it (self-graded).",
            {"tests_authored_by": "ai", "ai_lines": ai_total},
            "ci",
            verify.provenance,
        )

    status = "degraded" if verify.provenance == "simulated" else "computed"
    if record.diff.provenance == "simulated":
        status = "degraded"

    return _result(
        label,
        score,
        status,
        f"{pct:.0f}%",
        f"{total_covered} of {total_executable} agent-written executable lines run "
        f"under test ({pct:.0f}% coverage on agent-written lines).",
        evidence,
    )
