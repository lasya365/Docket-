"""RFC 15.3: decide/ is deterministic and touches nothing (P4, RFC 0 rules 3 and 8)."""

from __future__ import annotations

import builtins
import json
import socket

import pytest

from fixtures.build_fixtures import record

from docket.decide.backout_planner import plan_backout, rollout_text
from docket.decide.engine import run_signals
from docket.decide.evidence_chain import build_chain
from docket.decide.gate import decide

NAMES = ("held", "cleared", "model-bump", "unmatched")


def _run(rec, cfg, gate_cfg) -> str:
    signals = run_signals(rec, cfg.signals, cfg.sensitive)
    decision = decide(rec, signals, gate_cfg)
    chain = build_chain(rec, signals)
    backout = plan_backout(rec)
    payload = {
        "signals": [json.loads(s.model_dump_json()) for s in signals],
        "decision": json.loads(decision.model_dump_json()),
        "chain": json.loads(chain.model_dump_json()),
        "backout": json.loads(backout.model_dump_json()),
        "rollout": rollout_text(rec),
    }
    return json.dumps(payload, sort_keys=True)


@pytest.mark.parametrize("name", NAMES)
def test_same_input_gives_byte_identical_output(name, cfg, gate_cfg) -> None:
    rec = record(name)
    assert _run(rec, cfg, gate_cfg) == _run(record(name), cfg, gate_cfg)


@pytest.mark.parametrize("name", NAMES)
def test_decide_runs_with_no_network_and_no_filesystem(name, cfg, gate_cfg, monkeypatch) -> None:
    rec = record(name)
    expected = _run(rec, cfg, gate_cfg)        # warm every lazy import first

    def no_network(*args, **kwargs):
        raise AssertionError("decide/ opened a socket")

    def no_files(*args, **kwargs):
        raise AssertionError("decide/ opened a file")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(builtins, "open", no_files)

    assert _run(rec, cfg, gate_cfg) == expected


def test_evidence_ids_are_unique_within_a_run(cfg) -> None:
    for name in NAMES:
        signals = run_signals(record(name), cfg.signals, cfg.sensitive)
        ids = [item.id for signal in signals for item in signal.evidence]
        assert len(ids) == len(set(ids)), f"{name} repeated an evidence id"
        for signal in signals:
            for item in signal.evidence:
                assert item.id.startswith(f"ev:{signal.signal}:")


def test_every_signal_result_is_in_range(cfg) -> None:
    for name in NAMES:
        signals = run_signals(record(name), cfg.signals, cfg.sensitive)
        assert [s.signal for s in signals] == [
            "unattributed",
            "review_depth",
            "untested",
            "blast_radius",
        ]
        for signal in signals:
            assert 0.0 <= signal.score <= 100.0
            assert signal.score == round(signal.score, 2)
            assert signal.headline and signal.summary
            if signal.status == "not_computable":
                assert signal.score == 0.0
