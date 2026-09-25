"""The HTTP surface (RFC sections 7.1.1, 7.2, 11, 17).

No network: TestClient talks to the app in-process, and every external client is
left as None so the pipeline degrades instead of dialling out.
"""

from __future__ import annotations

import gzip
import json

import pytest
from fastapi.testclient import TestClient

from docket import pipeline
from docket.record.store import Store
from docket.server import create_app
from docket.settings import load_settings

TOKEN = "test-ingest-token"

LOGS_PAYLOAD = {
    "resourceLogs": [
        {
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "claude-code"}},
                    {"key": "user.email", "value": {"stringValue": "dev@acme.example"}},
                ]
            },
            "scopeLogs": [
                {
                    "logRecords": [
                        {
                            "timeUnixNano": "1758700000000000000",
                            "body": {"stringValue": "claude_code.user_prompt"},
                            "attributes": [
                                {"key": "session.id", "value": {"stringValue": "s-http-1"}},
                                {"key": "prompt.id", "value": {"stringValue": "p1"}},
                                {"key": "prompt", "value": {"stringValue": "fix the sync loop"}},
                                {"key": "prompt_length", "value": {"intValue": "17"}},
                            ],
                        }
                    ]
                }
            ],
        }
    ]
}

HOOK_PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "session_id": "s-http-1",
    "prompt_id": "p1",
    "cwd": "/home/dev/repo",
    "permission_mode": "acceptEdits",
    "tool_name": "Edit",
    "tool_use_id": "tu-1",
    "tool_input": {
        "file_path": "/home/dev/repo/provisioning/sync.py",
        "old_string": "retry = 1",
        "new_string": "retry = 3\nbackoff = 2",
    },
}


@pytest.fixture()
def app_bits(tmp_path, repo_root):
    settings = load_settings(
        repo_root / "config.json",
        env={"DOCKET_DB": str(tmp_path / "t.sqlite"), "DOCKET_INGEST_TOKEN": TOKEN},
    )
    store = Store(tmp_path / "t.sqlite")
    deps = pipeline.Deps(settings=settings, store=store)
    app = create_app(settings=settings, store=store, deps=deps)
    with TestClient(app) as client:
        yield client, store, settings
    store.close()


@pytest.fixture()
def client(app_bits):
    return app_bits[0]


def auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


# ---- ingest --------------------------------------------------------------

def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["ingest_authenticated"] is True


def test_otlp_requires_the_ingest_token(client):
    assert client.post("/v1/logs", json=LOGS_PAYLOAD).status_code == 401
    assert client.post("/v1/logs", json=LOGS_PAYLOAD, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/hooks/claude-code", json=HOOK_PAYLOAD).status_code == 401


def test_otlp_logs_accepts_json(client):
    r = client.post("/v1/logs", json=LOGS_PAYLOAD, headers=auth())
    assert r.status_code == 200
    assert r.json() == {"partialSuccess": {}}


def test_otlp_logs_accepts_gzip(client):
    body = gzip.compress(json.dumps(LOGS_PAYLOAD).encode("utf-8"))
    r = client.post(
        "/v1/logs",
        content=body,
        headers={**auth(), "Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert r.status_code == 200
    assert r.json() == {"partialSuccess": {}}


def test_otlp_rejects_protobuf_with_a_useful_message(client):
    r = client.post(
        "/v1/logs",
        content=b"\x00\x01protobuf",
        headers={**auth(), "Content-Type": "application/x-protobuf"},
    )
    assert r.status_code == 415
    assert "http/json" in r.text


def test_otlp_never_5xxs_on_a_malformed_body(client):
    r = client.post("/v1/logs", content=b"{not json", headers={**auth(), "Content-Type": "application/json"})
    assert r.status_code == 200
    r = client.post("/v1/logs", json={"resourceLogs": "nonsense"}, headers=auth())
    assert r.status_code == 200


def test_metrics_and_traces_routes_exist(client):
    for route in ("/v1/metrics", "/v1/traces"):
        r = client.post(route, json={}, headers=auth())
        assert r.status_code == 200
        assert r.json() == {"partialSuccess": {}}


def test_hook_ingest_answers_empty_200(client):
    r = client.post("/hooks/claude-code", json=HOOK_PAYLOAD, headers=auth())
    assert r.status_code == 200
    # A JSON body would be read by Claude Code as a hook decision (RFC 7.2).
    assert r.content == b""


def test_hook_ingest_survives_rubbish(client):
    assert client.post("/hooks/claude-code", content=b"{oops", headers=auth()).status_code == 200
    assert client.post("/hooks/claude-code", json={}, headers=auth()).status_code == 200


def test_ingested_session_shows_up(client):
    client.post("/v1/logs", json=LOGS_PAYLOAD, headers=auth())
    client.post("/hooks/claude-code", json=HOOK_PAYLOAD, headers=auth())
    sessions = client.get("/sessions").json()
    ids = {s["session_id"] for s in sessions}
    assert "s-http-1" in ids
    row = next(s for s in sessions if s["session_id"] == "s-http-1")
    assert row["events"] >= 1
    assert row["edits"] >= 1


def test_posted_coverage_is_stored(client):
    payload = {"files": {"provisioning/sync.py": {"executed_lines": [1, 2], "missing_lines": [3]}}}
    r = client.post("/coverage/abc123", json=payload, headers=auth())
    assert r.json() == {"stored": True}


# ---- reads ---------------------------------------------------------------

def test_unknown_change_is_404(client):
    assert client.get("/changes/nope").status_code == 404
    assert client.get("/changes/nope/verify").status_code == 404


def test_empty_queue_is_a_list(client):
    assert client.get("/changes").json() == []


def test_gate_config_route(client):
    gate = client.get("/config/gate").json()
    assert gate["threshold"] == 45
    assert set(gate["weights"]) == {"unattributed", "review_depth", "untested", "blast_radius"}


def test_threshold_must_be_sane(client):
    assert client.put("/config/threshold", json={}).status_code == 400
    assert client.put("/config/threshold", json={"threshold": 140}).status_code == 400


def test_threshold_override_survives_a_read(client):
    client.put("/config/threshold", json={"threshold": 30})
    assert client.get("/config/gate").json()["threshold"] == 30


def test_index_serves_the_frontend(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_no_cors_headers(client):
    r = client.get("/healthz")
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_manifest_ledger_is_empty_before_a_check(client):
    assert client.get("/manifest/ledger").json() == []


def test_manifest_check_files_a_baseline_then_nothing(client, app_bits):
    _client, store, _settings = app_bits
    first = client.post("/manifest/check").json()
    assert first == [], "the first sighting is a baseline, not a change"
    assert len(client.get("/manifest/ledger").json()) == 1
    assert client.post("/manifest/check").json() == [], "an unchanged manifest files nothing"


def test_run_route_reports_a_run_id_and_change_key(client):
    r = client.post("/run/4471?repo=acme/provisioning-service")
    body = r.json()
    assert body["change_key"] == "pr-acme-provisioning-service-4471"
    status = client.get(f"/status/{body['run_id']}").json()
    assert status["stage"] in {"collecting", "correlating", "deciding", "recording", "done", "error"}


# ---- client wiring -------------------------------------------------------
#
# A constructor whose signature drifts must not silently disable a whole plane.
# It happened once: CoverageCollector's __init__ gained a `token` keyword, the
# TypeError was swallowed by a shared try block, and real coverage collection
# was quietly off whenever GITHUB_TOKEN was set. Tests missed it because that
# branch is skipped without a token. These two tests close that hole.

def test_every_client_is_constructed_when_a_token_is_present(tmp_path, repo_root):
    from docket.server import _build_deps

    settings = load_settings(
        repo_root / "config.json",
        env={"DOCKET_DB": str(tmp_path / "w.sqlite"), "GITHUB_TOKEN": "ghp_" + "x" * 36},
    )
    store = Store(tmp_path / "w.sqlite")
    deps = _build_deps(settings, store)
    store.close()

    # PyGithub builds its client lazily, so this touches no network.
    assert deps.github is not None, "Repo Collector was not wired"
    assert deps.coverage is not None, "CI Collector was not wired"
    assert deps.status is not None, "GitHub status writer was not wired"
    assert deps.freshservice is not None, "Freshservice writer was not wired"
    assert deps.brief is not None, "Board Brief was not wired"


def test_freshservice_writer_can_find_its_ticket(tmp_path, repo_root):
    """Without the store the writer would POST a new change on every re-run (RFC 10.3)."""
    from docket.server import _build_deps

    settings = load_settings(repo_root / "config.json", env={"DOCKET_DB": str(tmp_path / "t2.sqlite")})
    store = Store(tmp_path / "t2.sqlite")
    deps = _build_deps(settings, store)
    assert getattr(deps.freshservice, "store", None) is store
    store.close()


def test_missing_token_degrades_without_disabling_the_rest(tmp_path, repo_root):
    from docket.server import _build_deps

    settings = load_settings(repo_root / "config.json", env={"DOCKET_DB": str(tmp_path / "n.sqlite")})
    store = Store(tmp_path / "n.sqlite")
    deps = _build_deps(settings, store)
    store.close()

    assert deps.github is None and deps.coverage is None and deps.status is None
    assert deps.freshservice is not None, "no GitHub token must not disable Freshservice"


def test_gate_config_carries_the_sensitive_lists_for_the_ui(client):
    """RFC 0 rule 4: the UI must not hard-code sensitive tools or shell patterns."""
    gate = client.get("/config/gate").json()
    assert gate["sensitive_paths"], "the gate needs these for HS3"
    sensitive = gate["sensitive"]
    assert "**/entitlement/**" in sensitive["paths"]
    assert "entitlement_svc.*" in sensitive["tools"]
    assert "terraform apply" in sensitive["bash_patterns"]
