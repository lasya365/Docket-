"""RFC 15.3: the Decision Gate.

Covers test_unmatched_is_never_waved_through, test_human_declared_label,
test_hard_stop_beats_average and test_regate.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fixtures.build_fixtures import record

from docket.decide.backout_planner import plan_backout
from docket.decide.engine import run_signals
from docket.decide.evidence_chain import build_chain
from docket.decide.gate import decide, regate
from docket.models.change_record import ChangeRecord, JoinSection
from docket.models.fragments import ChangedFile
from docket.models.run import Run
from docket.models.signals import EvidenceItem, SignalResult

SENSITIVE_FILE = "entitlement/roles.py"


def strip_sessions(name: str, human_declared: bool = False) -> ChangeRecord:
    """The same change with the telemetry turned off: nothing can be attributed."""
    rec = record(name).model_copy(deep=True)
    rec.sessions = []
    rec.join = JoinSection(method="none", confidence="unmatched", human_declared=human_declared)
    unknown = 0
    for per_line in rec.diff.attribution_by_line.values():
        for line_no in list(per_line):
            per_line[line_no] = "unknown"
            unknown += 1
    for hunk in rec.diff.hunk_attribution:
        hunk.label = "unknown"
        hunk.counts = {"unknown": sum(hunk.counts.values())}
        hunk.session_id = None
        hunk.prompt_id = None
    rec.diff.totals = {"ai": 0, "human": 0, "mixed": 0, "unknown": unknown}
    return rec


def add_sensitive_file(rec: ChangeRecord) -> ChangeRecord:
    rec = rec.model_copy(deep=True)
    rec.diff.files.append(
        ChangedFile(
            path=SENSITIVE_FILE,
            status="modified",
            additions=4,
            deletions=0,
            patch_present=True,
            hunks=[],
        )
    )
    return rec


def gate(rec: ChangeRecord, cfg, gate_cfg):
    signals = run_signals(rec, cfg.signals, cfg.sensitive)
    return signals, decide(rec, signals, gate_cfg)


# ---------------------------------------------------------------------------
# RFC 15.3: test_unmatched_is_never_waved_through
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["held", "cleared"])
def test_unmatched_is_never_waved_through(name, cfg, gate_cfg) -> None:
    rec = strip_sessions(name)
    signals, decision = gate(rec, cfg, gate_cfg)
    by_name = {s.signal: s for s in signals}

    assert by_name["unattributed"].status == "not_computable"
    assert by_name["untested"].status == "not_computable"
    assert by_name["blast_radius"].status == "degraded"
    assert decision.unmatched_policy_applied == "penalize"
    assert decision.excluded_signals == []
    assert decision.decision == "HOLD"


def test_unmatched_cleared_scores_exactly_52_5(cfg, gate_cfg) -> None:
    """80 (not computable) + 10 (review) + 80 (not computable) + 40 (degraded floor)."""
    rec = strip_sessions("cleared")
    _signals, decision = gate(rec, cfg, gate_cfg)
    assert decision.composite == pytest.approx(52.50, abs=0.01)
    assert decision.decision == "HOLD"
    assert decision.risk_level == "high"        # 52.50 sits between medium and high


def test_turning_telemetry_off_never_lowers_the_score(cfg, gate_cfg) -> None:
    for name in ("held", "cleared"):
        _s, matched = gate(record(name), cfg, gate_cfg)
        _s, unmatched = gate(strip_sessions(name), cfg, gate_cfg)
        if matched.decision == "APPROVE":
            assert unmatched.decision == "HOLD"


# ---------------------------------------------------------------------------
# RFC 15.3: test_human_declared_label
# ---------------------------------------------------------------------------

def test_human_declared_label(cfg, gate_cfg) -> None:
    rec = strip_sessions("cleared", human_declared=True)
    _signals, decision = gate(rec, cfg, gate_cfg)

    assert decision.unmatched_policy_applied == "neutral"
    assert sorted(decision.excluded_signals) == ["unattributed", "untested"]
    assert sorted(decision.weights_used) == ["blast_radius", "review_depth"]
    assert sum(decision.weights_used.values()) == pytest.approx(1.0)
    assert decision.decision == "APPROVE"


def test_human_declared_label_cannot_wave_through_a_sensitive_path(cfg, gate_cfg) -> None:
    """HS3 fires with or without the label (RFC 9.5 step 4)."""
    for declared in (False, True):
        rec = add_sensitive_file(strip_sessions("cleared", human_declared=declared))
        _signals, decision = gate(rec, cfg, gate_cfg)
        fired = [h.id for h in decision.hard_stops_fired]
        assert "HS3" in fired, f"human_declared={declared}"
        assert decision.decision == "HOLD"
        assert SENSITIVE_FILE in decision.hard_stops_fired[-1].reason


def test_the_label_does_nothing_when_a_session_was_matched(cfg, gate_cfg) -> None:
    rec = record("cleared").model_copy(deep=True)
    rec.join.human_declared = True
    _signals, decision = gate(rec, cfg, gate_cfg)
    assert decision.unmatched_policy_applied == "n/a"
    assert decision.excluded_signals == []


# ---------------------------------------------------------------------------
# RFC 15.3: test_hard_stop_beats_average
# ---------------------------------------------------------------------------

def _signal(name: str, score: float, status: str = "computed") -> SignalResult:
    return SignalResult(
        signal=name,
        label=name,
        score=score,
        status=status,
        headline=str(score),
        summary=f"{name} scored {score}",
        evidence=[
            EvidenceItem(
                id=f"ev:{name}:1",
                kind="note",
                text=f"{name} scored {score}",
                data={"score": score},
                source="config",
                provenance="simulated",
            )
        ],
    )


def test_hard_stop_beats_average(cfg, gate_cfg) -> None:
    rec = record("cleared")
    signals = [
        _signal("unattributed", 10.0),
        _signal("review_depth", 10.0),
        _signal("untested", 10.0),
        _signal("blast_radius", 95.0),
    ]
    decision = decide(rec, signals, gate_cfg)
    assert decision.composite == pytest.approx(31.25, abs=0.01)
    assert decision.composite <= decision.threshold        # the average would pass
    assert [h.id for h in decision.hard_stops_fired] == ["HS1"]
    assert decision.decision == "HOLD"
    assert "95.00" in decision.hard_stops_fired[0].reason


def test_hs2_fires_for_a_first_sighting(cfg, gate_cfg) -> None:
    rec = record("model-bump").model_copy(deep=True)
    rec.manifest.before = None
    rec.manifest.previous_available = False
    _signals, decision = gate(rec, cfg, gate_cfg)
    assert "HS2" in [h.id for h in decision.hard_stops_fired]
    assert decision.decision == "HOLD"


# ---------------------------------------------------------------------------
# RFC 15.3: test_regate
# ---------------------------------------------------------------------------

def _run(name: str, cfg, gate_cfg) -> Run:
    rec = record(name)
    signals = run_signals(rec, cfg.signals, cfg.sensitive)
    decision = decide(rec, signals, gate_cfg)
    return Run(
        run_id=f"run-{name}",
        change_key=rec.change_key,
        mode="replay",
        created_at=datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc),
        record=rec,
        signals=signals,
        decision=decision,
        chain=build_chain(rec, signals),
        backout=plan_backout(rec),
        rollout_text="",
        seal="",
    )


def test_regate(settings, cfg) -> None:
    held = _run("held", cfg, settings.gate_config())
    cleared = _run("cleared", cfg, settings.gate_config())

    # the slider moves to 80: held stays on HOLD, because HS1 fired
    loose = regate(held, settings.gate_config(threshold_override=80))
    assert loose.threshold == 80
    assert loose.composite == pytest.approx(73.88, abs=0.01)
    assert loose.composite <= loose.threshold
    assert [h.id for h in loose.hard_stops_fired] == ["HS1"]
    assert loose.decision == "HOLD"

    # the slider moves to 2: cleared flips to HOLD
    tight = regate(cleared, settings.gate_config(threshold_override=2))
    assert tight.threshold == 2
    assert tight.composite == pytest.approx(3.75, abs=0.01)
    assert tight.decision == "HOLD"
    assert tight.hard_stops_fired == []

    # and back again: regate is a pure function of (run, cfg)
    again = regate(cleared, settings.gate_config())
    assert again.model_dump() == cleared.decision.model_dump()


# ---------------------------------------------------------------------------
# the rest of RFC 9.5
# ---------------------------------------------------------------------------

def test_risk_bands(cfg, gate_cfg) -> None:
    rec = record("cleared")
    cases = [(0.0, "low"), (24.9, "low"), (25.0, "medium"), (49.9, "medium"),
             (50.0, "high"), (74.9, "high"), (75.0, "very_high"), (100.0, "very_high")]
    for score, expected in cases:
        signals = [_signal(n, score) for n in
                   ("unattributed", "review_depth", "untested", "blast_radius")]
        assert decide(rec, signals, gate_cfg).risk_level == expected, score


def test_all_excluded_gives_a_composite_of_zero(cfg, gate_cfg) -> None:
    rec = strip_sessions("cleared", human_declared=True)
    signals = [_signal(n, 0.0, status="not_computable") for n in
               ("unattributed", "review_depth", "untested", "blast_radius")]
    decision = decide(rec, signals, gate_cfg)
    assert decision.composite == 0.0
    assert sorted(decision.excluded_signals) == [
        "blast_radius", "review_depth", "unattributed", "untested"
    ]
    assert decision.decision == "APPROVE"


def test_degraded_is_floored_under_penalize_and_not_under_neutral(cfg, gate_cfg) -> None:
    rec_penalize = strip_sessions("cleared")
    rec_neutral = strip_sessions("cleared", human_declared=True)
    signals = [
        _signal("unattributed", 5.0, status="degraded"),
        _signal("review_depth", 5.0, status="degraded"),
        _signal("untested", 5.0, status="degraded"),
        _signal("blast_radius", 5.0, status="degraded"),
    ]
    assert decide(rec_penalize, signals, gate_cfg).composite == pytest.approx(40.0)
    assert decide(rec_neutral, signals, gate_cfg).composite == pytest.approx(5.0)


def test_config_hash_is_stable_and_present(cfg, gate_cfg) -> None:
    rec = record("held")
    signals = run_signals(rec, cfg.signals, cfg.sensitive)
    first = decide(rec, signals, gate_cfg)
    second = decide(rec, signals, gate_cfg)
    assert first.config_hash == second.config_hash
    assert len(first.config_hash) == 64

    fallback_cfg = gate_cfg.model_copy(deep=True)
    fallback_cfg.config_hash = ""
    hashed = decide(rec, signals, fallback_cfg)
    assert len(hashed.config_hash) == 64

    moved = gate_cfg.model_copy(deep=True)
    moved.config_hash = ""
    moved.threshold = 99
    assert decide(rec, signals, moved).config_hash != hashed.config_hash
