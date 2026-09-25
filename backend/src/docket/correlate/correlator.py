"""Correlator: join a PR to the agent sessions that produced it (RFC 8.1).

No I/O of any kind. `candidate_sessions` is every session the pipeline loaded
whose window overlaps the PR's commits; the Correlator only reads them.

Assumptions written down as RFC section 0 rule 2 asks:
  * "it equals the PR's repository URL (compare lowercase, without `.git`)":
    RepoFragment carries `repo` ("owner/name") and `url` (the *pull request*
    URL), not a repository URL. The simplest reading is to compare the last two
    path segments of the session's `repo_url`, lowercased and without a trailing
    `.git` or `/`, against `repo_fragment.repo` lowercased.
  * An ambiguous time match produces a note-only JoinResult (see types.py).
"""

from __future__ import annotations

import logging

from docket.models.config import CorrelatorConfig
from docket.models.fragments import RepoFragment, SessionFragment
from docket.correlate.types import JoinResult

log = logging.getLogger("docket.correlate.correlator")

_MIN_ABBREV = 7


def sha_matches(a: str | None, b: str | None) -> bool:
    """RFC 8.1 method 1: one string starts with the other, shorter >= 7 chars."""
    if not a or not b:
        return False
    a = a.strip().lower()
    b = b.strip().lower()
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < _MIN_ABBREV:
        return False
    return longer.startswith(shorter)


def _repo_key(url: str | None) -> str | None:
    """"https://github.com/Acme/Repo.git" -> "acme/repo". None when unknown."""
    if not url:
        return None
    text = url.strip().lower().rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    parts = [p for p in text.replace(":", "/").split("/") if p]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])



def explain_no_match(
    repo_fragment: RepoFragment,
    candidate_sessions: list[SessionFragment],
    cfg: CorrelatorConfig,
) -> list[str]:
    """Why did every join method fail? (RFC P9: UNMATCHED is a result, not a failure path.)

    "unmatched" on its own tells an operator nothing they can act on. These notes name
    the link in the chain that is actually broken, in the order it breaks: telemetry
    never arrived, or it arrived but nothing ties it to these commits.
    """
    notes: list[str] = []
    if not candidate_sessions:
        notes.append(
            "No agent session reached Docket in the window around these commits. Either the "
            "governed repository is not sending telemetry, or it cannot reach this Docket. "
            "Check GET /sessions, and that the environment was exported before the agent started."
        )
        return notes

    notes.append(f"{len(candidate_sessions)} agent session(s) were received in the window, none matched these commits.")

    reported_sha = sum(1 for s in candidate_sessions for c in s.tool_calls if c.git_commit_sha)
    if not reported_sha:
        notes.append(
            "sha: no session reported a commit SHA of its own, so the agent did not run the "
            "commit itself. This method only works when the agent commits."
        )
    else:
        notes.append(f"sha: {reported_sha} commit(s) reported by agents did not match any commit on this change.")

    trailer_key = cfg.trailer_key
    with_trailer = [c for c in repo_fragment.commits if c.trailers.get(trailer_key)]
    if not with_trailer:
        notes.append(
            f"trailer: no commit carries a {trailer_key} trailer. Install the prepare-commit-msg "
            "hook in the governed repository so every commit names the session that produced it."
        )

    if not cfg.allow_time_match:
        notes.append("time: disabled by correlator.allow_time_match.")
    else:
        pr_key = repo_fragment.repo.strip().lower()
        wrong_repo = sum(
            1 for s in candidate_sessions
            if _repo_key(s.repo_url) is not None and _repo_key(s.repo_url) != pr_key
        )
        no_email = sum(1 for s in candidate_sessions if not s.user_email)
        commit_emails = {(c.author_email or "").strip().lower() for c in repo_fragment.commits if c.author_email}
        session_emails = {(s.user_email or "").strip().lower() for s in candidate_sessions if s.user_email}
        if wrong_repo:
            notes.append(f"time: {wrong_repo} session(s) belong to a different repository than {pr_key}.")
        if no_email:
            notes.append(f"time: {no_email} session(s) carry no user email, so they cannot be matched by author.")
        if session_emails and commit_emails and not (session_emails & commit_emails):
            notes.append(
                "time: no session's user email matches the author of any commit here "
                f"(commits: {', '.join(sorted(commit_emails))})."
            )
    return notes


def correlate(
    repo_fragment: RepoFragment,
    candidate_sessions: list[SessionFragment],
    cfg: CorrelatorConfig,
) -> list[JoinResult]:
    """Try sha, then trailer, then time. An earlier method's session is never re-examined."""
    joins: list[JoinResult] = []
    taken: set[str] = set()
    by_id = {s.session_id: s for s in candidate_sessions}

    # --- method 1: sha ----------------------------------------------------
    for session in candidate_sessions:
        if session.session_id in taken:
            continue
        matched: list[str] = []
        for call in session.tool_calls:
            if not call.git_commit_sha:
                continue
            for commit in repo_fragment.commits:
                if sha_matches(call.git_commit_sha, commit.sha) and commit.sha not in matched:
                    matched.append(commit.sha)
        if matched:
            taken.add(session.session_id)
            joins.append(
                JoinResult(
                    session_id=session.session_id,
                    session=session,
                    method="sha",
                    confidence="claimed",
                    matched_commits=matched,
                )
            )

    # --- method 2: trailer ------------------------------------------------
    trailer_key = cfg.trailer_key
    named: dict[str, list[str]] = {}    # session id -> commit shas that name it
    for commit in repo_fragment.commits:
        for key, values in commit.trailers.items():
            if key.lower() != trailer_key.lower():
                continue
            for raw in values:
                sid = raw.strip()
                if not sid:
                    continue
                named.setdefault(sid, [])
                if commit.sha not in named[sid]:
                    named[sid].append(commit.sha)

    for sid, commits in named.items():
        if sid in taken:
            continue
        session = by_id.get(sid)
        taken.add(sid)
        if session is not None:
            joins.append(
                JoinResult(
                    session_id=sid,
                    session=session,
                    method="trailer",
                    confidence="claimed",
                    matched_commits=commits,
                )
            )
        else:
            # A ghost: a finding in its own right (RFC 8.1).
            log.debug("ghost session named by commit trailer: %s", sid)
            joins.append(
                JoinResult(
                    session_id=sid,
                    session=None,
                    method="trailer",
                    confidence="claimed",
                    matched_commits=commits,
                    note=f"commit names session {sid} but Docket never received its telemetry",
                )
            )

    # --- method 3: time ---------------------------------------------------
    if joins or not cfg.allow_time_match:
        return joins

    grace_s = cfg.time_match_grace_minutes * 60
    pr_key = repo_fragment.repo.strip().lower()
    candidates: list[tuple[SessionFragment, list[str]]] = []
    for session in candidate_sessions:
        if session.session_id in taken:
            continue
        session_key = _repo_key(session.repo_url)
        if session_key is not None and session_key != pr_key:
            continue
        if not session.user_email:
            continue
        email = session.user_email.strip().lower()
        window_end = session.ended_at.timestamp() + grace_s
        matched = [
            c.sha
            for c in repo_fragment.commits
            if (c.author_email or "").strip().lower() == email
            and session.started_at.timestamp() <= c.committed_at.timestamp() <= window_end
        ]
        if matched:
            candidates.append((session, matched))

    if len(candidates) == 1:
        session, matched = candidates[0]
        joins.append(
            JoinResult(
                session_id=session.session_id,
                session=session,
                method="time",
                confidence="inferred",
                matched_commits=matched,
            )
        )
    elif len(candidates) > 1:
        # v2 warned about this case and it stays a last resort: join nothing.
        log.debug("ambiguous time match: %d sessions overlap", len(candidates))
        joins.append(JoinResult.note_only(f"ambiguous: {len(candidates)} sessions overlap"))

    if not any(j.is_join for j in joins):
        # Nothing joined and no ghost either: say which link of the chain is broken.
        for note in explain_no_match(repo_fragment, candidate_sessions, cfg):
            joins.append(JoinResult.note_only(note))

    return joins
