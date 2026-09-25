"""Integration: the whole pipeline over the seeded cases (RFC sections 14, 15.3).

Two runs of the same machinery:
  * `test_golden_end_to_end` drives the TEST fixtures (provenance="real") through
    planes 2 and 3 and asserts the RFC 15.2 golden table to within 0.01. This is
    the cross-check that the real plane-2 path reproduces the record the decide
    plane was calibrated against.
  * `test_demo_seed_four_changes` drives `demo/seed/` (provenance="simulated")
    through the server route, which is the demo itself.
"""

from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient

from docket import pipeline
from docket.decide.engine import run_signals
from docket.decide.gate import decide
from docket.record.store import Store
from docket.server import create_app
from docket.settings import load_settings
from fixtures.build_fixtures import change_key_for, fragments, record

GOLDEN = {
    "held": {
        "unattributed": 56.29,
        "review_depth": 95.17,
        "untested": 69.04,
        "blast_radius": 75.00,
        "composite": 73.88,
        "decision": "HOLD",
        "hard_stops": ["HS1"],
    },
    "cleared": {
        "unattributed": 0.00,
        "review_depth": 10.00,
        "untested": 5.00,
        "blast_radius": 0.00,
        "composite": 3.75,
        "decision": "APPROVE",
        "hard_stops": [],
    },
    "model-bump": {
        "unattributed": 100.00,
        "review_depth": 100.00,
        "untested": 48.00,
        "blast_radius": 90.00,
        "composite": 84.50,
        "decision": "HOLD",
        "hard_stops": ["HS1"],
    },
}


def _record_via_plane_two(name: str, cfg):
    """The real plane-2 path: correlate -> attribute -> build."""
    from docket.correlate.correlator import correlate
    from docket.correlate.line_attributor import attribute
    from docket.correlate.record_builder import build_code, build_non_code

    parts = fragments(name)
    if "non_code_change" in parts:
        return build_non_code(parts["non_code_change"], cfg)
    joins = correlate(parts["repo_fragment"], parts["sessions"], cfg.correlator)
    attribution = attribute(parts["repo_fragment"], joins, cfg.attributor)
    return build_code(parts["repo_fragment"], joins, attribution, parts["coverage"], cfg)


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_golden_end_to_end(name, settings):
    """The plane-2 path reproduces the record the golden table was calibrated on."""
    cfg = settings.config
    built = _record_via_plane_two(name, cfg)
    direct = record(name)

    assert built.change_key == direct.change_key
    assert built.kind == direct.kind
    if built.kind == "code":
        assert built.diff.lines_added == direct.diff.lines_added
        assert built.diff.totals == direct.diff.totals
        assert built.diff.attribution_by_line == direct.diff.attribution_by_line
        assert [(a.reviewer, a.ask_to_approve_s) for a in built.review.approvers] == [
            (a.reviewer, a.ask_to_approve_s) for a in direct.review.approvers
        ]
        assert built.join.confidence == direct.join.confidence == "verified"

    signals = run_signals(built, cfg.signals, cfg.sensitive)
    scores = {s.signal: s.score for s in signals}
    want = GOLDEN[name]
    for signal_name in ("unattributed", "review_depth", "untested", "blast_radius"):
        assert scores[signal_name] == pytest.approx(want[signal_name], abs=0.01), (
            f"{name}.{signal_name}: got {scores[signal_name]}, want {want[signal_name]}"
        )

    decision = decide(built, signals, settings.gate_config())
    assert decision.composite == pytest.approx(want["composite"], abs=0.01)
    assert decision.decision == want["decision"]
    assert [h.id for h in decision.hard_stops_fired] == want["hard_stops"]


def test_signals_are_identical_from_both_record_paths(settings):
    """Whichever way the record was built, decide/ must say the same thing."""
    cfg = settings.config
    for name in ("held", "cleared"):
        a = run_signals(_record_via_plane_two(name, cfg), cfg.signals, cfg.sensitive)
        b = run_signals(record(name), cfg.signals, cfg.sensitive)
        assert [(s.signal, s.score, s.status) for s in a] == [(s.signal, s.score, s.status) for s in b]


# ---------------------------------------------------------------------------
# the demo path, through the server
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, repo_root):
    settings = load_settings(repo_root / "config.json", env={"DOCKET_DB": str(tmp_path / "t.sqlite")})
    store = Store(tmp_path / "t.sqlite")
    deps = pipeline.Deps(settings=settings, store=store, mode="replay")
    app = create_app(settings=settings, store=store, deps=deps)
    with TestClient(app) as c:
        yield c
    store.close()


def test_demo_seed_four_changes(client):
    body = client.post("/demo/seed").json()
    changes = body["changes"]
    assert len(changes) == 4, changes
    assert not [c for c in changes if "error" in c], changes

    by_seed = {c["seed"]: c for c in changes}
    assert by_seed["held"]["decision"] == "HOLD"
    assert by_seed["cleared"]["decision"] == "APPROVE"
    assert by_seed["model-bump"]["decision"] == "HOLD"
    assert by_seed["unmatched"]["decision"] == "HOLD"

    rows = client.get("/changes").json()
    assert len(rows) == 4
    assert all(row["simulated"] for row in rows), "seeded fragments are simulated and must say so (P8)"
    assert [r for r in rows if r["filed_by_docket"]], "the manifest change is filed by Docket"


def test_seeded_change_detail_and_seal(client):
    client.post("/demo/seed")
    key = change_key_for("held")
    run = client.get(f"/changes/{key}").json()
    assert run["decision"]["decision"] == "HOLD"
    assert len(run["signals"]) == 4
    assert len(run["chain"]["links"]) == 6
    assert run["seal"].startswith("sha256:")
    assert client.get(f"/changes/{key}/verify").json()["match"] is True


def test_threshold_slider_regates(client):
    client.post("/demo/seed")
    cleared = change_key_for("cleared")
    assert client.get(f"/changes/{cleared}").json()["decision"]["decision"] == "APPROVE"

    out = client.put("/config/threshold", json={"threshold": 2}).json()
    flipped = {row["change_key"]: row["decision"] for row in out}
    assert flipped[cleared] == "HOLD"
    assert client.get(f"/changes/{cleared}/verify").json()["match"] is True

    held = change_key_for("held")
    out = client.put("/config/threshold", json={"threshold": 80}).json()
    flipped = {row["change_key"]: row["decision"] for row in out}
    assert flipped[held] == "HOLD", "HS1 holds it whatever the threshold says"


def test_model_bump_chain_has_four_missing_links(client):
    client.post("/demo/seed")
    key = change_key_for("model-bump")
    run = client.get(f"/changes/{key}").json()
    missing = sorted(l["name"] for l in run["chain"]["links"] if not l["present"])
    assert missing == ["deploy", "diff", "intent", "review"]
    assert run["chain"]["missing_count"] == 4
    assert run["backout"]["available"] is True
    assert run["backout"]["steps"], "a bundle change must offer a real backout"


def test_unmatched_is_held_by_hard_stop(client):
    client.post("/demo/seed")
    key = change_key_for("unmatched")
    run = client.get(f"/changes/{key}").json()
    assert run["decision"]["decision"] == "HOLD"
    assert "HS3" in [h["id"] for h in run["decision"]["hard_stops_fired"]]
    assert run["record"]["join"]["confidence"] == "unmatched"


def test_run_route_in_replay_mode_finds_the_seed_instead_of_github(tmp_path, repo_root):
    """DOCKET_MODE=replay makes POST /run/{pr} look for a seed first (RFC section 14)."""
    settings = load_settings(
        repo_root / "config.json",
        env={"DOCKET_DB": str(tmp_path / "r.sqlite"), "DOCKET_MODE": "replay"},
    )
    store = Store(tmp_path / "r.sqlite")
    # github is None on purpose: if the route reached the collector it would fail.
    deps = pipeline.Deps(settings=settings, store=store, mode="replay")
    with TestClient(create_app(settings=settings, store=store, deps=deps)) as c:
        body = c.post("/run/4471?repo=acme/provisioning-service").json()
        assert c.get(f"/status/{body['run_id']}").json()["stage"] == "done"
        run = c.get(f"/changes/{body['change_key']}").json()
        assert run["mode"] == "replay"
        assert run["decision"]["decision"] == "HOLD"
    store.close()


def test_replay_writes_nothing_outside_the_store(client, tmp_path, repo_root):
    """Replayed runs are dry-run for Freshservice and GitHub (RFC section 14)."""
    before = {p.name for p in (repo_root / "out" / "freshservice").glob("*.json")}
    client.post("/demo/seed")
    after = {p.name for p in (repo_root / "out" / "freshservice").glob("*.json")}
    assert before == after


def test_queue_rows_carry_hard_stops_so_the_slider_can_preview_honestly(client):
    """RFC 12: dragging previews HOLD if composite > value OR any hard stop fired."""
    client.post("/demo/seed")
    rows = {r["change_key"]: r for r in client.get("/changes").json()}

    held = rows[change_key_for("held")]
    assert held["hard_stops"] == ["HS1"]
    unmatched = rows[change_key_for("unmatched")]
    assert "HS3" in unmatched["hard_stops"]
    assert rows[change_key_for("cleared")]["hard_stops"] == []

    # With the threshold at 100 nothing is over the line, so only a hard stop can hold
    # a change — which is exactly what the browser-side preview has to reproduce.
    out = {r["change_key"]: r["decision"] for r in client.put("/config/threshold", json={"threshold": 100}).json()}
    assert out[change_key_for("held")] == "HOLD"
    assert out[change_key_for("cleared")] == "APPROVE"
