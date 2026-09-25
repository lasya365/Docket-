"""The local-git Repo Collector (RFC 7.3, local flavour).

These tests build real git repositories under `tmp_path`. That is allowed and
still obeys rule 8: `git init` in a temp directory is local, offline and
hermetic. Every commit carries `-c user.email` / `-c user.name` so the tests pass
on a machine with no git identity configured.
"""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from docket.collectors.local_git import (
    LocalGitCollector,
    parse_name_status,
    parse_numstat,
    parse_remote,
    repo_identity,
    staged_section_provenance,
)
from docket.collectors.staging import STAGING_FILENAME, load_staged_evidence
from docket.correlate.record_builder import _ask_to_approve_s


# --------------------------------------------------------------------------- #
# a tiny git fixture kit
# --------------------------------------------------------------------------- #

# An identity and no signing on every call, so the tests do not depend on the
# machine's global git config (rule 8: a test is hermetic).
IDENTITY = [
    "-c", "user.email=dev@acme.example",
    "-c", "user.name=Dev Human",
    "-c", "commit.gpgsign=false",
    "-c", "core.hooksPath=/dev/null",
]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *IDENTITY, *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def new_repo(tmp_path: Path, name: str = "app") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main", ".")
    return repo


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture()
def branch_repo(tmp_path: Path) -> Path:
    """`main` with two files, `feat` with two commits on top of it."""
    repo = new_repo(tmp_path)
    write(repo, "keep.py", "".join(f"l{i}\n" for i in range(1, 9)))
    write(repo, "gone.py", "old = 1\n")
    commit_all(repo, "base")

    git(repo, "checkout", "-q", "-b", "feat")
    write(repo, "charge.py", "def charge():\n    return 1\n")
    commit_all(
        repo,
        "feat: retry the webhook\n\nCloses #12\n\nDocketSession-Id: sess-abc-1\n",
    )
    write(repo, "keep.py", "".join(f"l{i}\n" for i in range(1, 8)) + "l8-changed\n")
    write(repo, "charge.py", "def charge():\n    return 2\n")
    commit_all(repo, "fix: bump the retry")
    return repo


# --------------------------------------------------------------------------- #
# pure parsers
# --------------------------------------------------------------------------- #


def test_parse_remote_reads_owner_and_name() -> None:
    assert parse_remote("git@github.com:acme/app.git") == "acme/app"
    assert parse_remote("https://github.com/acme/app") == "acme/app"
    assert parse_remote("ssh://git@github.com:22/acme/app.git") == "acme/app"
    assert parse_remote("/srv/mirrors/app") is None          # a filesystem path names no owner
    assert parse_remote("file:///srv/mirrors/app") is None
    assert parse_remote("") is None
    assert parse_remote(None) is None


def test_parse_numstat_handles_renames_and_binaries() -> None:
    blob = "1\t1\tkeep.py\0" "2\t0\t\0old.py\0new.py\0" "-\t-\tlogo.png\0"
    entries = parse_numstat(blob)
    assert [e["path"] for e in entries] == ["keep.py", "new.py", "logo.png"]
    assert entries[1]["old_path"] == "old.py"
    assert entries[2]["binary"] is True
    assert (entries[2]["additions"], entries[2]["deletions"]) == (0, 0)


def test_parse_name_status_maps_to_the_rfc_vocabulary() -> None:
    blob = "A\0new.py\0" "D\0gone.py\0" "M\0keep.py\0" "R084\0old.py\0moved.py\0"
    assert parse_name_status(blob) == {
        "new.py": "added",
        "gone.py": "removed",
        "keep.py": "modified",
        "moved.py": "renamed",
    }


# --------------------------------------------------------------------------- #
# the collector, on real repositories
# --------------------------------------------------------------------------- #


def test_two_commit_branch_gives_files_hunks_and_added_lines(branch_repo: Path) -> None:
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD")

    assert [c.message.splitlines()[0] for c in fragment.commits] == [
        "feat: retry the webhook",
        "fix: bump the retry",
    ]
    assert fragment.head_sha == git(branch_repo, "rev-parse", "HEAD").strip()
    assert fragment.base_ref == "main" and fragment.head_ref == "HEAD"
    assert fragment.title == "fix: bump the retry"
    assert fragment.author_login == "Dev Human"
    assert fragment.commits[0].author_email == "dev@acme.example"
    assert fragment.url == f"file://{branch_repo}"
    assert fragment.created_at == fragment.commits[0].committed_at
    assert fragment.pr_number == 0

    by_path = {f.path: f for f in fragment.files}
    assert by_path["charge.py"].status == "added"
    assert by_path["charge.py"].additions == 2
    assert [line.line_no for h in by_path["charge.py"].hunks for line in h.added] == [1, 2]
    assert [line.text for h in by_path["charge.py"].hunks for line in h.added] == [
        "def charge():",
        "    return 2",
    ]

    keep = by_path["keep.py"]
    assert keep.status == "modified"
    assert (keep.additions, keep.deletions) == (1, 1)
    assert [line.line_no for h in keep.hunks for line in h.added] == [8]
    assert keep.hunks[0].hunk_id == "keep.py#0"


def test_commit_trailers_are_parsed_with_the_shared_parser(branch_repo: Path) -> None:
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD")
    assert fragment.commits[0].trailers == {"DocketSession-Id": ["sess-abc-1"]}
    # The shared parser searches a one-paragraph message too (that is what
    # `git interpret-trailers` does), so a conventional-commit subject reads as a
    # trailer here exactly as it does for a GitHub PR. Same parser, same quirk.
    assert fragment.commits[1].trailers == {"fix": ["bump the retry"]}


def test_staged_issue_scope_line_is_parsed(branch_repo: Path) -> None:
    staged = {
        "issue": {
            "number": 12,
            "title": "Webhook retries drop payments",
            "body": "Retry failed webhooks.\nScope: charge.py, tests/**",
            "url": "https://tracker.example/12",
        }
    }
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD", staged=staged)
    issue = fragment.linked_issue
    assert issue is not None
    assert issue.number == 12
    assert issue.title == "Webhook retries drop payments"
    assert issue.scope_paths == ["charge.py", "tests/**"]


def test_closes_hint_reads_the_issue_from_the_staging_file(branch_repo: Path) -> None:
    """`Closes #12` in a commit plus a `.docket.json` at the root."""
    (branch_repo / STAGING_FILENAME).write_text(
        json.dumps({"issue": {"title": "Webhook retries", "body": "Scope: charge.py"}}),
        encoding="utf-8",
    )
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD")
    assert fragment.linked_issue is not None
    assert fragment.linked_issue.number == 12            # taken from the commit message
    assert fragment.linked_issue.scope_paths == ["charge.py"]
    # The fragment itself is real git; the intent section is what gets labelled.
    assert fragment.provenance == "real"


def test_closes_hint_with_no_staging_file_is_missing_evidence(branch_repo: Path) -> None:
    """A number with no text is not intent: present = false, never an exception."""
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD")
    assert fragment.linked_issue is None
    assert fragment.provenance == "real"
    assert staged_section_provenance({}) == {}


def test_staged_reviews_reproduce_the_exact_ask_to_approve_s(branch_repo: Path) -> None:
    """RFC 8.3's formula must give back the number the scenario asked for."""
    staged = {
        "reviews": [
            {"reviewer": "alex", "state": "APPROVED", "ask_to_approve_s": 221, "comments": []}
        ]
    }
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD", staged=staged)

    assert len(fragment.reviews) == 1
    review = fragment.reviews[0]
    assert review.reviewer == "alex" and review.state == "APPROVED"
    assert review.commit_id == fragment.head_sha          # a fresh, not a stale, approval
    assert _ask_to_approve_s(fragment, "alex", review.submitted_at) == 221
    assert fragment.review_comments == []

    newest_commit = max(c.committed_at for c in fragment.commits)
    assert review.submitted_at == newest_commit + timedelta(seconds=221)
    assert fragment.review_requests[0].requested_at == newest_commit


def test_staged_ask_survives_commits_made_right_now(branch_repo: Path) -> None:
    """The live-demo condition: commits seconds old must not swallow the number.

    RFC 8.3 subtracts the newest commit at or before the approval, so a scenario
    scored moments after committing is exactly where a staged review time gets
    eaten. The timestamps are derived from that newest commit for this reason.
    """
    from datetime import datetime, timezone

    newest = max(
        LocalGitCollector().collect(branch_repo, "main", "HEAD").commits,
        key=lambda c: c.committed_at,
    ).committed_at
    age = (datetime.now(timezone.utc) - newest).total_seconds()
    assert 0 <= age < 300, "the fixture must commit 'now' for this test to mean anything"

    for wanted in (221, 25, 22, 1320, 0):
        fragment = LocalGitCollector().collect(
            branch_repo,
            "main",
            "HEAD",
            staged={"reviews": [{"reviewer": "alex", "ask_to_approve_s": wanted}]},
        )
        review = fragment.reviews[0]
        assert _ask_to_approve_s(fragment, "alex", review.submitted_at) == wanted


def test_a_staged_submitted_at_that_cannot_hold_the_number_is_re_anchored(branch_repo: Path) -> None:
    """An approval staged before the code existed still reproduces the number."""
    staged = {
        "reviews": [
            {
                "reviewer": "sam",
                "submitted_at": "2000-01-01T00:00:00Z",
                "ask_to_approve_s": 90,
            }
        ]
    }
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD", staged=staged)
    review = fragment.reviews[0]
    assert _ask_to_approve_s(fragment, "sam", review.submitted_at) == 90


def test_staged_review_comments_become_review_comments(branch_repo: Path) -> None:
    staged = {"reviews": [{"reviewer": "alex", "comments": ["charge.py", "keep.py"]}]}
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD", staged=staged)
    assert sorted(c.path for c in fragment.review_comments) == ["charge.py", "keep.py"]
    assert {c.reviewer for c in fragment.review_comments} == {"alex"}


def test_top_level_review_comments_reach_the_fragment(branch_repo: Path) -> None:
    """The shape a staging file uses: several comments across several files."""
    staged = {
        "reviews": [{"reviewer": "rdolan", "state": "APPROVED", "ask_to_approve_s": 1320}],
        "review_comments": [
            {"reviewer": "rdolan", "path": "charge.py", "line": 62, "body": "why 60s?"},
            {"reviewer": "rdolan", "path": "charge.py", "line": 74, "body": "not a hit"},
            {"reviewer": "rdolan", "path": "keep.py", "line": 52, "body": "still 503?"},
        ],
    }
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD", staged=staged)

    assert len(fragment.review_comments) == 3
    assert sorted({c.path for c in fragment.review_comments}) == ["charge.py", "keep.py"]
    assert {c.reviewer for c in fragment.review_comments} == {"rdolan"}
    # `line` and `body` have nowhere to go in a ReviewComment and are dropped,
    # not rejected: one unknown field must not throw away the comment.
    assert all(c.created_at == fragment.reviews[0].submitted_at for c in fragment.review_comments)


def test_review_comments_alone_are_enough_to_stage_a_review(branch_repo: Path) -> None:
    fragment = LocalGitCollector().collect(
        branch_repo, "main", "HEAD", staged={"review_comments": [{"reviewer": "x", "path": "a.py"}]}
    )
    assert len(fragment.review_comments) == 1
    assert "reviews" in fragment.body


def test_a_staged_repo_slug_names_the_change(branch_repo: Path) -> None:
    """A checkout at /tmp still reads as the repository it stands in for."""
    fragment = LocalGitCollector().collect(
        branch_repo, "main", "HEAD", staged={"repo": "acme/storefront-payments"}
    )
    assert fragment.repo == "acme/storefront-payments"
    assert "repo" in fragment.body


def test_the_fragment_stays_real_and_names_what_was_staged(branch_repo: Path) -> None:
    """A staged reviewer does not make a real git diff simulated (RFC P8)."""
    collector = LocalGitCollector()

    plain = collector.collect(branch_repo, "main", "HEAD")
    assert plain.provenance == "real"
    assert plain.body == ""

    staged = collector.collect(
        branch_repo,
        "main",
        "HEAD",
        staged={"pr_number": 41, "title": "Payments webhook retry", "reviews": [{"reviewer": "alex"}]},
    )
    assert staged.provenance == "real"          # commits, files and hunks all came from git
    assert staged.pr_number == 41
    assert staged.title == "Payments webhook retry"
    assert "reviews" in staged.body and "pr_number" in staged.body and "simulated" in staged.body


def test_staged_section_provenance_names_only_the_staged_sections() -> None:
    assert staged_section_provenance(None) == {}
    assert staged_section_provenance({}) == {}
    assert staged_section_provenance({"title": "x", "pr_number": 4}) == {}
    assert staged_section_provenance({"issue": {"number": 1}}) == {"intent": "simulated"}
    assert staged_section_provenance({"reviews": [{"reviewer": "a"}]}) == {"review": "simulated"}
    assert staged_section_provenance({"review_comments": [{"path": "a.py"}]}) == {"review": "simulated"}
    both = staged_section_provenance({"issue": {"number": 1}, "reviews": [{"reviewer": "a"}]})
    assert both == {"intent": "simulated", "review": "simulated"}
    # The diff and the coverage are never named here: git owns one, the
    # CoverageFragment owns the other.
    assert "diff" not in both and "verify" not in both


def test_empty_staging_block_leaves_the_fragment_real(branch_repo: Path) -> None:
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD", staged={})
    assert fragment.provenance == "real"
    assert fragment.reviews == [] and fragment.review_requests == []
    assert staged_section_provenance({}) == {}


def test_repo_without_an_origin_remote_is_named_local(branch_repo: Path) -> None:
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD")
    assert fragment.repo == f"local/{branch_repo.name}"


def test_repo_with_an_origin_remote_uses_owner_and_name(branch_repo: Path) -> None:
    git(branch_repo, "remote", "add", "origin", "git@github.com:acme/payments.git")
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD")
    assert fragment.repo == "acme/payments"


def test_binary_file_has_no_patch(tmp_path: Path) -> None:
    repo = new_repo(tmp_path, "binrepo")
    write(repo, "readme.md", "hello\n")
    commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "logo.png").write_bytes(bytes(range(256)) * 4)
    commit_all(repo, "add a logo")

    fragment = LocalGitCollector().collect(repo, "main", "HEAD")
    logo = next(f for f in fragment.files if f.path == "logo.png")
    assert logo.patch_present is False
    assert logo.hunks == []
    assert logo.status == "added"
    assert (logo.additions, logo.deletions) == (0, 0)


def test_deleted_file_is_reported_as_removed(branch_repo: Path) -> None:
    git(branch_repo, "rm", "-q", "gone.py")
    commit_all(branch_repo, "drop the dead module")

    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD")
    gone = next(f for f in fragment.files if f.path == "gone.py")
    assert gone.status == "removed"
    assert gone.deletions == 1
    assert gone.additions == 0
    assert [line for h in gone.hunks for line in h.added] == []
    assert gone.hunks[0].removed_count == 1


def test_renamed_file_keeps_its_new_path(tmp_path: Path) -> None:
    repo = new_repo(tmp_path, "renrepo")
    write(repo, "old.py", "".join(f"l{i}\n" for i in range(1, 12)))
    commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "feat")
    git(repo, "mv", "old.py", "new.py")
    write(repo, "new.py", "".join(f"l{i}\n" for i in range(1, 11)) + "l11-changed\n")
    commit_all(repo, "move it")

    fragment = LocalGitCollector().collect(repo, "main", "HEAD")
    moved = next(f for f in fragment.files if f.path == "new.py")
    assert moved.status == "renamed"
    assert [line.text for h in moved.hunks for line in h.added] == ["l11-changed"]


# --------------------------------------------------------------------------- #
# errors and injection
# --------------------------------------------------------------------------- #


def test_a_directory_that_is_not_a_repo_raises(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError):
        LocalGitCollector().collect(plain, "main", "HEAD")


def test_an_unresolvable_base_raises(branch_repo: Path) -> None:
    with pytest.raises(ValueError):
        LocalGitCollector().collect(branch_repo, "no-such-branch", "HEAD")


def test_repo_identity_gives_the_slug_and_head_sha(branch_repo: Path) -> None:
    repo, head_sha = repo_identity(branch_repo)
    assert repo == f"local/{branch_repo.name}"
    assert head_sha == git(branch_repo, "rev-parse", "HEAD").strip()


def test_run_git_is_injectable(branch_repo: Path) -> None:
    """The only I/O is one callable, so a caller can trace or fake every call."""
    calls: list[list[str]] = []
    real = LocalGitCollector()._run_git

    def recording(args, cwd):
        calls.append(list(args))
        return real(args, cwd)

    fragment = LocalGitCollector(recording).collect(branch_repo, "main", "HEAD")
    assert fragment.files
    assert all(call and isinstance(call[0], str) for call in calls)
    assert any(call[0] == "log" for call in calls)


# --------------------------------------------------------------------------- #
# the scenario file
# --------------------------------------------------------------------------- #


def test_staged_coverage_accepts_both_spellings_of_the_line_keys(tmp_path: Path) -> None:
    """A Docket-native file writes `executed`/`missing`; coverage.py writes `*_lines`."""
    from docket.collectors.ci_coverage import parse_coverage_json

    (tmp_path / STAGING_FILENAME).write_text(
        json.dumps(
            {
                "coverage": {
                    "ci_status": "success",
                    "files": {
                        "native.py": {"executed": [1, 2, 3], "missing": [4]},
                        "coveragepy.py": {"executed_lines": [7], "missing_lines": [8, 9]},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    spec = load_staged_evidence(tmp_path)
    assert spec is not None
    files = parse_coverage_json(spec.coverage)
    assert files["native.py"].executed == [1, 2, 3]
    assert files["native.py"].missing == [4]
    assert files["coveragepy.py"].executed == [7]
    assert files["coveragepy.py"].missing == [8, 9]
    assert spec.coverage["ci_status"] == "success"      # the rest of the block survives


def test_staging_loader_reads_top_level_review_comments(tmp_path: Path) -> None:
    (tmp_path / STAGING_FILENAME).write_text(
        json.dumps(
            {
                "repo": "acme/cms-core",
                "reviews": [{"reviewer": "rdolan", "ask_to_approve_s": 1320}],
                "review_comments": [
                    {"reviewer": "rdolan", "path": "auth/cache.py", "line": 62, "body": "why?"}
                ],
            }
        ),
        encoding="utf-8",
    )
    spec = load_staged_evidence(tmp_path)
    assert spec is not None
    staged = spec.staged()
    assert staged["repo"] == "acme/cms-core"
    assert staged["review_comments"][0]["path"] == "auth/cache.py"


def test_the_legacy_filename_still_loads_and_the_new_one_wins(tmp_path: Path) -> None:
    """`docket-scenario.json` is accepted silently so repos staged before the
    rename keep working; `.docket.json` is the documented name and takes priority."""
    (tmp_path / "docket-scenario.json").write_text(
        json.dumps({"title": "from the legacy file"}), encoding="utf-8"
    )
    spec = load_staged_evidence(tmp_path)
    assert spec is not None and spec.title == "from the legacy file"

    (tmp_path / STAGING_FILENAME).write_text(
        json.dumps({"title": "from the documented file"}), encoding="utf-8"
    )
    spec = load_staged_evidence(tmp_path)
    assert spec is not None and spec.title == "from the documented file"


def test_staging_loader_is_tolerant(tmp_path: Path) -> None:
    assert load_staged_evidence(tmp_path) is None                       # no file at all

    (tmp_path / STAGING_FILENAME).write_text("{not json", encoding="utf-8")
    assert load_staged_evidence(tmp_path) is None                       # malformed

    (tmp_path / STAGING_FILENAME).write_text("[1, 2]", encoding="utf-8")
    assert load_staged_evidence(tmp_path) is None                       # not an object


def test_staged_block_only_holds_what_was_declared(tmp_path: Path) -> None:
    (tmp_path / STAGING_FILENAME).write_text(
        json.dumps(
            {
                "pr_number": 41,
                "reviews": [{"reviewer": "alex", "ask_to_approve_s": 221}],
                "coverage": {"files": {"charge.py": {"executed_lines": [1]}}},
                "unknown_key": "ignored",
            }
        ),
        encoding="utf-8",
    )
    spec = load_staged_evidence(tmp_path)
    assert spec is not None
    staged = spec.staged()
    assert staged["pr_number"] == 41
    assert staged["reviews"][0]["ask_to_approve_s"] == 221
    assert "title" not in staged and "issue" not in staged
    assert "coverage" not in staged                              # coverage is not repo evidence


def test_a_staging_file_with_only_coverage_leaves_the_fragment_real(branch_repo: Path) -> None:
    (branch_repo / STAGING_FILENAME).write_text(
        json.dumps({"coverage": {"files": {"charge.py": {"executed_lines": [1]}}}}),
        encoding="utf-8",
    )
    spec = load_staged_evidence(branch_repo)
    assert spec is not None
    fragment = LocalGitCollector().collect(branch_repo, "main", "HEAD", staged=spec.staged())
    assert fragment.provenance == "real"
    assert staged_section_provenance(spec.staged()) == {}


# --------------------------------------------------------------------------- #
# the pipeline entry point and the HTTP route
# --------------------------------------------------------------------------- #


def _deps(tmp_path: Path, repo_root: Path, env: dict | None = None):
    from docket import pipeline
    from docket.record.store import Store
    from docket.settings import load_settings

    settings = load_settings(
        repo_root / "config.json",
        env={"DOCKET_DB": str(tmp_path / "t.sqlite"), **(env or {})},
    )
    store = Store(tmp_path / "t.sqlite")
    return settings, store, pipeline.Deps(settings=settings, store=store)


def test_run_local_change_scores_a_branch(branch_repo: Path, tmp_path: Path, repo_root: Path) -> None:
    from docket import pipeline

    (branch_repo / STAGING_FILENAME).write_text(
        json.dumps(
            {
                "pr_number": 41,
                "title": "Payments webhook retry",
                "issue": {
                    "number": 12,
                    "title": "Webhook retries drop payments",
                    "body": "Scope: charge.py",
                    "url": "https://tracker.example/12",
                },
                "reviews": [{"reviewer": "alex", "ask_to_approve_s": 221}],
            }
        ),
        encoding="utf-8",
    )
    settings, store, deps = _deps(tmp_path / "db", repo_root)
    try:
        run = pipeline.run_local_change(branch_repo, "main", "HEAD", deps)

        head_sha = git(branch_repo, "rev-parse", "HEAD").strip()
        assert run.change_key == f"local-local-{branch_repo.name}-{head_sha[:12]}"
        assert run.record.kind == "code"
        assert run.record.intent.present is True
        assert run.record.intent.scope_paths == ["charge.py"]
        assert run.record.review.approvers[0].ask_to_approve_s == 221
        assert run.record.diff.present is True
        # The demo's contract: the diff is real, the staged evidence is not.
        assert run.record.diff.provenance == "real"
        assert run.record.review.provenance == "simulated"
        assert run.record.intent.provenance == "simulated"
        assert run.decision.decision in ("APPROVE", "HOLD")
        assert store.latest_run(run.change_key) is not None
    finally:
        store.close()


def test_a_full_run_labels_the_staged_sections_and_keeps_the_diff_real(
    branch_repo: Path, tmp_path: Path, repo_root: Path
) -> None:
    """The evidence split: diff REAL, intent and review and verify SIMULATED."""
    from docket import pipeline

    (branch_repo / STAGING_FILENAME).write_text(
        json.dumps(
            {
                "pr_number": 218,
                "title": "REQ-118: cache resolved permissions per role",
                "repo": "acme/cms-core",
                "provenance": "simulated",
                "issue": {
                    "number": 118,
                    "title": "Permission resolution adds 40ms",
                    "body": "Scope: charge.py",
                    "url": "https://github.com/acme/cms-core/issues/118",
                },
                "reviews": [{"reviewer": "rdolan", "state": "APPROVED", "ask_to_approve_s": 1320}],
                "review_comments": [
                    {"reviewer": "rdolan", "path": "charge.py", "line": 6, "body": "why?"},
                    {"reviewer": "rdolan", "path": "charge.py", "line": 7, "body": "and this?"},
                    {"reviewer": "rdolan", "path": "keep.py", "line": 8, "body": "still 503?"},
                ],
                "coverage": {
                    "present": True,
                    "ci_status": "success",
                    "source": "simulated",
                    "provenance": "simulated",
                    "files": {"charge.py": {"executed": [1], "missing": [2]}},
                },
            }
        ),
        encoding="utf-8",
    )
    settings, store, deps = _deps(tmp_path / "db", repo_root)
    try:
        run = pipeline.run_local_change(branch_repo, "main", "HEAD", deps)
        record = run.record

        # the repository the scenario stands in for, and the key built from it
        head_sha = git(branch_repo, "rev-parse", "HEAD").strip()
        assert record.change_key == f"local-acme-cms-core-{head_sha[:12]}"

        # the evidence table
        assert record.diff.provenance == "real"
        assert record.intent.provenance == "simulated"
        assert record.review.provenance == "simulated"
        assert record.verify.provenance == "simulated"

        # a well-reviewed change reads as well reviewed
        assert record.review.approvers[0].ask_to_approve_s == 1320
        assert record.review.comments == 3
        assert record.review.files_commented == ["charge.py", "keep.py"]

        # staged coverage really lands, with lines in it
        assert record.verify.present is True
        assert record.verify.ci_status == "success"
        assert record.verify.coverage_by_line["charge.py"].executed == [1]
        assert record.verify.coverage_by_line["charge.py"].missing == [2]

        # and the change is still flagged in the UI, because a section is staged
        row = next(c for c in store.list_changes() if c["change_key"] == record.change_key)
        assert row["simulated"] is True
    finally:
        store.close()


def test_a_run_with_no_staging_file_is_real_all_through(
    branch_repo: Path, tmp_path: Path, repo_root: Path
) -> None:
    from docket import pipeline

    settings, store, deps = _deps(tmp_path / "db", repo_root)
    try:
        record = pipeline.run_local_change(branch_repo, "main", "HEAD", deps).record
        assert record.diff.provenance == "real"
        assert record.intent.provenance == "real"
        assert record.review.provenance == "real"
        assert record.verify.provenance == "real"
    finally:
        store.close()


def test_re_running_the_same_branch_keeps_one_change(branch_repo: Path, tmp_path: Path, repo_root: Path) -> None:
    from docket import pipeline

    settings, store, deps = _deps(tmp_path / "db", repo_root)
    try:
        first = pipeline.run_local_change(branch_repo, "main", "HEAD", deps)
        second = pipeline.run_local_change(branch_repo, "main", "HEAD", deps)
        assert first.change_key == second.change_key
        assert first.run_id != second.run_id
        assert [c["change_key"] for c in store.list_changes()] == [first.change_key]
    finally:
        store.close()


def test_local_coverage_is_absent_without_a_staging_file_or_the_switch(
    branch_repo: Path, tmp_path: Path, repo_root: Path
) -> None:
    from docket import pipeline

    settings, store, deps = _deps(tmp_path / "db", repo_root)
    try:
        run = pipeline.run_local_change(branch_repo, "main", "HEAD", deps)
        assert run.record.verify.present is False
        assert run.record.verify.coverage_by_line == {}
    finally:
        store.close()


def test_local_coverage_honours_the_staging_file_then_the_simulate_switch(
    branch_repo: Path, tmp_path: Path, repo_root: Path
) -> None:
    from docket import pipeline

    (branch_repo / STAGING_FILENAME).write_text(
        json.dumps(
            {"coverage": {"ci_status": "success", "files": {"charge.py": {"executed_lines": [1], "missing_lines": [2]}}}}
        ),
        encoding="utf-8",
    )
    settings, store, deps = _deps(tmp_path / "staged", repo_root)
    try:
        run = pipeline.run_local_change(branch_repo, "main", "HEAD", deps)
        assert run.record.verify.present is True
        assert "charge.py" in run.record.verify.coverage_by_line
        assert run.record.verify.ci_status == "success"
        assert run.record.verify.provenance == "simulated"
    finally:
        store.close()

    (branch_repo / STAGING_FILENAME).unlink()
    settings2, store2, deps2 = _deps(tmp_path / "sim", repo_root, env={"DOCKET_SIMULATE_COVERAGE": "1"})
    try:
        run2 = pipeline.run_local_change(branch_repo, "main", "HEAD", deps2)
        assert run2.record.verify.present is True
        assert run2.record.verify.coverage_by_line
        assert run2.record.verify.provenance == "simulated"
    finally:
        store2.close()


def test_post_run_local_starts_a_run(branch_repo: Path, tmp_path: Path, repo_root: Path) -> None:
    from fastapi.testclient import TestClient

    from docket.server import create_app

    settings, store, deps = _deps(tmp_path / "db", repo_root)
    try:
        with TestClient(create_app(settings=settings, store=store, deps=deps)) as client:
            response = client.post("/run/local", json={"path": str(branch_repo), "base": "main", "head": "HEAD"})
            assert response.status_code == 200
            body = response.json()
            head_sha = git(branch_repo, "rev-parse", "HEAD").strip()
            assert body["change_key"] == f"local-local-{branch_repo.name}-{head_sha[:12]}"

            status = client.get(f"/status/{body['run_id']}").json()
            assert status["stage"] == "done"
            change = client.get(f"/changes/{body['change_key']}").json()
            assert change["record"]["change_key"] == body["change_key"]
    finally:
        store.close()


def test_post_run_local_answers_with_the_key_the_run_actually_uses(
    branch_repo: Path, tmp_path: Path, repo_root: Path
) -> None:
    """The route computes the key before the run starts, so a staged `repo` must
    be visible to both. If they drift, the caller polls a change that never appears."""
    from fastapi.testclient import TestClient

    from docket.server import create_app

    (branch_repo / STAGING_FILENAME).write_text(
        json.dumps({"repo": "acme/storefront-payments", "pr_number": 214}), encoding="utf-8"
    )
    settings, store, deps = _deps(tmp_path / "db", repo_root)
    try:
        with TestClient(create_app(settings=settings, store=store, deps=deps)) as client:
            body = client.post("/run/local", json={"path": str(branch_repo)}).json()
            head_sha = git(branch_repo, "rev-parse", "HEAD").strip()
            assert body["change_key"] == f"local-acme-storefront-payments-{head_sha[:12]}"
            # and the run really stored it under that key
            assert client.get(f"/changes/{body['change_key']}").status_code == 200
    finally:
        store.close()


def test_post_run_local_rejects_a_path_that_is_not_a_repo(tmp_path: Path, repo_root: Path) -> None:
    from fastapi.testclient import TestClient

    from docket.server import create_app

    settings, store, deps = _deps(tmp_path / "db", repo_root)
    try:
        with TestClient(create_app(settings=settings, store=store, deps=deps)) as client:
            assert client.post("/run/local", json={}).status_code == 400
            assert client.post("/run/local", json={"path": str(tmp_path / "nope")}).status_code == 400
    finally:
        store.close()


def test_post_run_local_needs_the_api_token(branch_repo: Path, tmp_path: Path, repo_root: Path) -> None:
    from fastapi.testclient import TestClient

    from docket.server import create_app

    settings, store, deps = _deps(tmp_path / "db", repo_root, env={"DOCKET_API_TOKEN": "secret"})
    try:
        with TestClient(create_app(settings=settings, store=store, deps=deps)) as client:
            assert client.post("/run/local", json={"path": str(branch_repo)}).status_code == 401
            ok = client.post(
                "/run/local",
                json={"path": str(branch_repo)},
                headers={"Authorization": "Bearer secret"},
            )
            assert ok.status_code == 200
    finally:
        store.close()
