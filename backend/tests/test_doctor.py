"""`make doctor` (docket/doctor.py). Offline: every client is faked.

The real error paths were also exercised by hand against api.github.com, the
Anthropic API and a non-existent Freshservice domain with deliberately invalid keys.
These tests pin that behaviour down without the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from docket import doctor
from docket.settings import load_settings

from conftest import offline_config

GH_TOKEN = "ghp_secretvalue_should_never_print_0000"
FS_KEY = "fs_secret_key_never_print"
AN_KEY = "sk-ant-secret-never-print-000"


def make_settings(tmp_path, repo_root, env=None, **cfg_changes):
    raw = offline_config(json.loads((repo_root / "config.json").read_text(encoding="utf-8")))
    for dotted, value in cfg_changes.items():
        block, key = dotted.split("__")
        raw[block][key] = value
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    base = {"DOCKET_DB": str(tmp_path / "d.sqlite")}
    base.update(env or {})
    return load_settings(path, env=base)


def statuses(report, section):
    return [(c.status, c.title) for c in report.checks if c.section == section]


def no_network(*_a, **_k):
    raise AssertionError("the doctor contacted something it should not have")


def not_running(_url):
    raise ConnectionError("refused")


def text(report) -> str:
    return doctor.render(report)


# ---------------------------------------------------------------------------
# the baseline: nothing configured
# ---------------------------------------------------------------------------

def test_nothing_configured_is_warnings_not_failures(tmp_path, repo_root):
    s = make_settings(tmp_path, repo_root)
    r = doctor.run(s, github_factory=no_network, freshservice_factory=no_network,
                   anthropic_factory=no_network, http_get=not_running)
    assert not r.failed, text(r)
    assert statuses(r, "GitHub")[0][0] == doctor.SKIP
    assert statuses(r, "Freshservice")[0][0] == doctor.SKIP
    assert statuses(r, "Anthropic (board brief)")[0][0] == doctor.SKIP
    assert any("DOCKET_INGEST_TOKEN is empty" in c.title for c in r.checks)


def test_offline_contacts_nothing(tmp_path, repo_root):
    s = make_settings(tmp_path, repo_root, env={"GITHUB_TOKEN": GH_TOKEN, "ANTHROPIC_API_KEY": AN_KEY})
    r = doctor.run(s, offline=True, github_factory=no_network, freshservice_factory=no_network,
                   anthropic_factory=no_network, http_get=no_network)
    assert not r.failed


def test_the_doctor_never_creates_the_database(tmp_path, repo_root):
    s = make_settings(tmp_path, repo_root)
    doctor.run(s, offline=True)
    assert not (tmp_path / "d.sqlite").exists()


def test_change_me_token_is_flagged(tmp_path, repo_root):
    s = make_settings(tmp_path, repo_root, env={"DOCKET_INGEST_TOKEN": "change-me"})
    r = doctor.run(s, offline=True)
    assert any("change-me" in c.title and c.status == doctor.WARN for c in r.checks)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

class _Named:
    def __init__(self, name):
        self.name = name


class FakeRun:
    def __init__(self, artifacts):
        self._a = artifacts

    def get_artifacts(self):
        return [_Named(n) for n in self._a]


class FakeRepo:
    default_branch = "main"

    def __init__(self, fail_on=None, artifacts=("coverage-json",)):
        self.fail_on = fail_on or {}
        self.artifacts = artifacts

    def _maybe(self, name, value):
        from github import GithubException

        if name in self.fail_on:
            raise GithubException(self.fail_on[name], {"message": "Resource not accessible"}, None)
        return value

    def get_commits(self):
        return self._maybe("commits", ["c"])

    def get_pulls(self, state="all"):
        return self._maybe("pulls", ["p"])

    def get_issues(self, state="all"):
        return self._maybe("issues", ["i"])

    def get_workflow_runs(self):
        return self._maybe("actions", [FakeRun(self.artifacts)])

    def get_branch(self, name):
        class B:
            class commit:
                sha = "abc"
        return B()

    def get_commit(self, sha):
        repo = self

        class C:
            def get_combined_status(self):
                return repo._maybe("statuses", object())
        return C()


class FakeGithub:
    def __init__(self, user_exc=None, repo=None, repo_exc=None, scopes=None):
        self.user_exc, self.repo, self.repo_exc, self.oauth_scopes = user_exc, repo or FakeRepo(), repo_exc, scopes

    def get_user(self):
        if self.user_exc:
            raise self.user_exc

        class U:
            login = "octo"
        return U()

    def get_repo(self, name):
        if self.repo_exc:
            raise self.repo_exc
        return self.repo

    def get_rate_limit(self):
        class Core:
            remaining, limit = 4999, 5000

        class L:
            core = Core()
        return L()


def gh_settings(tmp_path, repo_root, **kw):
    return make_settings(tmp_path, repo_root, env={"GITHUB_TOKEN": GH_TOKEN},
                         github__default_repo=kw.get("repo", "acme/svc"))


def run_gh(settings, gh):
    r = doctor.Report(secrets=[GH_TOKEN])
    doctor.check_github(settings, r, lambda _t: gh)
    return r


def test_github_happy_path(tmp_path, repo_root):
    r = run_gh(gh_settings(tmp_path, repo_root), FakeGithub())
    assert not r.failed, text(r)
    titles = " ".join(c.title for c in r.checks)
    for perm in ("Contents: read", "Pull requests: read", "Issues: read", "Actions: read"):
        assert perm in titles
    assert "coverage-json" in titles


def test_github_bad_token(tmp_path, repo_root):
    from github import BadCredentialsException

    r = run_gh(gh_settings(tmp_path, repo_root), FakeGithub(user_exc=BadCredentialsException(401, {}, None)))
    assert r.failed and "rejected the token" in text(r)


def test_github_placeholder_repo(tmp_path, repo_root):
    r = run_gh(gh_settings(tmp_path, repo_root, repo="owner/name"), FakeGithub())
    assert r.failed and "default_repo is still" in text(r)


def test_github_repo_not_visible(tmp_path, repo_root):
    from github import UnknownObjectException

    r = run_gh(gh_settings(tmp_path, repo_root), FakeGithub(repo_exc=UnknownObjectException(404, {}, None)))
    assert r.failed and "not found, or this token cannot see it" in text(r)


def test_github_missing_permission_is_named(tmp_path, repo_root):
    r = run_gh(gh_settings(tmp_path, repo_root), FakeGithub(repo=FakeRepo(fail_on={"pulls": 403})))
    fails = [c for c in r.checks if c.status == doctor.FAIL]
    assert len(fails) == 1 and "Pull requests: read" in fails[0].title


def test_github_classic_token_without_repo_scope(tmp_path, repo_root):
    r = run_gh(gh_settings(tmp_path, repo_root), FakeGithub(scopes=["read:user", "gist"]))
    assert r.failed and "lacks the `repo` scope" in text(r)


def test_github_no_coverage_artifact_is_a_warning(tmp_path, repo_root):
    r = run_gh(gh_settings(tmp_path, repo_root), FakeGithub(repo=FakeRepo(artifacts=("other",))))
    assert not r.failed
    assert any(c.status == doctor.WARN and "coverage-json" in c.title for c in r.checks)


# ---------------------------------------------------------------------------
# Freshservice (httpx.MockTransport: real client, fake server)
# ---------------------------------------------------------------------------

FIELDS = {"change_fields": [
    {"name": "subject", "label": "Subject"},
    {"name": "docket_decision", "label": "Docket decision", "choices": [["APPROVE"], ["HOLD"]]},
    {"name": "docket_score", "label": "Docket score"},
    {"name": "docket_seal", "label": "Docket seal"},
    {"name": "docket_source_url", "label": "Docket source URL"},
]}


def fs_settings(tmp_path, repo_root, key=FS_KEY, **cfg):
    changes = {"freshservice__enabled": True, "freshservice__domain": "acme.freshservice.com",
               "freshservice__requester_id": 42}
    changes.update(cfg)
    env = {"FRESHSERVICE_API_KEY": key} if key else {}
    return make_settings(tmp_path, repo_root, env=env, **changes)


def fs_factory(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "the doctor must only read"
        for prefix, (status, body) in routes.items():
            if request.url.path.startswith("/api/v2" + prefix.split("?")[0]):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return lambda s: httpx.Client(base_url="https://acme.freshservice.com/api/v2",
                                  auth=(s.freshservice_api_key, "X"), transport=httpx.MockTransport(handler))


def run_fs(settings, routes):
    r = doctor.Report(secrets=[FS_KEY])
    doctor.check_freshservice(settings, r, fs_factory(routes))
    return r


OK_ROUTES = {"/changes": (200, {"changes": []}), "/requesters/42": (200, {}), "/change_form_fields": (200, FIELDS)}


def test_freshservice_happy_path(tmp_path, repo_root):
    r = run_fs(fs_settings(tmp_path, repo_root), OK_ROUTES)
    assert not r.failed, text(r)
    assert "all four Docket custom fields exist" in text(r)


def test_freshservice_bad_key(tmp_path, repo_root):
    r = run_fs(fs_settings(tmp_path, repo_root), {"/changes": (401, {})})
    assert r.failed and "rejected the API key" in text(r)


def test_freshservice_unknown_domain(tmp_path, repo_root):
    r = run_fs(fs_settings(tmp_path, repo_root), {"/changes": (404, {})})
    assert r.failed and "no Freshservice account answers" in text(r)


def test_freshservice_enabled_without_key(tmp_path, repo_root):
    r = run_fs(fs_settings(tmp_path, repo_root, key=None), OK_ROUTES)
    assert r.failed and "FRESHSERVICE_API_KEY is empty" in text(r)


def test_freshservice_placeholder_domain(tmp_path, repo_root):
    r = run_fs(fs_settings(tmp_path, repo_root, freshservice__domain="yourcompany.freshservice.com"), OK_ROUTES)
    assert r.failed and "still `yourcompany.freshservice.com`" in text(r)


def test_freshservice_unknown_requester(tmp_path, repo_root):
    routes = {k: v for k, v in OK_ROUTES.items() if not k.startswith("/requesters")}
    r = run_fs(fs_settings(tmp_path, repo_root), routes)
    assert r.failed and "neither a requester nor an agent" in text(r)


def test_freshservice_missing_custom_field_suggests_the_real_name(tmp_path, repo_root):
    prefixed = {"change_fields": [dict(f, name="cf_" + f["name"]) if "docket" in f["name"] else f
                                  for f in FIELDS["change_fields"]]}
    r = run_fs(fs_settings(tmp_path, repo_root), dict(OK_ROUTES, **{"/change_form_fields": (200, prefixed)}))
    assert r.failed
    assert "custom field `docket_decision`" in text(r)
    assert "cf_docket_decision" in text(r), "the fix line should suggest the account's real API name"


def test_freshservice_unreadable_field_list_is_a_warning(tmp_path, repo_root):
    routes = dict(OK_ROUTES, **{"/change_form_fields": (403, {})})
    r = run_fs(fs_settings(tmp_path, repo_root), routes)
    assert not r.failed
    assert any(c.status == doctor.WARN and "could not list the Change form fields" in c.title for c in r.checks)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def _anthropic_error(cls, status):
    import httpx2

    req = httpx2.Request("GET", "https://api.anthropic.com/v1/models/claude-sonnet-5")
    return cls("error", response=httpx2.Response(status, request=req), body=None)


class FakeAnthropic:
    def __init__(self, exc=None):
        self.exc = exc

        outer = self

        class Models:
            def retrieve(self, model_id):
                if outer.exc:
                    raise outer.exc

                class Info:
                    id = model_id
                return Info()

        self.models = Models()


def run_an(tmp_path, repo_root, exc=None):
    s = make_settings(tmp_path, repo_root, env={"ANTHROPIC_API_KEY": AN_KEY})
    r = doctor.Report(secrets=[AN_KEY])
    doctor.check_anthropic(s, r, lambda _k: FakeAnthropic(exc))
    return r


def test_anthropic_happy_path(tmp_path, repo_root):
    r = run_an(tmp_path, repo_root)
    assert not r.failed and "is available" in text(r)


@pytest.mark.parametrize("name,status,expect", [
    ("AuthenticationError", 401, "rejected the API key"),
    ("PermissionDeniedError", 403, "not allowed"),
    ("NotFoundError", 404, "is not available to this key"),
])
def test_anthropic_failures(tmp_path, repo_root, name, status, expect):
    import anthropic

    r = run_an(tmp_path, repo_root, _anthropic_error(getattr(anthropic, name), status))
    assert r.failed and expect in text(r)


def test_anthropic_rate_limit_is_only_a_warning(tmp_path, repo_root):
    import anthropic

    r = run_an(tmp_path, repo_root, _anthropic_error(anthropic.RateLimitError, 429))
    assert not r.failed


# ---------------------------------------------------------------------------
# secrets, the running server, exit codes
# ---------------------------------------------------------------------------

def test_no_secret_is_ever_printed(tmp_path, repo_root):
    """Even when an exception's message contains the secret, it is scrubbed."""
    s = make_settings(tmp_path, repo_root, env={"GITHUB_TOKEN": GH_TOKEN, "ANTHROPIC_API_KEY": AN_KEY,
                                                  "FRESHSERVICE_API_KEY": FS_KEY, "DOCKET_INGEST_TOKEN": "ingest-secret-1"},
                      github__default_repo="acme/svc", freshservice__enabled=True,
                      freshservice__domain="acme.freshservice.com", freshservice__requester_id=1)

    def leaky(_):
        raise RuntimeError(f"boom with {GH_TOKEN} and {AN_KEY} and {FS_KEY}")

    r = doctor.run(s, github_factory=leaky, freshservice_factory=leaky, anthropic_factory=leaky, http_get=not_running)
    out = doctor.render(r)
    for secret in (GH_TOKEN, AN_KEY, FS_KEY, "ingest-secret-1"):
        assert secret not in out
    assert "[redacted]" in out


def test_one_crashing_check_does_not_hide_the_others(tmp_path, repo_root):
    s = make_settings(tmp_path, repo_root, env={"GITHUB_TOKEN": GH_TOKEN, "ANTHROPIC_API_KEY": AN_KEY},
                      github__default_repo="acme/svc")
    r = doctor.run(s, github_factory=lambda _t: (_ for _ in ()).throw(ValueError("kaput")),
                   anthropic_factory=lambda _k: FakeAnthropic(), http_get=not_running)
    assert any("is available" in c.title for c in r.checks), "Anthropic still checked after GitHub crashed"


def test_running_server_without_ingest_token_is_flagged(tmp_path, repo_root):
    s = make_settings(tmp_path, repo_root)

    def fake_get(url):
        if url.endswith("/healthz"):
            return 200, {"ok": True, "ingest_authenticated": False}
        return 200, []

    r = doctor.Report()
    doctor.check_agent_setup(s, r, http_get=fake_get, which=lambda _t: "/usr/bin/x")
    assert any("running server has no ingest token" in c.title for c in r.checks)


def test_missing_hook_tools_are_named(tmp_path, repo_root):
    s = make_settings(tmp_path, repo_root)
    r = doctor.Report()
    doctor.check_agent_setup(s, r, http_get=not_running, which=lambda t: None if t == "jq" else "/usr/bin/x")
    assert any(c.status == doctor.WARN and c.title.startswith("jq not found") for c in r.checks)


def test_exit_code_is_1_on_any_failure(monkeypatch, tmp_path, repo_root):
    monkeypatch.setattr(doctor, "load_settings",
                        lambda: make_settings(tmp_path, repo_root, env={"GITHUB_TOKEN": GH_TOKEN},
                                              github__default_repo="owner/name"))
    monkeypatch.setattr(doctor, "_github", lambda _t: FakeGithub())
    monkeypatch.setattr(doctor, "_http_get", not_running)
    real_run = doctor.run
    monkeypatch.setattr(doctor, "run", lambda s, offline=False: real_run(
        s, offline=offline, github_factory=lambda _t: FakeGithub(), freshservice_factory=no_network,
        anthropic_factory=no_network, http_get=not_running))
    assert doctor.main([]) == 1
    assert doctor.main(["--offline"]) == 0


def test_a_foreign_service_on_the_port_is_named_as_such(tmp_path, repo_root):
    """Seen live: another service owned port 8000, so /healthz returned 404."""
    s = make_settings(tmp_path, repo_root)
    r = doctor.Report()
    doctor.check_agent_setup(s, r, http_get=lambda _u: (404, None), which=lambda _t: "/usr/bin/x")
    fail = next(c for c in r.checks if c.status == doctor.FAIL)
    assert "is not Docket" in fail.title
    assert "make serve PORT=" in fail.fix


# ---------------------------------------------------------------------------
# telemetry health — the check that answers "why is my change unmatched?"
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self, sessions=(), parts=None):
        self._sessions, self._parts = list(sessions), parts

    def list_sessions(self):
        return self._sessions

    def load_session_parts(self, _sid):
        if self._parts is None:
            raise RuntimeError("no parts")
        return self._parts


def telemetry(tmp_path, repo_root, store):
    s = make_settings(tmp_path, repo_root)
    r = doctor.Report()
    doctor.check_telemetry(s, r, store=store)
    return r


def test_no_session_ever_received_is_a_warning_with_the_fix(tmp_path, repo_root):
    r = telemetry(tmp_path, repo_root, FakeStore())
    c = next(c for c in r.checks if c.status == doctor.WARN)
    assert "no agent session has ever reached" in c.title
    assert "BEFORE starting the agent" in c.fix


def test_sessions_without_edits_name_the_hook_and_the_consequence(tmp_path, repo_root):
    store = FakeStore(sessions=[{"session_id": "a", "edits": 0, "ended_at": "2026-09-24T10:00:00Z"}])
    r = telemetry(tmp_path, repo_root, store)
    text = doctor.render(r)
    assert "carry no edit text" in text
    assert "literal address" in text, "must say env vars are not expanded in the hook url"
    assert "`unknown` instead of `ai`" in text, "must say what it costs"


def test_a_complete_session_reports_ok(tmp_path, repo_root, monkeypatch):
    from docket.models.fragments import DetailLevel, SessionFragment
    from datetime import datetime, timezone

    frag = SessionFragment(
        session_id="a", started_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 24, tzinfo=timezone.utc), repo_url="https://github.com/acme/app",
        detail=DetailLevel(prompts=True, tool_details=True, edits=True),
    )
    monkeypatch.setattr("docket.collectors.adapters.claude_code.build_session", lambda *a, **k: frag)
    store = FakeStore(sessions=[{"session_id": "a", "edits": 3, "ended_at": "2026-09-24T10:00:00Z"}], parts=([], [], {}))
    r = telemetry(tmp_path, repo_root, store)
    assert not r.failed
    assert "complete: prompts, tool details and edits" in doctor.render(r)


def test_redacted_prompts_are_named_with_the_missing_variable(tmp_path, repo_root, monkeypatch):
    from docket.models.fragments import DetailLevel, SessionFragment
    from datetime import datetime, timezone

    frag = SessionFragment(
        session_id="a", started_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
        ended_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
        detail=DetailLevel(prompts=False, tool_details=False, edits=True),
    )
    monkeypatch.setattr("docket.collectors.adapters.claude_code.build_session", lambda *a, **k: frag)
    store = FakeStore(sessions=[{"session_id": "a", "edits": 1, "ended_at": "2026-09-24T10:00:00Z"}], parts=([], [], {}))
    text = doctor.render(telemetry(tmp_path, repo_root, store))
    assert "OTEL_LOG_USER_PROMPTS=1" in text
    assert "OTEL_LOG_TOOL_DETAILS=1" in text
