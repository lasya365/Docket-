"""Repo Collector tests. The PyGithub client is faked: no test touches the network."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from docket.collectors.github import (
    GithubCollector,
    find_issue_number,
    parse_scope_paths,
    parse_trailers,
    to_utc,
)

NAIVE = datetime(2026, 3, 1, 12, 0, 0)          # PyGithub often hands back naive UTC
AWARE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# pure parsers
# --------------------------------------------------------------------------- #


def test_parse_trailers_reads_the_last_paragraph():
    message = (
        "Add retry backoff\n"
        "\n"
        "This is the body. Not-A-Trailer: because it is not in the last paragraph.\n"
        "\n"
        "DocketSession-Id: sess-abc\n"
        "Co-Authored-By: Someone <a@b.c>\n"
        "DocketSession-Id: sess-def\n"
    )
    trailers = parse_trailers(message)
    assert trailers["DocketSession-Id"] == ["sess-abc", "sess-def"]
    assert trailers["Co-Authored-By"] == ["Someone <a@b.c>"]
    assert "Not-A-Trailer" not in trailers


def test_parse_trailers_edge_cases():
    assert parse_trailers(None) == {}
    assert parse_trailers("") == {}
    assert parse_trailers("just a subject") == {}
    # A one-paragraph message whose only line is a trailer.
    assert parse_trailers("DocketSession-Id: s1") == {"DocketSession-Id": ["s1"]}
    # Windows line endings and trailing blank lines.
    assert parse_trailers("subject\r\n\r\nDocketSession-Id: s2\r\n\r\n") == {"DocketSession-Id": ["s2"]}
    # An underscore is not in [A-Za-z0-9-], so this is not a trailer.
    assert parse_trailers("subject\n\nnot_a_key: v") == {}
    # An empty value is not a trailer.
    assert parse_trailers("subject\n\nKey:   ") == {}


def test_find_issue_number_prefers_closing_keywords():
    assert find_issue_number("Mentions #99 and Fixes #114", "title") == 114
    assert find_issue_number("closes #7") == 7
    assert find_issue_number("Resolved #8") == 8
    assert find_issue_number("fixed #9") == 9
    # No closing keyword anywhere: the first bare reference in the body.
    assert find_issue_number("see #42 and #43") == 42
    # Body has nothing, the title carries it.
    assert find_issue_number("", "Fixes #55") == 55
    assert find_issue_number("body with no ref", "title #56") == 56
    # A closing keyword in the title beats a bare number in the body.
    assert find_issue_number("bare #1", "closes #2") == 2
    assert find_issue_number(None, None) is None
    assert find_issue_number("nothing here") is None


def test_parse_scope_paths():
    body = "Some text\nScope: provisioning/**, tests/**\nMore text"
    assert parse_scope_paths(body) == ["provisioning/**", "tests/**"]
    assert parse_scope_paths("scope:   a/**   ") == ["a/**"]
    assert parse_scope_paths("SCOPE: a/**,b/**") == ["a/**", "b/**"]
    assert parse_scope_paths("no scope line") == []
    assert parse_scope_paths(None) == []


def test_to_utc_makes_naive_datetimes_aware():
    assert to_utc(NAIVE) == AWARE
    assert to_utc(AWARE) == AWARE
    assert to_utc("2026-03-01T12:00:00Z") == AWARE
    assert to_utc(None).year == 1970
    assert to_utc("garbage").year == 1970


# --------------------------------------------------------------------------- #
# a fake PyGithub client
# --------------------------------------------------------------------------- #


def fake_commit(sha, message, email="dev@example.com", login="dev"):
    return NS(
        sha=sha,
        author=NS(login=login) if login else None,
        commit=NS(message=message, author=NS(email=email), committer=NS(date=NAIVE)),
    )


def fake_file(filename, status="modified", additions=2, deletions=1, patch="@@ -1,2 +1,3 @@\n a\n+b\n-c\n"):
    return NS(filename=filename, status=status, additions=additions,
              deletions=deletions, patch=patch)


class FakePR:
    def __init__(self, **over):
        self.number = over.get("number", 4471)
        self.title = over.get("title", "Add provisioning backoff")
        self.body = over.get("body", "Fixes #114\n\nSome description.")
        self.html_url = "https://github.com/acme/widgets/pull/4471"
        self.user = NS(login="dev")
        self.labels = [NS(name="docket:human-authored"), NS(name="size/L")]
        self.base = NS(ref="main")
        self.head = NS(ref="feature/backoff", sha="head-sha-1")
        self.created_at = NAIVE
        self.merged = False
        self._commits = over.get("commits", [
            fake_commit("a91f3c1d2e3f", "Add backoff\n\nDocketSession-Id: sess-abc\n"),
            fake_commit("b02e4d2e3f4a", "Fix typo"),
        ])
        self._files = over.get("files", [
            fake_file("provisioning/sync.py"),
            fake_file("big/blob.bin", patch=None, additions=9000),
        ])
        self._reviews = over.get("reviews", [
            NS(user=NS(login="alice"), state="APPROVED", submitted_at=NAIVE, commit_id="head-sha-1"),
            NS(user=None, state="COMMENTED", submitted_at=NAIVE, commit_id="head-sha-1"),
        ])
        self._comments = over.get("comments", [
            NS(user=NS(login="alice"), path="provisioning/sync.py", created_at=NAIVE),
        ])
        self._timeline = over.get("timeline", [
            NS(event="review_requested", created_at=NAIVE,
               raw_data={"requested_reviewer": {"login": "alice"}}),
            NS(event="labeled", created_at=NAIVE, raw_data={}),
        ])
        self.raise_on = over.get("raise_on", set())

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise RuntimeError(f"github is unhappy about {name}")

    def get_commits(self):
        self._maybe_raise("commits")
        return self._commits

    def get_files(self):
        self._maybe_raise("files")
        return self._files

    def get_reviews(self):
        self._maybe_raise("reviews")
        return self._reviews

    def get_review_comments(self):
        self._maybe_raise("comments")
        return self._comments

    def as_issue(self):
        self._maybe_raise("timeline")
        return NS(get_timeline=lambda: self._timeline)


class FakeRepo:
    def __init__(self, pr=None, issues=None):
        self.pr = pr or FakePR()
        self.issues = issues if issues is not None else {
            114: NS(number=114, title="Provisioning drifts",
                    body="Please fix.\nScope: provisioning/**, tests/**\n",
                    html_url="https://github.com/acme/widgets/issues/114")
        }

    def get_pull(self, n):
        return self.pr

    def get_issue(self, n):
        if n not in self.issues:
            raise KeyError(n)
        return self.issues[n]


class FakeGithub:
    def __init__(self, repo=None):
        self.repo = repo or FakeRepo()
        self.seen: list[str] = []

    def get_repo(self, full_name):
        self.seen.append(full_name)
        return self.repo


@pytest.fixture()
def collector():
    client = FakeGithub()
    return GithubCollector(client), client


# --------------------------------------------------------------------------- #
# collect()
# --------------------------------------------------------------------------- #


def test_collect_builds_a_full_repo_fragment(collector):
    coll, client = collector
    frag = coll.collect("acme/widgets", 4471)

    assert client.seen == ["acme/widgets"]
    assert frag.repo == "acme/widgets"
    assert frag.pr_number == 4471
    assert frag.title == "Add provisioning backoff"
    assert frag.url.endswith("/pull/4471")
    assert frag.author_login == "dev"
    assert frag.labels == ["docket:human-authored", "size/L"]
    assert frag.base_ref == "main" and frag.head_ref == "feature/backoff"
    assert frag.head_sha == "head-sha-1"
    assert frag.merged is False
    assert frag.created_at == AWARE and frag.created_at.tzinfo is not None
    assert frag.provenance == "real"


def test_collect_reads_commits_and_their_trailers(collector):
    coll, _ = collector
    frag = coll.collect("acme/widgets", 4471)
    first = frag.commits[0]
    assert first.sha == "a91f3c1d2e3f"
    assert first.author_login == "dev"
    assert first.author_email == "dev@example.com"
    assert first.committed_at == AWARE
    assert first.trailers == {"DocketSession-Id": ["sess-abc"]}
    assert frag.commits[1].trailers == {}


def test_collect_parses_patches_and_marks_missing_ones(collector):
    coll, _ = collector
    frag = coll.collect("acme/widgets", 4471)
    parsed, blob = frag.files
    assert parsed.path == "provisioning/sync.py"
    assert parsed.patch_present is True
    assert [l.text for l in parsed.hunks[0].added] == ["b"]
    assert parsed.hunks[0].hunk_id == "provisioning/sync.py#0"
    # GitHub omits the patch for very large files.
    assert blob.patch_present is False and blob.hunks == []


def test_collect_reads_reviews_comments_and_requests(collector):
    coll, _ = collector
    frag = coll.collect("acme/widgets", 4471)
    assert [(r.reviewer, r.state) for r in frag.reviews] == [("alice", "APPROVED"), ("", "COMMENTED")]
    assert frag.reviews[0].submitted_at == AWARE
    assert [(c.reviewer, c.path) for c in frag.review_comments] == [("alice", "provisioning/sync.py")]
    # Only review_requested events become requests.
    assert [r.reviewer for r in frag.review_requests] == ["alice"]


def test_collect_finds_the_linked_issue_and_its_scope(collector):
    coll, _ = collector
    issue = coll.collect("acme/widgets", 4471).linked_issue
    assert issue is not None
    assert issue.number == 114
    assert issue.title == "Provisioning drifts"
    assert issue.scope_paths == ["provisioning/**", "tests/**"]


def test_no_linked_issue_is_none_not_an_error():
    client = FakeGithub(FakeRepo(FakePR(body="No references here.", title="Plain")))
    frag = GithubCollector(client).collect("acme/widgets", 4471)
    assert frag.linked_issue is None


def test_an_issue_that_cannot_be_fetched_is_a_missing_link():
    client = FakeGithub(FakeRepo(FakePR(body="Fixes #999"), issues={}))
    frag = GithubCollector(client).collect("acme/widgets", 4471)
    assert frag.linked_issue is None            # missing evidence is a value


def test_failing_sub_calls_degrade_to_empty_sections():
    pr = FakePR(raise_on={"commits", "timeline", "reviews"})
    frag = GithubCollector(FakeGithub(FakeRepo(pr))).collect("acme/widgets", 4471)
    assert frag.commits == []
    assert frag.reviews == []
    assert frag.review_requests == []
    assert len(frag.files) == 2                 # the rest is kept


class Exploding:
    """Every attribute read raises: the row cannot be built at all."""

    def __getattr__(self, name):
        raise RuntimeError(f"boom on {name}")


def test_thin_rows_degrade_and_exploding_rows_are_skipped():
    # A row with fields missing keeps what exists (missing evidence is a value).
    pr = FakePR(commits=[NS(sha="x", author=None, commit=None), fake_commit("ok1", "subject")],
                files=[NS(filename="broken", patch=None), fake_file("a.py")])
    frag = GithubCollector(FakeGithub(FakeRepo(pr))).collect("acme/widgets", 4471)
    assert [c.sha for c in frag.commits] == ["x", "ok1"]
    assert frag.commits[0].message == "" and frag.commits[0].author_email is None
    assert frag.commits[0].committed_at.year == 1970
    assert [f.path for f in frag.files] == ["broken", "a.py"]
    assert frag.files[0].patch_present is False and frag.files[0].status == "modified"

    # A row that cannot be read at all is skipped; the rest is kept.
    pr2 = FakePR(commits=[Exploding(), fake_commit("ok2", "subject")],
                 files=[Exploding(), fake_file("b.py")],
                 reviews=[Exploding()], comments=[Exploding()], timeline=[Exploding()])
    frag2 = GithubCollector(FakeGithub(FakeRepo(pr2))).collect("acme/widgets", 4471)
    assert [c.sha for c in frag2.commits] == ["ok2"]
    assert [f.path for f in frag2.files] == ["b.py"]
    assert frag2.reviews == [] and frag2.review_comments == [] and frag2.review_requests == []


def test_fragment_round_trips_through_json(collector):
    coll, _ = collector
    frag = coll.collect("acme/widgets", 4471)
    import json
    assert frag.model_validate(json.loads(frag.model_dump_json())) == frag
