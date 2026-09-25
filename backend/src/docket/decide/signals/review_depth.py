"""Signal 2 of 4: review depth (RFC section 9.1).

Pure: standard library and docket.models only. No clock, no I/O.

Assumptions written down as RFC section 0 rule 2 asks:
  * The headline is the smallest `ask_to_approve_s` (RFC 9.1), while the speed term
    uses the *largest* one, because `min(speed_score(a))` is the slowest reviewer.
    Both numbers appear in the per-approver evidence, so both can be opened.
  * `mm ss` formatting: "1m 49s" above a minute, "47s" below it.
  * Sections actually read: `review` and `diff`. A missing diff (no line count to
    scale the expected reading time) or simulated provenance makes it `degraded`.
"""

from __future__ import annotations

from docket.models.change_record import ChangeRecord
from docket.models.config import ReviewDepthConfig
from docket.models.signals import EvidenceItem, SignalResult

SIGNAL = "review_depth"
LABEL_CODE = "Review depth"
LABEL_NON_CODE = "No reviewable artifact"


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


def format_duration(seconds: int | float) -> str:
    total = int(seconds)
    minutes, secs = divmod(max(total, 0), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


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


def compute(record: ChangeRecord, cfg: ReviewDepthConfig) -> SignalResult:
    """RFC 9.1. Returns a SignalResult; never raises on a missing section."""
    label = LABEL_CODE if record.kind == "code" else LABEL_NON_CODE
    review = record.review
    evidence: list[EvidenceItem] = []

    if not review.present:
        _add(
            evidence,
            "review",
            "No pull request and no reviewer exist for this change.",
            {"review_present": False, "kind": record.kind},
            "github",
            review.provenance,
        )
        return _result(
            label,
            100.0,
            "computed",
            "no review",
            "There is nothing for a reviewer to open: this change produced no pull "
            "request.",
            evidence,
        )

    if not review.approvers:
        _add(
            evidence,
            "review",
            "Nobody has approved this change.",
            {
                "approvers": 0,
                "comments": review.comments,
                "files_changed": review.files_changed,
            },
            "github",
            review.provenance,
        )
        return _result(
            label,
            100.0,
            "computed",
            "0 approvals",
            f"No reviewer has approved the {review.files_changed} changed files.",
            evidence,
        )

    lines_added = record.diff.lines_added
    expected_s = max(lines_added, 1) * cfg.seconds_per_line

    speeds: list[float] = []
    for approver in review.approvers:
        speed_score = _clamp(100.0 * (1.0 - approver.ask_to_approve_s / expected_s))
        speeds.append(speed_score)
        _add(
            evidence,
            "review",
            f"{approver.reviewer} approved {lines_added} added lines "
            f"{format_duration(approver.ask_to_approve_s)} after being asked; reading "
            f"them at {cfg.seconds_per_line} s per line would take "
            f"{format_duration(expected_s)}.",
            {
                "reviewer": approver.reviewer,
                "ask_to_approve_s": approver.ask_to_approve_s,
                "expected_s": round(expected_s, 2),
                "lines_added": lines_added,
                "seconds_per_line": cfg.seconds_per_line,
                "speed_score": round(speed_score, 2),
                "approved_commit": approver.approved_commit,
            },
            "github",
            review.provenance,
        )
    speed = min(speeds)

    if review.comments == 0:
        engagement = 100.0
    else:
        engagement = 100.0 * (
            1.0 - len(review.files_commented) / max(review.files_changed, 1)
        )
    _add(
        evidence,
        "review",
        (
            f"No reviewer left a comment on any of the {review.files_changed} "
            f"changed files."
            if review.comments == 0
            else f"{review.comments} review comments were left on "
            f"{len(review.files_commented)} of {review.files_changed} changed files."
        ),
        {
            "comments": review.comments,
            "files_commented": list(review.files_commented),
            "files_changed": review.files_changed,
            "engagement_score": round(engagement, 2),
        },
        "github",
        review.provenance,
    )

    score = cfg.speed_weight * speed + cfg.engagement_weight * engagement
    if review.stale_approval:
        score += cfg.stale_penalty
        _add(
            evidence,
            "review",
            "Every approval is against an older commit than the one on the branch now.",
            {"stale_approval": True, "stale_penalty": cfg.stale_penalty},
            "github",
            review.provenance,
        )
    score = _clamp(score)

    status = "computed"
    if review.provenance == "simulated" or record.diff.provenance == "simulated":
        status = "degraded"
    elif not record.diff.present:
        status = "degraded"

    fastest = min(a.ask_to_approve_s for a in review.approvers)
    return _result(
        label,
        score,
        status,
        format_duration(fastest),
        f"{len(review.approvers)} approval(s); the fastest arrived "
        f"{format_duration(fastest)} after the request against an expected reading "
        f"time of {format_duration(expected_s)} for {lines_added} added lines.",
        evidence,
    )
