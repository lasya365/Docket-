"""Repo Collector, local-git flavour: a branch on a laptop -> RepoFragment (RFC 7.3).

`collectors/github.py` needs a real pull request. A branch on a laptop, an
air-gapped checkout or a change on a host Docket has no adapter for has none, so
this collector builds the SAME `RepoFragment` out of `git` alone. Planes 2 to 4
cannot tell the two apart, which is the point: only the source of plane 1 changes.

`git` is the only I/O here, plus one read of the repository's `.docket.json` when
a branch says `Closes #n` and the caller staged nothing (see `collectors/staging.py`).
`run_git` is injected so a test can fake it; the shipped tests use real temporary
repositories, which are local, offline and hermetic (rule 8).

Nothing is duplicated: commit trailers use `parse_trailers`, the issue `Scope:`
line uses `parse_scope_paths`, timestamps use `to_utc` and patches use
`diffparse.parse_patch` - the same functions the GitHub collector uses (rule 2 of
this task, RFC 0 rule 5's spirit: one mapping table, one parser).

The mapping table (RFC 0 rule 5: one table per adapter, and if real data
disagrees the table changes, not the callers):

| Need                | git command                                             |
|---------------------|---------------------------------------------------------|
| repo root           | `rev-parse --show-toplevel`                             |
| head sha            | `rev-parse --verify {ref}^{commit}`                     |
| repo name           | `remote get-url origin` -> `owner/name`                 |
| commits             | `log --reverse {base}..{head} --format=%H %an %ae %cI %B`|
| title / author      | `log -1 {head}` (subject of %B, %an)                    |
| files + counts      | `diff {base}...{head} -M --numstat -z`                  |
| file status         | `diff {base}...{head} -M --name-status -z`              |
| patch               | `diff {base}...{head} -M --unified=3 -- {paths}`        |

Assumptions taken here (RFC section 0, rule 2 - simplest reading, written down):
  * A path that is not a git repository, or a `base`/`head` that git cannot
    resolve, raises `ValueError`. That is a caller error, exactly like asking
    PyGithub for a PR that does not exist; it is not "missing evidence".
    Everything found AFTER those three checks degrades to empty instead.
  * `repo` is `staged["repo"]` when the staging file names one (a checkout at
    /tmp/build-42 is still `acme/storefront`), else `owner/name` from the
    `origin` remote, else `local/{folder name}`, which keeps the change key
    stable for a repo that was never pushed.
  * `pr_number` is 0 when nothing is staged: there is no pull request. The
    change key for a local run never uses it (see `pipeline.change_key_for_local`).
  * `body` carries a note naming the staged parts, and is empty when nothing was
    staged, so the note is never mistaken for a PR description.
  * A file git reports as binary gets `patch_present = false` and no hunks,
    which is what the Line Attributor already reports as `unknown`.
  * A `Closes #n` in a commit message only becomes a `linked_issue` when the
    issue text is actually available (staged, or in the repo's `.docket.json`).
    A bare number with no title, body or scope is not evidence of intent.

Provenance (RFC P8, section 0 rule 7) is decided PER SECTION, not per fragment:
  * `RepoFragment.provenance` stays `"real"` for every local run. The commits,
    the files and the hunks always come out of git, and the diff is the
    strongest evidence Docket has; labelling it simulated because a reviewer was
    staged undersells it and floors every signal at the degraded score.
  * What WAS staged is reported by `staged_section_provenance(staged)`, which the
    pipeline hands to the Record Builder so only the sections that were staged
    (`intent` for an issue, `review` for reviews or comments) carry
    `provenance = "simulated"`. Coverage labels itself through its own fragment.
  * `body` still names every staged part, so a reader of the fragment alone can
    see what was propped up.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from docket.collectors.diffparse import parse_patch
from docket.collectors.github import (
    EPOCH,
    find_issue_number,
    parse_scope_paths,
    parse_trailers,
    to_utc,
)
from docket.collectors.staging import load_staged_evidence
from docket.models.fragments import (
    ChangedFile,
    CommitRef,
    LinkedIssue,
    RepoFragment,
    Review,
    ReviewComment,
    ReviewRequest,
)

log = logging.getLogger("docket.collectors.local_git")

# Record and field separators inside `git log --format=...`. Neither can appear
# in a commit message, so the output is unambiguous without any quoting.
RS = "\x1e"
FS = "\x1f"
LOG_FORMAT = f"%H{FS}%an{FS}%ae{FS}%cI{FS}%B{RS}"

DEFAULT_UNIFIED = 3

# git status letter -> the vocabulary of RFC 6.2 (`added | modified | removed | renamed`).
STATUS_MAP = {
    "A": "added",
    "M": "modified",
    "D": "removed",
    "R": "renamed",
    "C": "copied",
    "T": "modified",        # type change: the content still changed
    "U": "modified",        # unmerged: treated as a modification
}

GitRunner = Callable[[Sequence[str], Path], str]


# --------------------------------------------------------------------------- #
# the one subprocess
# --------------------------------------------------------------------------- #


def run_git(args: Sequence[str], cwd: Path) -> str:
    """Run one `git` command in `cwd` and return stdout. Raises on a non-zero exit."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


_DEFAULT_RUNNER: GitRunner = run_git


# --------------------------------------------------------------------------- #
# pure helpers (tested directly)
# --------------------------------------------------------------------------- #


def parse_remote(url: str | None) -> str | None:
    """`git@github.com:acme/app.git` -> `acme/app`. A local path gives None."""
    if not url:
        return None
    raw = url.strip()
    if raw.endswith(".git"):
        raw = raw[:-4]
    raw = raw.rstrip("/")
    if not raw:
        return None

    if "://" in raw:
        scheme, _, rest = raw.partition("://")
        if scheme.lower() == "file":
            return None                     # a filesystem remote names no owner
        rest = rest.partition("/")[2]       # drop [user@]host[:port]
    elif ":" in raw and not raw.startswith((".", "/", "~")):
        rest = raw.partition(":")[2]        # scp-like: user@host:owner/name
    else:
        return None                         # a plain filesystem path

    parts = [p for p in rest.split("/") if p]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def _nul_fields(blob: str) -> list[str]:
    """`-z` output -> its NUL-terminated fields, with the trailing empty one gone."""
    fields = blob.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    return fields


def parse_numstat(blob: str) -> list[dict[str, Any]]:
    """`git diff --numstat -z` -> one entry per file, in git's order.

    A rename emits `adds\\tdels\\t` with the two paths as separate fields; a
    binary file emits `-` for both counts.
    """
    out: list[dict[str, Any]] = []
    fields = _nul_fields(blob)
    i = 0
    while i < len(fields):
        head = fields[i]
        i += 1
        parts = head.split("\t")
        if len(parts) < 3:
            log.debug("skipping malformed numstat record: %r", head[:60])
            continue
        adds_raw, dels_raw, path = parts[0], parts[1], parts[2]
        old_path: str | None = None
        if path == "":                      # rename or copy: old then new follow
            if i + 1 >= len(fields):
                log.debug("truncated rename record in numstat")
                break
            old_path, path = fields[i], fields[i + 1]
            i += 2
        binary = adds_raw == "-" or dels_raw == "-"
        out.append(
            {
                "path": path,
                "old_path": old_path,
                "additions": 0 if binary else _int(adds_raw),
                "deletions": 0 if binary else _int(dels_raw),
                "binary": binary,
            }
        )
    return out


def parse_name_status(blob: str) -> dict[str, str]:
    """`git diff --name-status -z` -> `{path: status word}` (RFC 6.2 vocabulary)."""
    out: dict[str, str] = {}
    fields = _nul_fields(blob)
    i = 0
    while i < len(fields):
        code = fields[i].strip()
        i += 1
        if not code:
            continue
        letter = code[0].upper()
        takes_two = letter in ("R", "C")
        needed = 2 if takes_two else 1
        if i + needed > len(fields):
            log.debug("truncated name-status record %r", code)
            break
        path = fields[i + 1] if takes_two else fields[i]
        i += needed
        out[path] = STATUS_MAP.get(letter, "modified")
    return out


def _int(value: str) -> int:
    try:
        return int(value.strip())
    except (AttributeError, ValueError):
        return 0


def parse_log(blob: str) -> list[CommitRef]:
    """`git log --format=LOG_FORMAT` -> `CommitRef`s, oldest first if git was told to."""
    commits: list[CommitRef] = []
    for record in blob.split(RS):
        record = record.lstrip("\n")
        if not record.strip():
            continue
        fields = record.split(FS)
        if len(fields) < 5:
            log.debug("skipping malformed git log record: %r", record[:60])
            continue
        sha, author_name, author_email, committed, message = fields[0], fields[1], fields[2], fields[3], fields[4]
        message = message.rstrip("\n")
        commits.append(
            CommitRef(
                sha=sha.strip(),
                message=message,
                author_login=author_name.strip() or None,
                author_email=author_email.strip() or None,
                committed_at=to_utc(committed),
                trailers=parse_trailers(message),
            )
        )
    return commits


# --------------------------------------------------------------------------- #
# identity, used by the HTTP route before the background task starts
# --------------------------------------------------------------------------- #


def staged_section_provenance(staged: dict | None) -> dict[str, str]:
    """Which `ChangeRecord` sections a staged block makes simulated (RFC P8).

    Only the sections whose evidence was staged are named. The diff is never in
    here: it always comes from git. Coverage is not either: a `CoverageFragment`
    carries its own provenance.
    """
    out: dict[str, str] = {}
    if not isinstance(staged, dict):
        return out
    if staged.get("issue"):
        out["intent"] = "simulated"
    if staged.get("reviews") or staged.get("review_comments"):
        out["review"] = "simulated"
    return out


def repo_identity(
    repo_path: str | Path,
    head_ref: str = "HEAD",
    run_git_fn: GitRunner | None = None,
) -> tuple[str, str]:
    """`(repo slug, head sha)` for a local repo. Raises ValueError if it is not one."""
    return LocalGitCollector(run_git_fn).identity(repo_path, head_ref)


# --------------------------------------------------------------------------- #
# the collector
# --------------------------------------------------------------------------- #


class LocalGitCollector:
    """`collect(repo_path, base_ref, head_ref, staged) -> RepoFragment`."""

    def __init__(self, run_git: GitRunner | None = None):
        self._run_git: GitRunner = run_git or _DEFAULT_RUNNER

    # -- git plumbing ------------------------------------------------------ #

    def _git(self, args: Sequence[str], cwd: Path, label: str, default: str = "") -> str:
        """One git call whose failure is a missing section, not a crash (RFC 0 rule 6)."""
        try:
            return self._run_git(list(args), cwd)
        except Exception as exc:
            log.debug("git %s unavailable in %s: %s", label, cwd, exc)
            return default

    def _resolve(self, ref: str, cwd: Path) -> str:
        try:
            return self._run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd).strip()
        except Exception as exc:
            raise ValueError(f"git cannot resolve {ref!r} in {cwd}: {exc}") from exc

    def _root(self, repo_path: str | Path) -> Path:
        path = Path(repo_path).expanduser()
        if not path.is_dir():
            raise ValueError(f"{path} is not a directory")
        try:
            top = self._run_git(["rev-parse", "--show-toplevel"], path).strip()
        except Exception as exc:
            raise ValueError(f"{path} is not a git repository: {exc}") from exc
        return Path(top) if top else path.resolve()

    # -- pieces ------------------------------------------------------------ #

    def identity(self, repo_path: str | Path, head_ref: str = "HEAD") -> tuple[str, str]:
        """The `(repo, head_sha)` a run of this branch WILL use.

        The staging file is read here too: the HTTP route answers with the
        change key before the run starts, and a staged `repo` changes it.
        """
        root = self._root(repo_path)
        spec = load_staged_evidence(root)
        staged_repo = (spec.repo or "") if spec is not None else ""
        return self._repo_name(root, staged_repo), self._resolve(head_ref, root)

    def _repo_name(self, root: Path, staged_repo: str = "") -> str:
        if staged_repo:
            return staged_repo
        remote = self._git(["remote", "get-url", "origin"], root, "origin remote").strip()
        return parse_remote(remote) or f"local/{root.name}"

    def _commits(self, root: Path, base: str, head: str) -> list[CommitRef]:
        blob = self._git(
            ["log", "--reverse", f"{base}..{head}", f"--format={LOG_FORMAT}"],
            root,
            "log",
        )
        return parse_log(blob)

    def _head_commit(self, root: Path, head: str) -> CommitRef | None:
        blob = self._git(["log", "-1", f"--format={LOG_FORMAT}", head], root, "head commit")
        commits = parse_log(blob)
        return commits[0] if commits else None

    def _files(self, root: Path, base: str, head: str) -> list[ChangedFile]:
        spec = f"{base}...{head}"
        numstat = self._git(["diff", spec, "-M", "--numstat", "-z"], root, "numstat")
        statuses = parse_name_status(
            self._git(["diff", spec, "-M", "--name-status", "-z"], root, "name-status")
        )

        files: list[ChangedFile] = []
        for entry in parse_numstat(numstat):
            path = entry["path"]
            paths = [entry["old_path"], path] if entry["old_path"] else [path]
            patch = "" if entry["binary"] else self._patch(root, spec, paths)
            patch_present = bool(patch.strip())
            files.append(
                ChangedFile(
                    path=path,
                    status=statuses.get(path, "modified"),
                    additions=entry["additions"],
                    deletions=entry["deletions"],
                    patch_present=patch_present,
                    hunks=parse_patch(path, patch) if patch_present else [],
                )
            )
        return files

    def _patch(self, root: Path, spec: str, paths: Iterable[str]) -> str:
        args = ["diff", spec, "-M", f"--unified={DEFAULT_UNIFIED}", "--"]
        args += [f":(literal){p}" for p in paths]
        # parse_patch skips everything before the first `@@`, so the file header
        # riding along here costs nothing and keeps this to one git call.
        return self._git(args, root, f"patch for {list(paths)[-1]}")

    # -- staged evidence --------------------------------------------------- #

    def _staged_issue(self, staged: dict, root: Path, commits: list[CommitRef]) -> tuple[LinkedIssue | None, bool]:
        """`(issue, was_staged)`. Staged first, then `Closes #n` + the staging file."""
        raw = staged.get("issue")
        if isinstance(raw, dict) and raw:
            return self._issue_from(raw), True

        messages = "\n\n".join(c.message for c in commits)
        number = find_issue_number(messages)
        if number is None:
            return None, False
        spec = load_staged_evidence(root)
        if spec is None or spec.issue is None:
            # A number with no text is not evidence of intent (assumption above).
            log.debug("commits mention #%s but no issue text is available", number)
            return None, False
        data = spec.issue.model_dump(mode="json")
        if not data.get("number"):
            data["number"] = number
        return self._issue_from(data), True

    @staticmethod
    def _issue_from(raw: dict) -> LinkedIssue | None:
        try:
            body = str(raw.get("body") or "")
            return LinkedIssue(
                number=int(raw.get("number") or 0),
                title=str(raw.get("title") or ""),
                body=body,
                url=str(raw.get("url") or ""),
                scope_paths=parse_scope_paths(body),
            )
        except (TypeError, ValueError) as exc:
            log.debug("skipping malformed staged issue: %s", exc)
            return None

    def _staged_reviews(
        self,
        staged: dict,
        commits: list[CommitRef],
        created_at: datetime,
        head_sha: str,
    ) -> tuple[list[Review], list[ReviewRequest], list[ReviewComment], bool]:
        """Reviews, requests and comments built so RFC 8.3 reproduces `ask_to_approve_s`.

        The Record Builder computes
            `ask_to_approve_s = approved_at - max(requested_at, created_at, newest
             commit at or before approved_at)`.
        So: anchor the approval to the newest commit and put the request exactly
        `ask_to_approve_s` before it. `created_at` is the first commit's date, so
        it can never be the maximum.
        """
        raw_reviews = staged.get("reviews")
        raw_comments = staged.get("review_comments")
        raw_reviews = raw_reviews if isinstance(raw_reviews, list) else []
        raw_comments = raw_comments if isinstance(raw_comments, list) else []
        if not raw_reviews and not raw_comments:
            return [], [], [], False

        commit_times = sorted(c.committed_at for c in commits)
        newest = commit_times[-1] if commit_times else created_at

        reviews: list[Review] = []
        requests: list[ReviewRequest] = []
        comments: list[ReviewComment] = []

        for item in raw_reviews:
            if not isinstance(item, dict):
                log.debug("skipping non-object staged review: %r", item)
                continue
            reviewer = str(item.get("reviewer") or "").strip()
            state = str(item.get("state") or "APPROVED").strip().upper()
            submitted = to_utc(item.get("submitted_at")) if item.get("submitted_at") else None
            requested = to_utc(item.get("requested_at")) if item.get("requested_at") else None

            ask = item.get("ask_to_approve_s")
            if ask is not None:
                try:
                    ask_s = max(0, int(ask))
                except (TypeError, ValueError):
                    log.debug("ignoring unreadable ask_to_approve_s %r", ask)
                    ask_s = None
            else:
                ask_s = None

            if ask_s is not None:
                anchor = newest
                if submitted is not None:
                    before = [t for t in commit_times if t <= submitted]
                    anchor = before[-1] if before else created_at
                    if submitted - timedelta(seconds=ask_s) < anchor:
                        # The staged approval time cannot produce this number; the
                        # number is what the repository asked for, so it wins.
                        log.debug("re-anchoring staged review for %s to the newest commit", reviewer)
                        submitted = newest + timedelta(seconds=ask_s)
                else:
                    submitted = newest + timedelta(seconds=ask_s)
                requested = submitted - timedelta(seconds=ask_s)
            elif submitted is None:
                submitted = newest

            reviews.append(
                Review(
                    reviewer=reviewer,
                    state=state,
                    submitted_at=submitted,
                    commit_id=str(item.get("commit_id") or head_sha),
                )
            )
            if requested is not None:
                requests.append(ReviewRequest(reviewer=reviewer, requested_at=requested))
            for path in item.get("comments") or []:
                if not isinstance(path, str) or not path:
                    continue
                comments.append(ReviewComment(reviewer=reviewer, path=path, created_at=submitted))

        # A top-level `review_comments` list, the shape RepoFragment itself uses.
        # This is what makes a well-reviewed change read as well reviewed: the
        # Record Builder counts them and lists the files they touched (RFC 8.3).
        submitted_by_reviewer = {r.reviewer: r.submitted_at for r in reviews}
        for item in raw_comments:
            if not isinstance(item, dict):
                log.debug("skipping non-object staged review comment: %r", item)
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                log.debug("skipping staged review comment with no path")
                continue
            reviewer = str(item.get("reviewer") or "").strip()
            created = (
                to_utc(item["created_at"])
                if item.get("created_at")
                else submitted_by_reviewer.get(reviewer, newest)
            )
            comments.append(ReviewComment(reviewer=reviewer, path=path, created_at=created))

        return reviews, requests, comments, True

    # -- the one public entry point ---------------------------------------- #

    def collect(
        self,
        repo_path: str | Path,
        base_ref: str = "main",
        head_ref: str = "HEAD",
        staged: dict | None = None,
    ) -> RepoFragment:
        root = self._root(repo_path)
        staged = staged if isinstance(staged, dict) else {}

        head_sha = self._resolve(head_ref, root)
        self._resolve(base_ref, root)               # fail loudly on a bad base (assumption above)

        commits = self._commits(root, base_ref, head_ref)
        head_commit = self._head_commit(root, head_ref)

        created_at = commits[0].committed_at if commits else (
            head_commit.committed_at if head_commit else EPOCH
        )

        staged_parts: list[str] = []

        issue, issue_staged = self._staged_issue(staged, root, commits)
        if issue_staged:
            staged_parts.append("issue")

        reviews, requests, comments, reviews_staged = self._staged_reviews(
            staged, commits, created_at, head_sha
        )
        if reviews_staged:
            staged_parts.append("reviews")

        pr_number = 0
        raw_number = staged.get("pr_number")
        if raw_number:
            try:
                pr_number = int(raw_number)
                staged_parts.append("pr_number")
            except (TypeError, ValueError):
                log.debug("ignoring unreadable staged pr_number %r", raw_number)

        title = str(staged.get("title") or "").strip()
        if title:
            staged_parts.append("title")
        else:
            title = (head_commit.message.splitlines()[0].strip() if head_commit and head_commit.message else "")

        url = str(staged.get("url") or "").strip()
        if url:
            staged_parts.append("url")
        else:
            url = f"file://{root}"

        staged_repo = str(staged.get("repo") or "").strip()
        if staged_repo:
            staged_parts.append("repo")

        # The fragment is real: git produced every commit, file and hunk here.
        # `staged_section_provenance` is what labels the staged sections (P8).
        provenance = "real"
        body = ""
        if staged_parts:
            body = (
                "Local git change. Staged evidence (not read from a real system, "
                f"provenance=simulated): {', '.join(staged_parts)}."
            )

        return RepoFragment(
            repo=self._repo_name(root, staged_repo),
            pr_number=pr_number,
            title=title,
            body=body,
            url=url,
            author_login=(head_commit.author_login if head_commit else "") or "",
            labels=[],
            base_ref=base_ref,
            head_ref=head_ref,
            head_sha=head_sha,
            created_at=created_at,
            merged=False,
            commits=commits,
            files=self._files(root, base_ref, head_ref),
            reviews=reviews,
            review_requests=requests,
            review_comments=comments,
            linked_issue=issue,
            provenance=provenance,
        )
