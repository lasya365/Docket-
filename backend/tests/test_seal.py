"""The seal (RFC 10.5, RFC 15.3 `test_seal`)."""

from __future__ import annotations

import json

from docket.models.run import Advisory, RunOutputs
from docket.record.seal import canonical_json, seal, verify_seal

from fixtures.runs import make_run


def test_the_same_run_gives_the_same_seal():
    first = make_run("held")
    second = make_run("held")
    assert seal(first) == seal(second)
    assert seal(first).startswith("sha256:")
    assert len(seal(first)) == len("sha256:") + 64
    assert seal(first) == seal(first)          # and it is stable across calls


def test_changing_one_evidence_text_changes_the_seal():
    run = make_run("held")
    before = seal(run)
    run.signals[0].evidence[0].text = run.signals[0].evidence[0].text + " (edited)"
    assert seal(run) != before


def test_changing_the_advisory_does_not_change_the_seal():
    run = make_run("held")
    before = seal(run)
    run.advisory = [Advisory(source="llm", model="claude-sonnet-5", kind="brief", text="hello")]
    run.outputs = RunOutputs(freshservice_change_id=77, github_status_state="pending",
                             errors=["something went wrong"])
    assert seal(run) == before


def test_verify_follows_the_stored_seal():
    run = make_run("held")
    assert verify_seal(run) is True
    run.record.title = "Renamed after the decision"
    assert verify_seal(run) is False
    run.seal = ""
    assert verify_seal(run) is False


def test_the_canonical_json_is_sorted_compact_and_only_the_sealed_keys():
    run = make_run("held")
    text = canonical_json(run)
    payload = json.loads(text)
    # sort_keys=True, separators=(",", ":"), ensure_ascii=False
    assert text == json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert list(payload) == ["backout", "chain", "decision", "record", "signals"]
    assert "advisory" not in payload and "outputs" not in payload


def test_a_decision_change_changes_the_seal():
    run = make_run("held")
    before = seal(run)
    run.decision.composite = run.decision.composite + 0.01
    assert seal(run) != before
