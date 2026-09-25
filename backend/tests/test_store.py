"""The SQLite store (RFC 10.6). Offline: the store touches nothing but its file."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from docket.collectors.otlp import FlatEvent
from docket.models.fragments import EditEvent
from docket.models.manifest import AgentManifest, ManifestSnapshot, PromptRef
from docket.record.store import Store

from fixtures.build_fixtures import record
from fixtures.runs import make_run

T0 = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "docket.sqlite")
    yield s
    s.close()


def _snapshot(agent_id: str, sequence: int, model: str) -> ManifestSnapshot:
    manifest = AgentManifest(agent_id=agent_id, model=model, prompt=PromptRef(sha256="a" * 64))
    return ManifestSnapshot(
        agent_id=agent_id,
        manifest_hash=f"{sequence:064d}",
        manifest=manifest,
        observed_at=T0 + timedelta(hours=sequence),
        sequence=sequence,
    )


def test_tables_are_created_idempotently(tmp_path):
    path = tmp_path / "docket.sqlite"
    first = Store(path)
    first.set_override("threshold", 45)
    first.close()

    second = Store(path)                      # opening again must not wipe or fail
    assert second.get_override("threshold") == 45
    second.close()


def test_run_round_trip(store):
    run = make_run("held")
    store.save_run(run)

    loaded = store.latest_run(run.change_key)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == run.model_dump(mode="json")
    assert store.latest_run("pr-nothing-here-1") is None
    assert store.get_run(run.run_id).run_id == run.run_id


def test_latest_run_is_the_newest_and_runs_for_lists_history(store):
    first = make_run("held")
    second = make_run("held", run_id="run-held-0002",
                      created_at=first.created_at + timedelta(minutes=5))
    store.save_run(first)
    store.save_run(second)

    assert store.latest_run(first.change_key).run_id == "run-held-0002"
    history = store.runs_for(first.change_key)
    assert [r["run_id"] for r in history] == ["run-held-0002", "run-held-0001"]
    assert history[0]["decision"] == "HOLD"
    assert history[0]["seal"].startswith("sha256:")


def test_list_changes_is_latest_per_change_newest_first(store):
    held = make_run("held")
    cleared = make_run("cleared", created_at=held.created_at + timedelta(minutes=1))
    bump = make_run("model-bump", created_at=held.created_at + timedelta(minutes=2))
    stale_held = make_run("held", run_id="run-held-0000",
                          created_at=held.created_at - timedelta(hours=1))
    for run in (stale_held, held, cleared, bump):
        store.save_run(run)

    rows = store.list_changes()
    assert [r["change_key"] for r in rows] == [
        bump.change_key, cleared.change_key, held.change_key,
    ]
    row = next(r for r in rows if r["change_key"] == held.change_key)
    assert set(row) == {
        # the field list in RFC 11 ...
        "change_key", "title", "kind", "decision", "composite", "risk_level",
        "join_confidence", "missing_links", "filed_by_docket", "freshservice_id",
        "updated_at", "simulated",
        # ... plus the hard stops, which RFC 12's threshold slider needs to preview
        # "HOLD if composite > value OR any hard stop fired" without a round trip.
        "hard_stops",
    }
    assert row["decision"] == "HOLD"
    assert row["kind"] == "code"
    assert row["filed_by_docket"] is False
    assert row["missing_links"] == held.chain.missing_count
    assert row["join_confidence"] == held.record.join.confidence
    assert row["simulated"] is False

    bump_row = next(r for r in rows if r["change_key"] == bump.change_key)
    assert bump_row["filed_by_docket"] is True          # no-code kinds are filed by Docket


def test_list_changes_flags_simulated_sections_and_the_ticket_id(store):
    rec = record("held")
    rec.verify.provenance = "simulated"
    run = make_run("held", record=rec)
    store.save_run(run)
    store.save_ticket(run.change_key, 4242, "https://example.freshservice.com/a/changes/4242")

    row = store.list_changes()[0]
    assert row["simulated"] is True
    assert row["freshservice_id"] == 4242


def test_status_stages(store):
    run = make_run("held")
    store.set_status(run.run_id, "collecting")
    assert store.get_status(run.run_id)["stage"] == "collecting"
    assert store.get_status(run.run_id)["change_key"] is None   # the run is not saved yet

    store.save_run(run)
    store.set_status(run.run_id, "done", "4 signals scored")
    status = store.get_status(run.run_id)
    assert status["stage"] == "done"
    assert status["message"] == "4 signals scored"
    assert status["change_key"] == run.change_key
    assert store.get_status("no-such-run") is None


def test_tickets(store):
    assert store.ticket_for("pr-a-b-1") is None
    store.save_ticket("pr-a-b-1", 7, "https://example.freshservice.com/a/changes/7")
    store.save_ticket("pr-a-b-1", 8, "https://example.freshservice.com/a/changes/8")
    assert store.ticket_for("pr-a-b-1") == {
        "change_key": "pr-a-b-1",
        "freshservice_id": 8,
        "url": "https://example.freshservice.com/a/changes/8",
    }


def test_manifest_ledger(store):
    store.append_snapshot(_snapshot("resolution-agent", 1, "vendor-4.1"))
    store.append_snapshot(_snapshot("resolution-agent", 2, "vendor-4.2"))
    store.append_snapshot(_snapshot("other-agent", 1, "vendor-3.0"))

    assert store.latest_snapshot("resolution-agent").manifest.model == "vendor-4.2"
    assert store.latest_snapshot("unknown-agent") is None
    assert [s.sequence for s in store.ledger("resolution-agent")] == [2, 1]
    assert len(store.ledger()) == 3


def test_overrides(store):
    assert store.get_override("threshold") is None
    assert store.get_override("threshold", 45) == 45
    store.set_override("threshold", 80.5)
    assert store.get_override("threshold") == 80.5
    store.set_override("threshold", 2)
    assert store.get_override("threshold") == 2


def test_events_edits_and_session_parts(store):
    store.save_event(FlatEvent("user_prompt", T0, {"session.id": "s1", "prompt.id": "p1"}))
    store.save_event(FlatEvent("tool_result", T0 + timedelta(seconds=5), {
        "session.id": "s1", "tool_name": "Edit",
        "vcs.repository.url.full": "https://github.com/acme/provisioning-service",
    }))
    store.save_event(FlatEvent("api_request", T0, {"model": "vendor-4.2"}))   # no session.id
    store.save_edit_event(EditEvent(
        session_id="s1", tool_use_id="t1", prompt_id="p1", tool_name="Edit",
        file_path="provisioning/sync.py", written_lines=["a = 1"],
        timestamp=T0 + timedelta(seconds=6),
    ))
    store.upsert_session_meta(
        "s1", {"repo_root": "/repo", "cwd": "/repo", "permission_mode": "default"}, seen_at=T0
    )
    store.upsert_session_meta(
        "s1", {"permission_mode": "acceptEdits"}, seen_at=T0 + timedelta(seconds=9)
    )

    events, edits, meta = store.load_session_parts("s1")
    assert [e.name for e in events] == ["user_prompt", "tool_result"]
    assert events[0].attrs["prompt.id"] == "p1"
    assert events[0].attrs["session.id"] == "s1"
    assert len(edits) == 1 and edits[0].file_path == "provisioning/sync.py"
    assert meta["repo_root"] == "/repo"
    assert meta["modes"] == ["default", "acceptEdits"]

    rows = store.list_sessions()
    assert [r["session_id"] for r in rows] == ["s1"]        # "_none" is never assembled
    assert set(rows[0]) == {"session_id", "started_at", "ended_at", "events", "edits", "repo_url"}
    assert rows[0]["events"] == 2 and rows[0]["edits"] == 1
    assert rows[0]["repo_url"] == "https://github.com/acme/provisioning-service"

    assert store.sessions_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1)) == ["s1"]
    assert store.sessions_between(T0 + timedelta(days=1), T0 + timedelta(days=2)) == []


def test_save_event_accepts_anything_shaped_like_a_flat_event(store):
    class Duck:
        name = "tool_result"
        timestamp = T0
        attrs = {"session.id": "s2", "tool_name": "Bash"}

    store.save_event(Duck())
    events, _, _ = store.load_session_parts("s2")
    assert [e.name for e in events] == ["tool_result"]
    assert events[0].attrs["tool_name"] == "Bash"


def test_reset_runs_keeps_the_collected_evidence(store):
    run = make_run("held")
    store.save_run(run)
    store.set_status(run.run_id, "done")
    store.save_ticket(run.change_key, 512, "https://example.freshservice.com/a/changes/512")
    store.save_event(FlatEvent("user_prompt", T0, {"session.id": "s1"}))
    store.upsert_session_meta("s1", {"cwd": "/repo"}, seen_at=T0)
    store.append_snapshot(_snapshot("resolution-agent", 1, "vendor-4.1"))

    store.reset_runs()

    assert store.list_changes() == []
    assert store.latest_run(run.change_key) is None
    assert store.get_status(run.run_id) is None
    assert store.ticket_for(run.change_key) is None
    # evidence and the ledger survive
    assert store.list_sessions()[0]["events"] == 1
    assert store.session_meta("s1")["cwd"] == "/repo"
    assert len(store.ledger()) == 1


def test_coverage_round_trip(store):
    assert store.posted_coverage("abc") is None
    store.save_coverage("abc", {"files": {"a.py": {"executed": [1], "missing": [2]}}})
    store.save_coverage("abc", {"files": {"a.py": {"executed": [1, 2], "missing": []}}})
    assert store.posted_coverage("abc")["files"]["a.py"]["executed"] == [1, 2]
    assert store.coverage_for("abc") == store.posted_coverage("abc")


def test_save_event_and_session_meta_take_the_explicit_row_form(store):
    """server.py passes (session_id, name, ts, attrs) and meta as keywords."""
    store.save_event("s3", "user_prompt", T0, {"session.id": "s3", "prompt.id": "p9"})
    store.upsert_session_meta("s3", cwd="/w", repo_root="/w", permission_mode="plan")

    events, _, meta = store.load_session_parts("s3")
    assert [e.name for e in events] == ["user_prompt"]
    assert events[0].attrs["prompt.id"] == "p9"
    assert meta["cwd"] == "/w" and meta["repo_root"] == "/w"
    assert meta["modes"] == ["plan"]
