"""The Freshservice writer (RFC 10.3, RFC 15.3 `test_freshservice_payload`).

Offline: the httpx client and the clock are injected, so nothing here reaches the
network and no assertion depends on the wall clock.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

import pytest

from docket.models.run import Advisory
from docket.record.freshservice import ADVISORY_HEADING, FreshserviceWriter
from docket.record.store import Store

from fixtures.build_fixtures import record
from fixtures.runs import make_run

NOW = datetime(2026, 9, 21, 9, 0, 0, tzinfo=timezone.utc)
API_KEY = "fs-secret-key-never-logged"


def clock():
    return NOW


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeClient:
    """Stands in for httpx.Client. Records every request, answers from a queue."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def request(self, method, url, json=None, auth=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json, "auth": auth})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, {"change": {"id": 9001}})


class ExplodingClient:
    def request(self, *args, **kwargs):
        raise ConnectionError("no network in tests")


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "docket.sqlite")
    yield s
    s.close()


@pytest.fixture()
def live_settings(settings):
    settings.freshservice_api_key = API_KEY
    settings.config.freshservice.enabled = True
    settings.config.freshservice.domain = "acme.freshservice.com"
    return settings


def writer(settings, tmp_path, store=None, client=None, status_writer=None):
    return FreshserviceWriter(
        settings, store=store, client=client, clock=clock,
        status_writer=status_writer, out_dir=tmp_path / "out" / "freshservice",
    )


def dry_run_document(settings, tmp_path, run, store=None):
    w = writer(settings, tmp_path, store=store)
    w.write(run)
    return json.loads(w.dry_run_path(run).read_text(encoding="utf-8"))


# ---- the payload ---------------------------------------------------------


def test_dry_run_payload_has_nested_plans_both_dates_and_the_config_risk_code(
    settings, tmp_path, store
):
    run = make_run("held")
    document = dry_run_document(settings, tmp_path, run, store)
    payload = document["payload"]

    # nested planning fields (RFC 10.3: Freshservice rejects a flat backout_plan)
    assert payload["planning_fields"]["backout_plan"]["description"]
    assert run.rollout_text in payload["planning_fields"]["rollout_plan"]["description"]
    assert "CAB approves" in payload["planning_fields"]["rollout_plan"]["description"]   # HOLD gate
    assert payload["planning_fields"]["reason_for_change"]["description"]
    assert payload["planning_fields"]["change_impact"]["description"]

    # both dates, from the injected clock, at the configured offset and window
    fs = settings.config.freshservice
    assert payload["planned_start_date"] == "2026-09-22T09:00:00Z"      # now + 24 h
    assert payload["planned_end_date"] == "2026-09-22T10:00:00Z"        # + 1 h window
    assert fs.planned_start_offset_hours == 24 and fs.planned_window_hours == 1

    # every code comes from config, nothing is hard-coded (RFC section 0 rule 4)
    assert payload["risk"] == fs.risk_codes[run.decision.risk_level] == 3
    assert payload["priority"] == fs.defaults.priority
    assert payload["impact"] == fs.defaults.impact
    assert payload["status"] == fs.defaults.status
    assert payload["change_type"] == fs.defaults.change_type
    assert payload["requester_id"] == fs.requester_id

    # custom fields, by the configured names
    fields = payload["custom_fields"]
    assert fields[fs.custom_fields.decision] == "HOLD"
    assert fields[fs.custom_fields.score] == pytest.approx(73.88)
    assert fields[fs.custom_fields.seal] == run.seal
    assert fields[fs.custom_fields.source_url] == run.record.source_url

    assert payload["subject"] == f"[Docket] {run.record.title}"
    assert document["method"] == "POST" and document["url"].endswith("/api/v2/changes")
    assert run.outputs.freshservice_change_id == 0                      # the fake dry-run id


def test_the_description_and_the_note_escape_untrusted_text(settings, tmp_path):
    rec = record("held")
    rec.intent.text = '<script>alert("pwn")</script> & friends'
    run = make_run("held", record=rec)
    run.signals[0].evidence[0].text = "<img src=x onerror=alert(1)>"
    run.advisory = [Advisory(
        source="llm", model="claude-sonnet-5", kind="question",
        text="<b>Who reviewed entitlement/grants.py?</b>", evidence_refs=["ev:untested:1"],
    )]

    document = dry_run_document(settings, tmp_path, run)
    payload = document["payload"]
    description = payload["description"]
    note = document["note"]["body"]

    assert "<script>" not in description and "<script>" not in note
    assert "&lt;script&gt;" in payload["planning_fields"]["reason_for_change"]["description"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in note
    assert "&lt;b&gt;" in note

    # and the parts RFC 10.3 asks for are all there
    assert "HOLD" in description
    assert "73.88" in description
    for signal in run.signals:
        assert signal.label in description
    assert "<b>MISSING</b>" in description                  # the deploy link is missing
    assert "Join:" in description
    assert "HS1" in description                             # the hard stop
    assert run.seal in description
    assert f"/#/changes/{run.change_key}" in description
    assert ADVISORY_HEADING in note or "Advisory, written by AI" in note


def test_the_description_is_a_full_report(settings, tmp_path):
    run = make_run("held")
    description = dry_run_document(settings, tmp_path, run)["payload"]["description"]

    for heading in ("The change", "Why: the stated intent", "Risk signals", "Evidence behind each signal",
                    "Code changes and who wrote them", "AI agent sessions", "Human review and testing",
                    "Evidence chain", "Rollout and backout", "Integrity"):
        assert heading in description
    for signal in run.signals:
        for item in signal.evidence:
            assert html.escape(item.text) in description
    for f in run.record.diff.files:
        assert f.path in description
    if run.record.intent.url:
        assert f'href="{run.record.intent.url}"' in description


def test_only_http_links_are_rendered_as_links(settings, tmp_path):
    rec = record("held")
    rec.source_url = "javascript:alert(1)"
    run = make_run("held", record=rec)
    description = dry_run_document(settings, tmp_path, run)["payload"]["description"]
    assert 'href="javascript:' not in description


def test_an_empty_custom_field_name_is_not_sent(settings, tmp_path):
    fields = settings.config.freshservice.custom_fields
    fields.seal = ""
    fields.source_url = ""
    payload = dry_run_document(settings, tmp_path, make_run("held"))["payload"]
    assert "" not in payload["custom_fields"]
    assert set(payload["custom_fields"]) == {fields.decision, fields.score}

    fields.decision = fields.score = ""
    payload = dry_run_document(settings, tmp_path, make_run("held", run_id="run-held-0009"))["payload"]
    assert "custom_fields" not in payload


def test_the_deploy_window_wins_over_the_clock(settings, tmp_path):
    rec = record("held")
    rec.deploy.present = True
    rec.deploy.window = "2026-10-01T22:00:00Z"
    run = make_run("held", record=rec)

    payload = dry_run_document(settings, tmp_path, run)["payload"]
    assert payload["planned_start_date"] == "2026-10-01T22:00:00Z"
    assert payload["planned_end_date"] == "2026-10-01T23:00:00Z"


def test_a_free_text_window_falls_back_to_the_clock(settings, tmp_path):
    rec = record("held")
    rec.deploy.present = True
    rec.deploy.window = "next Tuesday evening"
    run = make_run("held", record=rec)

    payload = dry_run_document(settings, tmp_path, run)["payload"]
    assert payload["planned_start_date"] == "2026-09-22T09:00:00Z"


def test_a_no_code_change_files_a_default_reason(settings, tmp_path):
    run = make_run("model-bump")
    payload = dry_run_document(settings, tmp_path, run)["payload"]
    assert payload["planning_fields"]["reason_for_change"]["description"] == (
        "No stated intent. Filed by Docket."
    )
    assert payload["risk"] == settings.config.freshservice.risk_codes["very_high"]


def test_the_dry_run_never_touches_the_client(settings, tmp_path):
    client = FakeClient()
    w = writer(settings, tmp_path, client=client)
    w.write(make_run("held"))
    assert client.calls == []


# ---- the live path -------------------------------------------------------


def test_upsert_creates_then_updates(live_settings, tmp_path, store):
    client = FakeClient([
        FakeResponse(201, {"change": {"id": 512}}),     # POST /changes
        FakeResponse(201, {"note": {"id": 1}}),         # POST /changes/512/notes
    ])
    run = make_run("held")
    w = writer(live_settings, tmp_path, store=store, client=client)
    outputs = w.write(run)

    assert outputs.errors == []
    assert outputs.freshservice_change_id == 512
    assert outputs.freshservice_url == "https://acme.freshservice.com/a/changes/512"
    assert store.ticket_for(run.change_key)["freshservice_id"] == 512
    assert [c["method"] for c in client.calls] == ["POST", "POST"]
    assert client.calls[0]["url"] == "https://acme.freshservice.com/api/v2/changes"
    assert client.calls[0]["auth"] == (API_KEY, "X")     # basic auth, password X
    assert client.calls[1]["url"].endswith("/changes/512/notes")
    assert "body" in client.calls[1]["json"]

    second = FakeClient([FakeResponse(200, {"change": {"id": 512}}), FakeResponse(201, {})])
    rerun = make_run("held", run_id="run-held-0002")
    writer(live_settings, tmp_path, store=store, client=second).write(rerun)
    assert second.calls[0]["method"] == "PUT"
    assert second.calls[0]["url"].endswith("/changes/512")


def test_a_4xx_is_captured_and_never_raises(live_settings, tmp_path, store):
    body = '{"description":"Validation failed","errors":[{"field":"planned_start_date"}]}'
    client = FakeClient([FakeResponse(400, {}, text=body)])
    run = make_run("held")

    outputs = writer(live_settings, tmp_path, store=store, client=client).write(run)

    assert outputs.freshservice_change_id is None
    assert len(outputs.errors) == 1
    assert "400" in outputs.errors[0] and "planned_start_date" in outputs.errors[0]
    assert API_KEY not in outputs.errors[0]              # RFC 17.5: never log a secret
    assert len(client.calls) == 1                        # no note is posted after a failure


def test_a_transport_failure_is_captured_and_never_raises(live_settings, tmp_path, store):
    run = make_run("held")
    outputs = writer(live_settings, tmp_path, store=store, client=ExplodingClient()).write(run)
    assert outputs.freshservice_change_id is None
    assert "ConnectionError" in outputs.errors[0]


# ---- approval sync -------------------------------------------------------


class RecordingStatusWriter:
    def __init__(self):
        self.calls = []

    def write(self, run, head_sha=None, approval=None, state=None):
        self.calls.append(approval)
        run.outputs.github_status_state = "success" if approval == "approved" else "failure"
        return run.outputs.github_status_state


@pytest.mark.parametrize("code,expected", [(1, "approved"), (2, "rejected"), (9, "unknown")])
def test_sync_approval_maps_through_config(live_settings, tmp_path, store, code, expected):
    run = make_run("held")
    store.save_run(run)
    store.save_ticket(run.change_key, 512, "https://acme.freshservice.com/a/changes/512")
    status_writer = RecordingStatusWriter()
    client = FakeClient([FakeResponse(200, {"change": {"id": 512, "approval_status": code}})])

    outputs = writer(
        live_settings, tmp_path, store=store, client=client, status_writer=status_writer
    ).sync_approval(run.change_key)

    assert outputs.freshservice_approval == expected
    assert store.latest_run(run.change_key).outputs.freshservice_approval == expected
    assert status_writer.calls == ([expected] if expected != "unknown" else [])
    assert client.calls[0]["method"] == "GET"


def test_sync_approval_without_a_run_returns_none(live_settings, tmp_path, store):
    assert writer(live_settings, tmp_path, store=store, client=FakeClient()).sync_approval(
        "pr-nothing-1"
    ) is None


def test_the_store_may_be_passed_to_write_instead_of_the_constructor(live_settings, tmp_path, store):
    """pipeline.py builds the writer without a store; either way the upsert works."""
    client = FakeClient([FakeResponse(201, {"change": {"id": 77}}), FakeResponse(201, {})])
    run = make_run("held")
    writer(live_settings, tmp_path, client=client).write(run, store=store)
    assert store.ticket_for(run.change_key)["freshservice_id"] == 77

    second = FakeClient([FakeResponse(200, {"change": {"id": 77}}), FakeResponse(201, {})])
    writer(live_settings, tmp_path, client=second).write(
        make_run("held", run_id="run-held-0003"), store=store
    )
    assert second.calls[0]["method"] == "PUT"


def test_without_a_store_a_known_change_id_still_updates(live_settings, tmp_path):
    client = FakeClient([FakeResponse(200, {"change": {"id": 88}}), FakeResponse(201, {})])
    run = make_run("held")
    run.outputs.freshservice_change_id = 88          # carried over from the previous run
    writer(live_settings, tmp_path, client=client).write(run)
    assert client.calls[0]["method"] == "PUT" and client.calls[0]["url"].endswith("/changes/88")


def test_sync_approval_takes_the_store_as_an_argument(live_settings, tmp_path, store):
    run = make_run("held")
    store.save_run(run)
    store.save_ticket(run.change_key, 512, "https://acme.freshservice.com/a/changes/512")
    client = FakeClient([FakeResponse(200, {"change": {"approval_status": 1}})])

    outputs = writer(live_settings, tmp_path, client=client).sync_approval(run.change_key, store)
    assert outputs.freshservice_approval == "approved"
