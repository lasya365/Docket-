"""A finished `Run` built by hand, for the plane-4 tests (RFC 6.5).

decide/ is written in parallel, so nothing here imports it: the Decision, the
SignalResults, the EvidenceChain and the BackoutPlan are hand-made from the
fixture ChangeRecord. The scores match RFC 15.2 so the objects read like a real
run, but no test in this file asserts them: the golden table belongs to
test_signals_golden.

Assumptions written down as RFC section 0 rule 2 asks:
  * Chain links are present exactly when the matching record section is present,
    which is what RFC 9.7 does.
  * `make_run` seals the run it built unless the caller passed a seal, so a run
    coming out of this helper always verifies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from docket.models.change_record import ChangeRecord
from docket.models.common import CHAIN_ORDER
from docket.models.run import Run
from docket.models.signals import (
    BackoutPlan,
    BackoutStep,
    ChainLink,
    Decision,
    EvidenceChain,
    EvidenceItem,
    HardStopHit,
    SignalResult,
)
from docket.record.seal import seal as seal_run

from fixtures.build_fixtures import record

RUN_T0 = datetime(2026, 9, 17, 8, 30, 0, tzinfo=timezone.utc)

# score, status, headline, summary per fixture (RFC 15.2 for the numbers)
SCORES: dict[str, dict[str, tuple[float, str, str]]] = {
    "held": {
        "unattributed": (56.29, "computed", "340 / 604"),
        "review_depth": (95.17, "computed", "112 s for 812 lines"),
        "untested": (69.04, "computed", "187 / 604 covered"),
        "blast_radius": (75.00, "computed", "3 amplifiers"),
    },
    "cleared": {
        "unattributed": (0.00, "computed", "0 / 50"),
        "review_depth": (10.00, "computed", "1320 s for 64 lines"),
        "untested": (5.00, "computed", "38 / 40 covered"),
        "blast_radius": (0.00, "computed", "no amplifiers"),
    },
    "unmatched": {
        "unattributed": (80.00, "not_computable", "no session"),
        "review_depth": (80.00, "not_computable", "no approval"),
        "untested": (80.00, "not_computable", "no session"),
        "blast_radius": (25.00, "computed", "1 sensitive path"),
    },
    "model-bump": {
        "unattributed": (100.00, "not_computable", "no diff"),
        "review_depth": (100.00, "not_computable", "no review"),
        "untested": (48.00, "degraded", "2 regressions in 50"),
        "blast_radius": (90.00, "computed", "model version change"),
    },
}

LABELS = {
    "unattributed": "Unattributed change",
    "review_depth": "Review depth",
    "untested": "Untested generation",
    "blast_radius": "Blast radius",
}

HARD_STOPS = {
    "held": [("HS1", "blast_radius and review_depth are at or above 90")],
    "cleared": [],
    "unmatched": [("HS3", "no session and the change touches **/entitlement/**")],
    "model-bump": [("HS1", "unattributed is at or above 90")],
}

DECISIONS = {
    "held": ("HOLD", 73.88, "high"),
    "cleared": ("APPROVE", 3.75, "low"),
    "unmatched": ("HOLD", 66.25, "high"),
    "model-bump": ("HOLD", 84.50, "very_high"),
}


def _signals(name: str) -> list[SignalResult]:
    out: list[SignalResult] = []
    for signal, (score, status, headline) in SCORES[name].items():
        evidence = [
            EvidenceItem(
                id=f"ev:{signal}:{n}",
                kind="note",
                text=f"{LABELS[signal]} evidence {n} for {name}.",
                data={"n": n, "score": score},
                source="session" if signal != "untested" else "ci",
                provenance="real",
            )
            for n in (1, 2)
        ]
        out.append(SignalResult(
            signal=signal,
            label=LABELS[signal],
            score=score,
            status=status,
            headline=headline,
            summary=f"{LABELS[signal]} scored {score:.2f} ({headline}).",
            evidence=evidence,
        ))
    return out


def _decision(name: str) -> Decision:
    value, composite, risk = DECISIONS[name]
    return Decision(
        decision=value,
        composite=composite,
        threshold=45.0,
        risk_level=risk,
        weights_used={
            "unattributed": 0.25, "review_depth": 0.25, "untested": 0.25, "blast_radius": 0.25,
        },
        excluded_signals=[],
        hard_stops_fired=[HardStopHit(id=i, reason=r) for i, r in HARD_STOPS[name]],
        unmatched_policy_applied="penalize" if name in ("unmatched", "model-bump") else "n/a",
        config_hash="0" * 64,
    )


def _chain(rec: ChangeRecord) -> EvidenceChain:
    present = {
        "intent": rec.intent.present,
        # RFC 9.7: for a no-code kind the session link is the manifest change.
        "session": rec.manifest.present if rec.kind != "code"
        else any(s.present for s in rec.sessions),
        "diff": rec.diff.present,
        "review": rec.review.present,
        "verify": rec.verify.present,
        "deploy": rec.deploy.present,
    }
    links = [
        ChainLink(
            name=link,
            present=present[link],
            summary=f"{link} recorded" if present[link] else f"no {link} evidence",
        )
        for link in CHAIN_ORDER
    ]
    return EvidenceChain(links=links, missing_count=sum(1 for l in links if not l.present))


def _backout(name: str, rec: ChangeRecord) -> BackoutPlan:
    if rec.kind != "code":
        before = rec.manifest.before
        after = rec.manifest.after
        steps = [BackoutStep(
            key="model",
            from_value=after.manifest.model if after else "unknown",
            to_value=before.manifest.model if before else "unknown",
        )]
        text = "Restore the previous agent bundle: " + "; ".join(
            f"{s.key} {s.from_value} -> {s.to_value}" for s in steps
        )
        return BackoutPlan(
            available=bool(before),
            summary="Roll the agent bundle back one snapshot.",
            steps=steps,
            naive_plan="Revert the pull request",
            why_naive_fails="There is no commit: the change is a manifest edit.",
            text=text,
        )
    return BackoutPlan(
        available=True,
        summary="Revert the merge commit and redeploy.",
        steps=[BackoutStep(key="code", from_value="head", to_value="base")],
        naive_plan="Revert commit a91f3c",
        why_naive_fails="The change spans several commits across two services.",
        text="Revert the pull request, redeploy the previous build, then re-run the sync job.",
    )


def make_run(name: str = "held", **overrides: Any) -> Run:
    """A complete Run for `name`. Any Run field can be overridden by keyword."""
    rec: ChangeRecord = overrides.pop("record", None) or record(name)
    run = Run(
        run_id=overrides.pop("run_id", f"run-{name}-0001"),
        change_key=overrides.pop("change_key", rec.change_key),
        mode=overrides.pop("mode", "replay"),
        created_at=overrides.pop("created_at", RUN_T0),
        record=rec,
        signals=overrides.pop("signals", None) or _signals(name),
        decision=overrides.pop("decision", None) or _decision(name),
        chain=overrides.pop("chain", None) or _chain(rec),
        backout=overrides.pop("backout", None) or _backout(name, rec),
        rollout_text=overrides.pop(
            "rollout_text",
            "Merge the pull request, deploy through the standard pipeline, watch the sync queue.",
        ),
        seal="",
    )
    sealed = overrides.pop("seal", None)
    for key, value in overrides.items():
        setattr(run, key, value)
    run.seal = sealed if sealed is not None else seal_run(run)
    return run
