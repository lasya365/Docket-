"""RFC 15.2: the golden table, to within 0.01, with the shipped config.json."""

from __future__ import annotations

import pytest

from fixtures.build_fixtures import record

from docket.decide.engine import run_signals
from docket.decide.gate import decide

# fixture -> {signal: (score, status)}, composite, decision, hard stops
GOLDEN = {
    "held": {
        "signals": {
            "unattributed": (56.29, "computed"),
            "review_depth": (95.17, "computed"),
            "untested": (69.04, "computed"),
            "blast_radius": (75.00, "computed"),
        },
        "composite": 73.88,
        "decision": "HOLD",
        "hard_stops": ["HS1"],
        "risk": "high",
    },
    "cleared": {
        "signals": {
            "unattributed": (0.00, "computed"),
            "review_depth": (10.00, "computed"),
            "untested": (5.00, "computed"),
            "blast_radius": (0.00, "computed"),
        },
        "composite": 3.75,
        "decision": "APPROVE",
        "hard_stops": [],
        "risk": "low",
    },
    "model-bump": {
        "signals": {
            "unattributed": (100.00, "computed"),
            "review_depth": (100.00, "computed"),
            "untested": (48.00, "degraded"),
            "blast_radius": (90.00, "computed"),
        },
        "composite": 84.50,
        "decision": "HOLD",
        "hard_stops": ["HS1"],
        "risk": "very_high",
    },
}


@pytest.fixture()
def results(cfg, gate_cfg):
    out = {}
    for name in GOLDEN:
        rec = record(name)
        signals = run_signals(rec, cfg.signals, cfg.sensitive)
        out[name] = (rec, signals, decide(rec, signals, gate_cfg))
    return out


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_signal_scores(results, name) -> None:
    _rec, signals, _decision = results[name]
    by_name = {s.signal: s for s in signals}
    for signal_name, (score, status) in GOLDEN[name]["signals"].items():
        assert by_name[signal_name].score == pytest.approx(score, abs=0.01), signal_name
        assert by_name[signal_name].status == status, signal_name


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_gate_outcome(results, name) -> None:
    _rec, _signals, decision = results[name]
    expected = GOLDEN[name]
    assert decision.composite == pytest.approx(expected["composite"], abs=0.01)
    assert decision.decision == expected["decision"]
    assert [h.id for h in decision.hard_stops_fired] == expected["hard_stops"]
    assert decision.risk_level == expected["risk"]
    assert decision.excluded_signals == []
    assert decision.config_hash


def test_held_arithmetic_is_traceable(results) -> None:
    """RFC 15.2's own check on the implementation: every number can be opened."""
    _rec, signals, _decision = results["held"]
    by_name = {s.signal: s for s in signals}

    unattributed = by_name["unattributed"]
    assert unattributed.headline == "340 / 604"
    scope_item = next(i for i in unattributed.evidence if "out_lines" in i.data)
    assert scope_item.data["out_lines"] == 340
    assert scope_item.data["ai_lines"] == 604
    out_files = {i.data["path"] for i in unattributed.evidence if i.kind == "file"}
    assert out_files == {"entitlement/grants.py", "cache/reconcile.py"}

    review = by_name["review_depth"]
    asks = sorted(i.data["ask_to_approve_s"] for i in review.evidence if "ask_to_approve_s" in i.data)
    assert asks == [109, 112]
    slowest = next(i for i in review.evidence if i.data.get("ask_to_approve_s") == 112)
    assert slowest.data["expected_s"] == pytest.approx(1624.0)
    assert slowest.data["speed_score"] == pytest.approx(93.10, abs=0.01)
    assert review.headline == "1m 49s"          # the smallest ask_to_approve_s

    untested = by_name["untested"]
    summary = next(i for i in untested.evidence if "ai_executable" in i.data)
    assert (summary.data["ai_covered"], summary.data["ai_executable"]) == (187, 604)
    assert untested.headline == "31%"

    blast = by_name["blast_radius"]
    assert blast.evidence[0].data["display_name"] == "entitlement_svc.grant"
    assert blast.evidence[0].data["points"] == 40
    path_item = next(i for i in blast.evidence if i.kind == "file")
    assert path_item.data["pattern"] == "**/entitlement/**"
    assert path_item.data["points"] == 25
    auto = next(i for i in blast.evidence if "ratio" in i.data)
    assert auto.data["points"] == 10
    assert blast.headline == "2 sensitive"


def test_untested_shows_the_file_average_next_to_the_ai_coverage(results) -> None:
    """The contrast this product exists to show (RFC 9.3)."""
    _rec, signals, _decision = results["held"]
    untested = next(s for s in signals if s.signal == "untested")
    rows = {i.data["path"]: i.data for i in untested.evidence if "file_average_pct" in i.data}
    assert set(rows) == {
        "provisioning/sync.py",
        "provisioning/backoff.py",
        "entitlement/grants.py",
        "cache/reconcile.py",
    }
    grants = rows["entitlement/grants.py"]
    assert grants["ai_coverage_pct"] == pytest.approx(0.0)
    # the number the dashboard showed the board before this change landed, next to
    # the number for the lines the agent actually wrote
    assert grants["rest_of_file_pct"] > 80.0
    assert grants["file_average_pct"] > grants["ai_coverage_pct"]
    sync = rows["provisioning/sync.py"]
    assert sync["rest_of_file_pct"] > sync["ai_coverage_pct"] == pytest.approx(65.0)


def test_labels_vary_by_kind(results) -> None:
    code_labels = {s.signal: s.label for s in results["held"][1]}
    assert code_labels == {
        "unattributed": "Unattributed change",
        "review_depth": "Review depth",
        "untested": "Untested generation",
        "blast_radius": "Blast radius",
    }
    non_code_labels = {s.signal: s.label for s in results["model-bump"][1]}
    assert non_code_labels == {
        "unattributed": "No stated intent",
        "review_depth": "No reviewable artifact",
        "untested": "Unverified behaviour",
        "blast_radius": "Blast radius",
    }


def test_model_bump_rehearsal_arithmetic(results) -> None:
    _rec, signals, _decision = results["model-bump"]
    untested = next(s for s in signals if s.signal == "untested")
    data = next(i.data for i in untested.evidence if "cases" in i.data)
    assert (data["cases"], data["regressed"], data["changed_acceptable"]) == (50, 2, 4)
    assert data["regressed_term"] == pytest.approx(40.0)
    assert data["changed_term"] == pytest.approx(8.0)
    assert untested.status == "degraded"          # the corpus is simulated

    blast = next(s for s in signals if s.signal == "blast_radius")
    base = next(i.data for i in blast.evidence if "non_code_base" in i.data)
    assert base["non_code_base"] == 90
    workflows = next(i.data for i in blast.evidence if "workflows" in i.data)
    assert len(workflows["workflows"]) == 3
    assert blast.headline == "3 workflows"
