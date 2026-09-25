"""The GitHub commit status writer (RFC 10.4). The PyGithub client is injected."""

from __future__ import annotations

import pytest

from docket.record.github_status import MAX_DESCRIPTION, GithubStatusWriter

from fixtures.runs import make_run

HEAD_SHA = "4471a91f3c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f"


class FakeCommit:
    def __init__(self, sha, statuses):
        self.sha = sha
        self._statuses = statuses

    def create_status(self, state, target_url, description, context):
        self._statuses.append({
            "sha": self.sha, "state": state, "target_url": target_url,
            "description": description, "context": context,
        })


class FakePull:
    def __init__(self, sha):
        self.head = type("Head", (), {"sha": sha})()


class FakeRepo:
    def __init__(self, name, statuses, pull_sha=HEAD_SHA):
        self.full_name = name
        self.statuses = statuses
        self.pulls_asked: list[int] = []
        self._pull_sha = pull_sha

    def get_pull(self, number):
        self.pulls_asked.append(number)
        return FakePull(self._pull_sha)

    def get_commit(self, sha):
        return FakeCommit(sha, self.statuses)


class FakeGithub:
    def __init__(self, pull_sha=HEAD_SHA):
        self.statuses: list[dict] = []
        self.repos_asked: list[str] = []
        self._pull_sha = pull_sha

    def get_repo(self, full_name):
        self.repos_asked.append(full_name)
        return FakeRepo(full_name, self.statuses, self._pull_sha)


class ExplodingGithub:
    def get_repo(self, full_name):
        raise RuntimeError("github is down")


def test_a_held_code_change_posts_a_pending_status(settings):
    github = FakeGithub()
    run = make_run("held")

    state = GithubStatusWriter(settings, github_client=github).write(run)

    assert state == settings.config.github.status_states["HOLD"] == "pending"
    assert github.repos_asked == ["acme/provisioning-service"]
    posted = github.statuses[0]
    assert posted["sha"] == HEAD_SHA
    assert posted["context"] == settings.config.github.status_context == "docket/gate"
    assert posted["description"] == "HOLD · risk 73.9 over limit 45 · CAB approval requested"
    assert len(posted["description"]) <= MAX_DESCRIPTION
    assert posted["target_url"] == f"http://localhost:8000/#/changes/{run.change_key}"
    assert run.outputs.github_status_state == "pending"
    assert run.outputs.errors == []


def test_an_approved_change_posts_success(settings):
    github = FakeGithub()
    run = make_run("cleared")
    state = GithubStatusWriter(settings, github_client=github).write(run)
    assert state == "success"
    assert github.statuses[0]["description"].startswith("APPROVE · risk 3.8 under limit 45")


def test_the_description_never_exceeds_140_characters(settings):
    github = FakeGithub()
    run = make_run("held")
    run.decision.composite = 123456.789
    GithubStatusWriter(settings, github_client=github).write(run)
    assert len(github.statuses[0]["description"]) <= MAX_DESCRIPTION


def test_the_freshservice_url_is_preferred_as_the_target(settings):
    github = FakeGithub()
    run = make_run("held")
    run.outputs.freshservice_url = "https://acme.freshservice.com/a/changes/512"
    GithubStatusWriter(settings, github_client=github).write(run)
    assert github.statuses[0]["target_url"] == "https://acme.freshservice.com/a/changes/512"


@pytest.mark.parametrize("approval,expected", [("approved", "success"), ("rejected", "failure")])
def test_an_approval_sync_overrides_the_state(settings, approval, expected):
    github = FakeGithub()
    run = make_run("held")
    state = GithubStatusWriter(settings, github_client=github).write(run, approval=approval)
    assert state == expected == github.statuses[0]["state"]


def test_a_no_code_change_is_skipped_because_it_has_no_commit(settings):
    github = FakeGithub()
    run = make_run("model-bump")
    assert GithubStatusWriter(settings, github_client=github).write(run) is None
    assert github.statuses == [] and github.repos_asked == []
    assert run.outputs.github_status_state is None
    assert run.outputs.errors == []                      # a skip is not an error


def test_an_explicit_head_sha_wins_over_everything_on_the_record(settings):
    github = FakeGithub()
    run = make_run("held")
    GithubStatusWriter(settings, github_client=github).write(run, head_sha="deadbeef")
    assert github.statuses[0]["sha"] == "deadbeef"
    assert github.repos_asked == ["acme/provisioning-service"]


def test_without_any_commit_on_the_record_the_pull_request_is_asked(settings):
    github = FakeGithub(pull_sha="cafebabe")
    run = make_run("unmatched")                          # no sessions
    run.record.review.approvers = []                     # and no approval to point at
    GithubStatusWriter(settings, github_client=github).write(run)
    assert github.statuses[0]["sha"] == "cafebabe"


def test_a_failure_is_captured_and_never_raises(settings):
    run = make_run("held")
    assert GithubStatusWriter(settings, github_client=ExplodingGithub()).write(run) is None
    assert run.outputs.errors and "github is down" in run.outputs.errors[0]
    assert run.outputs.github_status_state is None


def test_no_client_is_a_silent_skip_and_no_pull_url_is_recorded(settings):
    run = make_run("held")
    assert GithubStatusWriter(settings, github_client=None).write(run) is None
    assert run.outputs.errors == []                      # no token configured is not a failure

    run2 = make_run("held")
    run2.record.source_url = None
    assert GithubStatusWriter(settings, github_client=FakeGithub()).write(run2) is None
    assert "no pull request url" in run2.outputs.errors[0]
