"""RFC 15.3: sha match with an abbreviated SHA, trailer match, ghost session,
an ambiguous time match joining nothing. No network, no files (RFC 0 rule 8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from docket.correlate.correlator import correlate, sha_matches
from docket.models.fragments import (
    ChangedFile,
    CommitRef,
    DetailLevel,
    RepoFragment,
    SessionFragment,
    ToolCall,
)

T0 = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)
FULL_SHA = "a91f3c7d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8091"
OTHER_SHA = "ffee1122334455667788990011223344556677aa"


def commit(sha: str, *, trailers: dict | None = None, email: str = "dev@acme.example", minutes: int = 30) -> CommitRef:
    return CommitRef(
        sha=sha,
        message="work",
        author_login="dev",
        author_email=email,
        committed_at=T0 + timedelta(minutes=minutes),
        trailers=trailers or {},
    )


def repo(commits: list[CommitRef], repo_name: str = "acme/provisioning-service") -> RepoFragment:
    return RepoFragment(
        repo=repo_name,
        pr_number=7,
        title="t",
        body="b",
        url=f"https://github.com/{repo_name}/pull/7",
        author_login="dev",
        base_ref="main",
        head_ref="feature",
        head_sha=commits[-1].sha,
        created_at=T0,
        merged=False,
        commits=commits,
        files=[ChangedFile(path="a.py", status="modified", additions=0, deletions=0, patch_present=True)],
        reviews=[],
        review_requests=[],
        review_comments=[],
        linked_issue=None,
    )


def session(
    sid: str,
    *,
    commit_sha: str | None = None,
    email: str | None = "dev@acme.example",
    repo_url: str | None = "https://github.com/acme/provisioning-service.git",
    start_min: int = -60,
    end_min: int = 60,
) -> SessionFragment:
    calls = []
    if commit_sha:
        calls.append(
            ToolCall(
                tool_use_id=f"{sid}-c",
                prompt_id=None,
                name="Bash",
                display_name="Bash",
                success=True,
                git_commit_sha=commit_sha,
                timestamp=T0 + timedelta(minutes=30),
            )
        )
    return SessionFragment(
        session_id=sid,
        user_email=email,
        repo_url=repo_url,
        started_at=T0 + timedelta(minutes=start_min),
        ended_at=T0 + timedelta(minutes=end_min),
        tool_calls=calls,
        detail=DetailLevel(prompts=True, tool_details=True, edits=True),
    )


# --- method 1: sha ---------------------------------------------------------

def test_sha_matches_rule():
    assert sha_matches("a91f3c7", FULL_SHA)          # exactly 7 chars
    assert sha_matches(FULL_SHA, "a91f3c7d")
    assert not sha_matches("a91f3c", FULL_SHA)       # 6 chars is too short
    assert not sha_matches("b91f3c7d", FULL_SHA)
    assert not sha_matches(None, FULL_SHA)


def test_sha_match_with_abbreviated_sha(cfg):
    r = repo([commit(FULL_SHA)])
    joins = correlate(r, [session("s1", commit_sha=FULL_SHA[:8])], cfg.correlator)

    assert len(joins) == 1
    assert (joins[0].session_id, joins[0].method, joins[0].confidence) == ("s1", "sha", "claimed")
    assert joins[0].matched_commits == [FULL_SHA]
    assert joins[0].session is not None and not joins[0].is_ghost


def test_sha_beats_trailer_and_a_session_is_never_examined_twice(cfg):
    r = repo([commit(FULL_SHA, trailers={"DocketSession-Id": ["s1"]})])
    joins = correlate(r, [session("s1", commit_sha=FULL_SHA[:7])], cfg.correlator)

    assert [j.method for j in joins] == ["sha"]


# --- method 2: trailer -----------------------------------------------------

def test_trailer_match(cfg):
    r = repo([commit(FULL_SHA, trailers={"DocketSession-Id": ["s9"]})])
    joins = correlate(r, [session("s9", commit_sha=None)], cfg.correlator)

    assert len(joins) == 1
    assert (joins[0].session_id, joins[0].method, joins[0].confidence) == ("s9", "trailer", "claimed")
    assert joins[0].matched_commits == [FULL_SHA]
    assert joins[0].session is not None


def test_ghost_session(cfg):
    """A commit names a session Docket never received: a finding in its own right."""
    r = repo([commit(FULL_SHA, trailers={"DocketSession-Id": ["never-seen"]})])
    joins = correlate(r, [], cfg.correlator)

    assert len(joins) == 1
    ghost = joins[0]
    assert ghost.is_ghost and ghost.session is None
    assert (ghost.session_id, ghost.method, ghost.confidence) == ("never-seen", "trailer", "claimed")
    assert ghost.note == "commit names session never-seen but Docket never received its telemetry"


def test_ghost_and_real_session_together(cfg):
    r = repo([
        commit(FULL_SHA, trailers={"DocketSession-Id": ["s1"]}, minutes=30),
        commit(OTHER_SHA, trailers={"DocketSession-Id": ["gone"]}, minutes=40),
    ])
    joins = correlate(r, [session("s1", commit_sha=None)], cfg.correlator)

    by_id = {j.session_id: j for j in joins}
    assert set(by_id) == {"s1", "gone"}
    assert by_id["s1"].session is not None
    assert by_id["gone"].is_ghost


# --- method 3: time --------------------------------------------------------

def test_time_match_joins_exactly_one(cfg):
    r = repo([commit(FULL_SHA)])
    joins = correlate(r, [session("only")], cfg.correlator)

    assert len(joins) == 1
    assert (joins[0].method, joins[0].confidence) == ("time", "inferred")
    assert joins[0].matched_commits == [FULL_SHA]


def test_ambiguous_time_match_joins_nothing(cfg):
    r = repo([commit(FULL_SHA)])
    joins = correlate(r, [session("a"), session("b")], cfg.correlator)

    assert [j for j in joins if j.is_join] == []
    assert "ambiguous: 2 sessions overlap" in [j.note for j in joins]


def test_time_match_respects_the_switch(cfg):
    r = repo([commit(FULL_SHA)])
    off = cfg.correlator.model_copy(update={"allow_time_match": False})

    assert [j for j in correlate(r, [session("only")], off) if j.is_join] == []


def test_time_match_needs_matching_repo_and_email(cfg):
    r = repo([commit(FULL_SHA)])
    assert [j for j in correlate(r, [session("s", repo_url="https://github.com/acme/other.git")], cfg.correlator) if j.is_join] == []
    assert [j for j in correlate(r, [session("s", email="someone@else.example")], cfg.correlator) if j.is_join] == []
    # an unknown repo_url is not a disqualification
    assert len(correlate(r, [session("s", repo_url=None)], cfg.correlator)) == 1


def test_time_match_uses_the_grace_window(cfg):
    """The commit lands after the session ends, but inside the grace period."""
    r = repo([commit(FULL_SHA, minutes=30)])
    inside = session("s", start_min=-60, end_min=10)     # 20 min before the commit, grace is 30
    outside = session("s", start_min=-60, end_min=-5)    # 35 min before

    assert len(correlate(r, [inside], cfg.correlator)) == 1
    assert [j for j in correlate(r, [outside], cfg.correlator) if j.is_join] == []


def test_no_matches_returns_an_empty_list(cfg):
    r = repo([commit(FULL_SHA, email="nobody@acme.example")])
    assert [j for j in correlate(r, [session("s", commit_sha=OTHER_SHA, email="dev@acme.example")], cfg.correlator) if j.is_join] == []


def test_correlator_does_no_io(cfg, monkeypatch):
    """RFC 0 rule 3 in spirit: plane 2 reads nothing but its arguments."""
    import builtins
    import socket

    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network in correlate()"))
    monkeypatch.setattr(builtins, "open", lambda *a, **k: pytest.fail("file read in correlate()"))
    r = repo([commit(FULL_SHA)])
    assert len(correlate(r, [session("s1", commit_sha=FULL_SHA[:8])], cfg.correlator)) == 1


# ---------------------------------------------------------------------------
# Why did it not match? (RFC P9: UNMATCHED is a result, so it has to be readable.)
# ---------------------------------------------------------------------------

def notes_for(repo_fragment, sessions, cfg):
    return [j.note for j in correlate(repo_fragment, sessions, cfg.correlator) if j.note]


def test_no_telemetry_at_all_says_so(cfg):
    r = repo([commit(FULL_SHA)])
    text = " ".join(notes_for(r, [], cfg))
    assert "No agent session reached Docket" in text
    assert "GET /sessions" in text, "the note must name the thing to check"


def test_sessions_but_no_trailer_names_the_missing_hook(cfg):
    r = repo([commit(FULL_SHA)])
    s = session("s", email="someone-else@acme.example")
    text = " ".join(notes_for(r, [s], cfg))
    assert "1 agent session(s) were received" in text
    assert "prepare-commit-msg" in text, "the note must name the hook that stamps the trailer"


def test_agent_that_never_committed_is_explained(cfg):
    r = repo([commit(FULL_SHA)])
    s = session("s", email="someone-else@acme.example")   # no git_commit_sha on any tool call
    text = " ".join(notes_for(r, [s], cfg))
    assert "no session reported a commit SHA of its own" in text


def test_email_mismatch_is_named_with_the_commit_author(cfg):
    r = repo([commit(FULL_SHA)])
    s = session("s", email="agent-box@acme.example")
    text = " ".join(notes_for(r, [s], cfg))
    assert "no session's user email matches the author" in text


def test_a_successful_join_adds_no_diagnostic_noise(cfg):
    """The notes are for failures only; a clean join stays clean."""
    r = repo([commit(FULL_SHA)])
    joined = correlate(r, [session("s", commit_sha=r.commits[0].sha)], cfg.correlator)
    assert [j for j in joined if j.is_join]
    assert [j.note for j in joined if j.note] == []
