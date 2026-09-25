"""Repo Collector: a GitHub PR -> RepoFragment (RFC 7.3).

The PyGithub client is injected, so no test touches the network (rule 8).
This module is the only place PyGithub field names appear (rule 5); if real
data disagrees with the table in RFC 7.3, the table changes, not the callers.

Assumptions taken here (RFC section 0, rule 2 — simplest reading, written down):
  * PyGithub returns naive datetimes on older versions; every datetime is read
    as UTC and returned tz-aware.
  * A sub-call that fails (the timeline is the flaky one) is logged at DEBUG
    and yields an empty list. Missing evidence is a value, not an exception.
  * A review with no user, or a commit with no author object, keeps the
    fragment and leaves the login None.
  * "The last paragraph" of a commit message for trailer parsing is the last
    block of non-empty lines; a message that is one paragraph is searched too,
    which is what `git interpret-trailers` does.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from docket.models.fragments import (
    ChangedFile,
    CommitRef,
    LinkedIssue,
    RepoFragment,
    Review,
    ReviewComment,
    ReviewRequest,
)
from docket.collectors.diffparse import parse_patch

log = logging.getLogger("docket.collectors.github")

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

TRAILER_LINE = re.compile(r"^([A-Za-z0-9-]+):\s*(.+)$")
CLOSING_ISSUE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")
BARE_ISSUE = re.compile(r"#(\d+)")
SCOPE_LINE = re.compile(r"(?im)^\s*scope\s*:\s*(.+)$")

REVIEW_REQUESTED_EVENT = "review_requested"


# --------------------------------------------------------------------------- #
# pure parsers (tested directly)
# --------------------------------------------------------------------------- #


def parse_trailers(message: str | None) -> dict[str, list[str]]:
    """Trailers from the last paragraph of a commit message (RFC 7.3).

    A trailer is a line matching `^([A-Za-z0-9-]+):\\s*(.+)$`. A paragraph that
    holds no trailer at all contributes nothing.
    """
    out: dict[str, list[str]] = {}
    if not message or not message.strip():
        return out

    lines = message.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # The last paragraph: walk back over trailing blanks, then to the blank above.
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    start = end
    while start > 0 and lines[start - 1].strip():
        start -= 1

    for line in lines[start:end]:
        match = TRAILER_LINE.match(line.strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if not value:
            continue
        out.setdefault(key, []).append(value)
    return out


def find_issue_number(body: str | None, title: str | None = None) -> int | None:
    """The PR's linked issue number: closing keywords first, then a bare `#n`."""
    for pattern in (CLOSING_ISSUE, BARE_ISSUE):
        for text in (body or "", title or ""):
            if not text:
                continue
            match = pattern.search(text)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:      # pragma: no cover - the regex guarantees digits
                    continue
    return None


def parse_scope_paths(text: str | None) -> list[str]:
    """`Scope: provisioning/**, tests/**` -> ["provisioning/**", "tests/**"]."""
    if not text:
        return []
    paths: list[str] = []
    for match in SCOPE_LINE.finditer(text):
        for part in match.group(1).split(","):
            cleaned = part.strip().strip("`").strip()
            if cleaned and cleaned not in paths:
                paths.append(cleaned)
    return paths


def to_utc(value: Any) -> datetime:
    """Any datetime PyGithub hands back -> tz-aware UTC. None gives the epoch."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            return to_utc(datetime.fromisoformat(raw))
        except ValueError:
            log.debug("unparseable GitHub timestamp: %r", value)
    return EPOCH


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _login(user: Any) -> str | None:
    login = getattr(user, "login", None) if user is not None else None
    return login if isinstance(login, str) and login else None


def _safe(label: str, fn, default):
    """Run one GitHub sub-call; a failure is a missing section, not a crash."""
    try:
        return fn()
    except Exception as exc:
        log.debug("github %s unavailable: %s", label, exc)
        return default


# --------------------------------------------------------------------------- #
# the collector
# --------------------------------------------------------------------------- #


class GithubCollector:
    """`collect(repo, pr_number) -> RepoFragment`. The client is injected."""

    def __init__(self, github_client: Any):
        self._client = github_client

    # -- pieces, each one testable on a fake ------------------------------- #

    def _commits(self, pr: Any) -> list[CommitRef]:
        commits: list[CommitRef] = []
        for c in _safe("commits", pr.get_commits, []) or []:
            try:
                commit = getattr(c, "commit", None)
                message = _text(getattr(commit, "message", ""))
                author = getattr(commit, "author", None)
                committer = getattr(commit, "committer", None)
                email = getattr(author, "email", None)
                commits.append(
                    CommitRef(
                        sha=_text(getattr(c, "sha", "")),
                        message=message,
                        author_login=_login(getattr(c, "author", None)),
                        author_email=email if isinstance(email, str) and email else None,
                        committed_at=to_utc(getattr(committer, "date", None)),
                        trailers=parse_trailers(message),
                    )
                )
            except Exception as exc:
                log.debug("skipping malformed commit: %s", exc)
        return commits

    def _files(self, pr: Any) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        for f in _safe("files", pr.get_files, []) or []:
            try:
                path = _text(getattr(f, "filename", ""))
                patch = getattr(f, "patch", None)
                patch_present = isinstance(patch, str) and patch != ""
                files.append(
                    ChangedFile(
                        path=path,
                        status=_text(getattr(f, "status", "")) or "modified",
                        additions=int(getattr(f, "additions", 0) or 0),
                        deletions=int(getattr(f, "deletions", 0) or 0),
                        patch_present=patch_present,
                        hunks=parse_patch(path, patch) if patch_present else [],
                    )
                )
            except Exception as exc:
                log.debug("skipping malformed file: %s", exc)
        return files

    def _reviews(self, pr: Any) -> list[Review]:
        reviews: list[Review] = []
        for r in _safe("reviews", pr.get_reviews, []) or []:
            try:
                reviews.append(
                    Review(
                        reviewer=_login(getattr(r, "user", None)) or "",
                        state=_text(getattr(r, "state", "")),
                        submitted_at=to_utc(getattr(r, "submitted_at", None)),
                        commit_id=_text(getattr(r, "commit_id", "")),
                    )
                )
            except Exception as exc:
                log.debug("skipping malformed review: %s", exc)
        return reviews

    def _review_comments(self, pr: Any) -> list[ReviewComment]:
        comments: list[ReviewComment] = []
        for c in _safe("review comments", pr.get_review_comments, []) or []:
            try:
                comments.append(
                    ReviewComment(
                        reviewer=_login(getattr(c, "user", None)) or "",
                        path=_text(getattr(c, "path", "")),
                        created_at=to_utc(getattr(c, "created_at", None)),
                    )
                )
            except Exception as exc:
                log.debug("skipping malformed review comment: %s", exc)
        return comments

    def _review_requests(self, pr: Any) -> list[ReviewRequest]:
        """From the issue timeline: events where `event == "review_requested"`."""
        requests: list[ReviewRequest] = []

        def _timeline() -> Iterable[Any]:
            return pr.as_issue().get_timeline()

        for event in _safe("timeline", _timeline, []) or []:
            try:
                if _text(getattr(event, "event", "")) != REVIEW_REQUESTED_EVENT:
                    continue
                raw = getattr(event, "raw_data", None) or {}
                reviewer = ""
                if isinstance(raw, dict):
                    requested = raw.get("requested_reviewer") or {}
                    if isinstance(requested, dict):
                        reviewer = _text(requested.get("login"))
                    if not reviewer:
                        team = raw.get("requested_team") or {}
                        if isinstance(team, dict):
                            reviewer = _text(team.get("name") or team.get("slug"))
                requests.append(
                    ReviewRequest(
                        reviewer=reviewer,
                        requested_at=to_utc(getattr(event, "created_at", None)),
                    )
                )
            except Exception as exc:
                log.debug("skipping malformed timeline event: %s", exc)
        return requests

    def _linked_issue(self, repo_obj: Any, pr: Any) -> LinkedIssue | None:
        number = find_issue_number(_text(getattr(pr, "body", "")), _text(getattr(pr, "title", "")))
        if number is None:
            return None
        issue = _safe(f"issue #{number}", lambda: repo_obj.get_issue(number), None)
        if issue is None:
            return None
        try:
            body = _text(getattr(issue, "body", ""))
            return LinkedIssue(
                number=int(getattr(issue, "number", number) or number),
                title=_text(getattr(issue, "title", "")),
                body=body,
                url=_text(getattr(issue, "html_url", "")),
                scope_paths=parse_scope_paths(body),
            )
        except Exception as exc:
            log.debug("skipping malformed issue #%s: %s", number, exc)
            return None

    # -- the one public entry point ---------------------------------------- #

    def collect(self, repo: str, pr_number: int) -> RepoFragment:
        repo_obj = self._client.get_repo(repo)
        pr = repo_obj.get_pull(pr_number)

        labels: list[str] = []
        for label in _safe("labels", lambda: list(getattr(pr, "labels", []) or []), []) or []:
            name = getattr(label, "name", None)
            if isinstance(name, str) and name:
                labels.append(name)

        base = getattr(pr, "base", None)
        head = getattr(pr, "head", None)

        return RepoFragment(
            repo=repo,
            pr_number=int(getattr(pr, "number", pr_number) or pr_number),
            title=_text(getattr(pr, "title", "")),
            body=_text(getattr(pr, "body", "")),
            url=_text(getattr(pr, "html_url", "")),
            author_login=_login(getattr(pr, "user", None)) or "",
            labels=labels,
            base_ref=_text(getattr(base, "ref", "")),
            head_ref=_text(getattr(head, "ref", "")),
            head_sha=_text(getattr(head, "sha", "")),
            created_at=to_utc(getattr(pr, "created_at", None)),
            merged=bool(getattr(pr, "merged", False)),
            commits=self._commits(pr),
            files=self._files(pr),
            reviews=self._reviews(pr),
            review_requests=self._review_requests(pr),
            review_comments=self._review_comments(pr),
            linked_issue=self._linked_issue(repo_obj, pr),
            provenance="real",
        )
