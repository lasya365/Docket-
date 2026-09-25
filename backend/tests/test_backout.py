"""RFC 9.8: the Backout Planner and the rollout text."""

from __future__ import annotations

import pytest

from fixtures.build_fixtures import record

from docket.decide.backout_planner import plan_backout, rollout_text
from docket.models.change_record import DeploySection


def test_non_code_backout_restores_the_previous_bundle() -> None:
    rec = record("model-bump")
    plan = plan_backout(rec)

    assert plan.available
    assert plan.summary == "Restore agent bundle #2 → #1"
    assert [(s.key, s.from_value, s.to_value) for s in plan.steps] == [
        ("model", "vendor-4.2", "vendor-4.1")
    ]
    assert plan.naive_plan is None
    assert "vendor-4.2 → vendor-4.1" in plan.text
    assert plan.text.splitlines()[0] == plan.summary


def test_non_code_backout_without_a_previous_snapshot() -> None:
    rec = record("model-bump").model_copy(deep=True)
    rec.manifest.before = None
    rec.manifest.previous_available = False
    plan = plan_backout(rec)

    assert not plan.available
    assert plan.steps == []
    assert plan.text == (
        "No earlier bundle is on record. This is the first time Docket has seen "
        "this agent."
    )


def test_a_tools_change_spells_out_the_delta() -> None:
    rec = record("model-bump").model_copy(deep=True)
    rec.manifest.changed_keys = ["tools"]
    rec.manifest.after.manifest.tools = ["kb.search", "ticket.update", "entitlement.grant"]
    plan = plan_backout(rec)

    step = plan.steps[0]
    assert step.key == "tools"
    assert "entitlement.grant" in step.from_value
    assert "entitlement.grant" not in step.to_value
    assert "-entitlement.grant" in plan.text


@pytest.mark.parametrize("name", ["held", "cleared"])
def test_code_backout_names_the_agent_that_wrote_it(name) -> None:
    rec = record(name)
    plan = plan_backout(rec)
    number = rec.change_key.rsplit("-", 1)[-1]

    assert plan.available
    assert plan.naive_plan is not None
    assert plan.naive_plan.startswith(f"Revert PR #{number} (")
    assert [s.key for s in plan.steps] == ["code"]
    assert plan.why_naive_fails is not None
    assert "does not change the agent that wrote it" in plan.why_naive_fails
    assert "claude-code" in plan.why_naive_fails
    assert plan.why_naive_fails in plan.text


def test_code_backout_without_a_session_has_no_why_naive_fails() -> None:
    rec = record("unmatched")
    plan = plan_backout(rec)
    assert plan.available
    assert plan.why_naive_fails is None
    assert plan.naive_plan is not None


def test_rollout_text() -> None:
    assert rollout_text(record("held")) == (
        "Merge PR #4471 into the base branch after CAB approval."
    )
    text = rollout_text(record("model-bump"))
    assert text.startswith("Apply bundle #2 to resolution-agent. Affects: ")
    assert "incident-triage" in text


def test_rollout_text_carries_the_deploy_window() -> None:
    rec = record("held").model_copy(deep=True)
    rec.deploy = DeploySection(present=True, target="prod-eu", window="Sat 02:00-03:00")
    text = rollout_text(rec)
    assert "prod-eu" in text and "Sat 02:00-03:00" in text


def test_every_plan_has_text_a_board_can_paste() -> None:
    for name in ("held", "cleared", "model-bump", "unmatched"):
        plan = plan_backout(record(name))
        assert plan.text.strip()
        assert plan.summary.strip()
        assert rollout_text(record(name)).strip()
