"""RFC 15.3: the Evidence Chain, and the no-code change's four missing links."""

from __future__ import annotations

import pytest

from fixtures.build_fixtures import record

from docket.decide.engine import run_signals
from docket.decide.evidence_chain import build_chain
from docket.models.change_record import JoinSection
from docket.models.common import CHAIN_ORDER


def chain_for(name: str, cfg):
    rec = record(name)
    signals = run_signals(rec, cfg.signals, cfg.sensitive)
    return rec, signals, build_chain(rec, signals)


@pytest.mark.parametrize("name", ["held", "cleared", "model-bump", "unmatched"])
def test_always_six_links_in_order(name, cfg) -> None:
    _rec, _signals, chain = chain_for(name, cfg)
    assert [link.name for link in chain.links] == list(CHAIN_ORDER)
    assert chain.missing_count == sum(1 for link in chain.links if not link.present)
    for link in chain.links:
        assert link.summary


def test_chain_non_code(cfg) -> None:
    """model-bump has exactly four missing links: intent, diff, review, deploy."""
    _rec, _signals, chain = chain_for("model-bump", cfg)
    missing = [link.name for link in chain.links if not link.present]
    assert missing == ["intent", "diff", "review", "deploy"]
    assert chain.missing_count == 4

    by_name = {link.name: link for link in chain.links}
    assert by_name["intent"].summary == "none: no requirement exists"
    assert by_name["diff"].summary == "none: nothing to review"
    assert by_name["review"].summary == "none: no reviewer, no approval"
    assert "already live across 3 workflows" in by_name["deploy"].summary

    # the session link is never missing for a no-code change: the manifest is the session
    assert by_name["session"].present
    assert by_name["session"].summary == "manifest · model: vendor-4.1 → vendor-4.2"
    assert by_name["verify"].summary == "rehearsed · 50 cases · 2 regressed"


def test_chain_for_a_code_change(cfg) -> None:
    _rec, _signals, chain = chain_for("held", cfg)
    by_name = {link.name: link for link in chain.links}
    assert by_name["intent"].present and by_name["intent"].summary.startswith("#114 · ")
    assert by_name["session"].summary == (
        "claude-code · 5 turns · 2 retries · 5 files · join verified"
    )
    assert by_name["diff"].summary == "PR #4471 · 812 lines · 604 AI-authored"
    assert by_name["review"].summary == "2 approvals · fastest 1m 49s"
    assert by_name["verify"].summary == "CI success · 31% on AI lines"
    assert by_name["deploy"].summary == "no planned window"
    assert chain.missing_count == 1


def test_unmatched_session_link(cfg) -> None:
    rec = record("cleared").model_copy(deep=True)
    rec.sessions = []
    rec.join = JoinSection(method="none", confidence="unmatched")
    signals = run_signals(rec, cfg.signals, cfg.sensitive)
    link = next(l for l in build_chain(rec, signals).links if l.name == "session")
    assert not link.present
    assert link.summary == "UNMATCHED: no agent session found"

    rec.join.human_declared = True
    link = next(
        l
        for l in build_chain(rec, run_signals(rec, cfg.signals, cfg.sensitive)).links
        if l.name == "session"
    )
    assert link.summary == "declared human-authored"


def test_ghost_sessions_appear_in_the_session_detail(cfg) -> None:
    rec = record("held").model_copy(deep=True)
    ghost = rec.sessions[0].model_copy(deep=True)
    ghost.present = False
    ghost.session_id = "ghost-1"
    rec.sessions.append(ghost)
    signals = run_signals(rec, cfg.signals, cfg.sensitive)
    link = next(l for l in build_chain(rec, signals).links if l.name == "session")
    assert link.present
    assert "ghost-1" in link.detail


def test_evidence_refs_point_at_real_evidence_ids(cfg) -> None:
    for name in ("held", "cleared", "model-bump", "unmatched"):
        rec, signals, chain = chain_for(name, cfg)
        known = {item.id for signal in signals for item in signal.evidence}
        for link in chain.links:
            assert set(link.evidence_refs) <= known, (name, link.name)
