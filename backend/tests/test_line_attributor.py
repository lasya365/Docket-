"""RFC 15.3: exact, fuzzy (`mixed`), human, trivial-line inheritance, absolute-path
suffix match, discarded lines, confidence raised to `verified`, and all `unknown`
when edits were not captured."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from docket.correlate.line_attributor import (
    attribute,
    is_trivial,
    match_path,
    norm,
    raise_confidence,
)
from docket.correlate.types import JoinResult
from docket.models.fragments import (
    ChangedFile,
    CommitRef,
    DetailLevel,
    DiffLine,
    EditEvent,
    Hunk,
    RepoFragment,
    SessionFragment,
)

T0 = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)
SHA = "a91f3c7d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8091"


# --- builders --------------------------------------------------------------

def make_file(path: str, hunks: list[list[str]], *, start: int = 10, patch_present: bool = True) -> ChangedFile:
    models: list[Hunk] = []
    line_no = start
    for idx, texts in enumerate(hunks):
        models.append(
            Hunk(
                hunk_id=f"{path}#{idx}",
                path=path,
                old_start=max(line_no - 3, 1),
                old_lines=3,
                new_start=line_no,
                new_lines=len(texts),
                added=[DiffLine(line_no=line_no + i, text=t) for i, t in enumerate(texts)],
                removed_count=0,
            )
        )
        line_no += len(texts) + 5
    return ChangedFile(
        path=path,
        status="modified",
        additions=sum(len(h) for h in hunks),
        deletions=0,
        patch_present=patch_present,
        hunks=models if patch_present else [],
    )


def make_repo(files: list[ChangedFile]) -> RepoFragment:
    return RepoFragment(
        repo="acme/provisioning-service",
        pr_number=7,
        title="t",
        body="b",
        url="https://github.com/acme/provisioning-service/pull/7",
        author_login="dev",
        base_ref="main",
        head_ref="feature",
        head_sha=SHA,
        created_at=T0,
        merged=False,
        commits=[
            CommitRef(
                sha=SHA, message="m", author_login="dev",
                author_email="dev@acme.example", committed_at=T0,
            )
        ],
        files=files,
        reviews=[],
        review_requests=[],
        review_comments=[],
        linked_issue=None,
    )


def make_session(
    sid: str,
    edits: list[tuple[str, list[str]]],
    *,
    edits_captured: bool = True,
    prompt_id: str | None = "p1",
) -> SessionFragment:
    events = [
        EditEvent(
            session_id=sid,
            tool_use_id=f"{sid}-e{i}",
            prompt_id=prompt_id,
            tool_name="Edit",
            file_path=path,
            written_lines=lines,
            timestamp=T0 + timedelta(minutes=i),
        )
        for i, (path, lines) in enumerate(edits)
    ]
    return SessionFragment(
        session_id=sid,
        repo_root="/home/dev/repo",
        started_at=T0 - timedelta(hours=1),
        ended_at=T0 + timedelta(hours=1),
        edit_events=events,
        detail=DetailLevel(prompts=True, tool_details=True, edits=edits_captured),
    )


def joined(session: SessionFragment) -> list[JoinResult]:
    return [
        JoinResult(
            session_id=session.session_id, session=session,
            method="sha", confidence="claimed", matched_commits=[SHA],
        )
    ]


# --- primitives ------------------------------------------------------------

def test_norm_collapses_whitespace():
    assert norm("  a   b\tc \n") == "a b c"


def test_trivial_definition(cfg):
    a = cfg.attributor
    assert is_trivial("", a)
    assert is_trivial("  }  ", a)
    assert is_trivial("});", a)
    assert is_trivial("  else:  ", a)
    assert is_trivial("pass", a)
    assert is_trivial('"""', a)
    assert is_trivial("end", a)
    assert is_trivial("ab", a)                      # shorter than min_line_length
    assert not is_trivial("total = compute(3)", a)
    assert not is_trivial("return total + 1", a)    # "return" alone is trivial, this is not


def test_absolute_path_suffix_match_longest_wins():
    paths = ["sync.py", "provisioning/sync.py", "other.py"]
    assert match_path("/home/dev/repo/provisioning/sync.py", paths) == "provisioning/sync.py"
    assert match_path("/home/dev/repo/sync.py", paths) == "sync.py"
    assert match_path("/home/dev/repo/nope.py", paths) is None


# --- the ladder ------------------------------------------------------------

def test_exact_fuzzy_and_human(cfg):
    """An exact match is `ai`; a line a person tidied is `mixed`; the rest is `human`."""
    repo = make_repo([make_file("app/svc.py", [[
        "total = compute(1) + offset",                  # exact   -> ai
        "value = normalise(payload, strict=True)",      # tidied  -> mixed
        "assert guard_77(ctx) is not None  # by hand",  # nothing -> human
    ]])])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", [
        "total = compute(1) + offset",
        "value = normalise( payload, strict=True )",
    ])])
    joins = joined(session)

    result = attribute(repo, joins, cfg.attributor)

    assert result.attribution_by_line["app/svc.py"] == {10: "ai", 11: "mixed", 12: "human"}
    assert result.totals == {"ai": 1, "mixed": 1, "human": 1, "unknown": 0}
    assert result.matched_lines_by_session == {"s1": 2}


def test_absolute_path_suffix_match_credits_the_right_pr_file(cfg):
    repo = make_repo([
        make_file("provisioning/sync.py", [["total = compute(1) + offset"]]),
        make_file("cache/sync.py", [["total = compute(1) + offset"]]),
    ])
    session = make_session("s1", [("/home/dev/repo/provisioning/sync.py", ["total = compute(1) + offset"])])

    result = attribute(repo, joined(session), cfg.attributor)

    assert result.attribution_by_line["provisioning/sync.py"] == {10: "ai"}
    assert result.attribution_by_line["cache/sync.py"] == {10: "human"}


def test_a_line_is_credited_only_once(cfg):
    """The pool is a multiset: one written line can pay for one diff line."""
    repo = make_repo([make_file("app/svc.py", [[
        "total = compute(1) + offset",
        "total = compute(1) + offset",
    ]])])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", ["total = compute(1) + offset"])])

    result = attribute(repo, joined(session), cfg.attributor)

    assert result.attribution_by_line["app/svc.py"] == {10: "ai", 11: "human"}


# --- trivial-line inheritance ---------------------------------------------

def test_trivial_lines_inherit_the_previous_non_trivial_label(cfg):
    repo = make_repo([make_file("app/svc.py", [[
        "total = compute(1) + offset",                 # ai
        "}",                                           # trivial -> previous = ai
        "assert guard_1(ctx) is not None  # by hand",  # human
        "})",                                          # trivial -> previous = human
    ]])])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", ["total = compute(1) + offset"])])

    result = attribute(repo, joined(session), cfg.attributor)

    assert result.attribution_by_line["app/svc.py"] == {10: "ai", 11: "ai", 12: "human", 13: "human"}
    # Trivial lines are not credited to the session.
    assert result.matched_lines_by_session == {"s1": 1}


def test_a_leading_trivial_line_takes_the_next_label(cfg):
    repo = make_repo([make_file("app/svc.py", [["{", "total = compute(1) + offset"]])])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", ["total = compute(1) + offset"])])

    result = attribute(repo, joined(session), cfg.attributor)
    assert result.attribution_by_line["app/svc.py"] == {10: "ai", 11: "ai"}


def test_a_hunk_of_only_trivial_lines_is_unknown(cfg):
    repo = make_repo([make_file("app/svc.py", [["}", "});"]])])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", ["total = compute(1) + offset"])])

    result = attribute(repo, joined(session), cfg.attributor)

    assert result.attribution_by_line["app/svc.py"] == {10: "unknown", 11: "unknown"}
    assert [h.label for h in result.hunk_attribution] == ["unknown"]


def test_inheritance_does_not_cross_a_hunk_boundary(cfg):
    repo = make_repo([make_file("app/svc.py", [["total = compute(1) + offset"], ["}"]])])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", ["total = compute(1) + offset"])])

    result = attribute(repo, joined(session), cfg.attributor)
    labels = result.attribution_by_line["app/svc.py"]
    assert labels[10] == "ai"
    assert labels[16] == "unknown"


# --- hunk labels -----------------------------------------------------------

def test_hunk_label_needs_eighty_percent(cfg):
    ai_lines = [f"total_{i} = compute({i}) + offset" for i in range(9)]
    human_line = "assert guard_1(ctx) is not None  # by hand"
    repo = make_repo([make_file("app/svc.py", [ai_lines + [human_line]])])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", ai_lines)])

    result = attribute(repo, joined(session), cfg.attributor)
    hunk = result.hunk_attribution[0]

    assert hunk.label == "ai"                        # 9 of 10
    assert hunk.counts == {"ai": 9, "human": 1}
    assert (hunk.session_id, hunk.prompt_id) == ("s1", "p1")


def test_hunk_label_falls_back_to_mixed(cfg):
    ai_lines = [f"total_{i} = compute({i}) + offset" for i in range(5)]
    human_lines = [f"assert guard_{i}(ctx) is not None  # by hand" for i in range(5)]
    repo = make_repo([make_file("app/svc.py", [ai_lines + human_lines])])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", ai_lines)])

    result = attribute(repo, joined(session), cfg.attributor)
    assert result.hunk_attribution[0].label == "mixed"


# --- discarded work --------------------------------------------------------

def test_discarded_lines_and_files(cfg):
    """Whatever is left in a pool is work the agent did that never reached the diff."""
    repo = make_repo([make_file("app/svc.py", [["total = compute(1) + offset"]])])
    session = make_session("s1", [
        ("/home/dev/repo/app/svc.py", [
            "total = compute(1) + offset",
            "dropped_a = compute(41) + offset",
            "dropped_b = compute(42) + offset",
        ]),
        ("/home/dev/repo/app/legacy.py", ["legacy_a = compute(1) + offset"]),
    ])

    result = attribute(repo, joined(session), cfg.attributor)
    discard = result.discarded_by_session["s1"]

    # legacy.py is not a PR file, so it has no pool: only the two leftover svc.py lines count.
    assert discard.lines == 2
    assert discard.files == ["app/legacy.py"]
    assert result.files_written_by_session["s1"] == ["app/svc.py", "app/legacy.py"]


def test_a_written_file_with_no_matched_line_is_discarded(cfg):
    repo = make_repo([
        make_file("app/svc.py", [["total = compute(1) + offset"]]),
        make_file("app/other.py", [["assert guard_1(ctx) is not None  # by hand"]]),
    ])
    session = make_session("s1", [
        ("/home/dev/repo/app/svc.py", ["total = compute(1) + offset"]),
        ("/home/dev/repo/app/other.py", ["nothing_here = compute(99) + offset"]),
    ])

    result = attribute(repo, joined(session), cfg.attributor)
    assert result.discarded_by_session["s1"].files == ["app/other.py"]
    assert result.discarded_by_session["s1"].lines == 1


# --- confidence ------------------------------------------------------------

def test_confidence_raised_to_verified(cfg):
    lines = [f"total_{i} = compute({i}) + offset" for i in range(3)]
    repo = make_repo([make_file("app/svc.py", [lines])])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", lines)])
    joins = joined(session)
    assert joins[0].confidence == "claimed"

    result = attribute(repo, joins, cfg.attributor)
    raise_confidence(joins, result, cfg.correlator.verify_min_lines)

    assert result.matched_lines_by_session["s1"] == 3 >= cfg.correlator.verify_min_lines
    assert joins[0].confidence == "verified"


def test_confidence_stays_claimed_below_the_threshold(cfg):
    lines = ["total_0 = compute(0) + offset"]
    repo = make_repo([make_file("app/svc.py", [lines])])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", lines)])
    joins = joined(session)

    result = attribute(repo, joins, cfg.attributor)
    raise_confidence(joins, result, cfg.correlator.verify_min_lines)

    assert joins[0].confidence == "claimed"


def test_a_ghost_is_never_verified(cfg):
    repo = make_repo([make_file("app/svc.py", [["total = compute(1) + offset"]])])
    joins = [JoinResult(session_id="gone", session=None, method="trailer", confidence="claimed")]

    result = attribute(repo, joins, cfg.attributor)
    raise_confidence(joins, result, cfg.correlator.verify_min_lines)

    assert joins[0].confidence == "claimed"
    assert result.attribution_by_line["app/svc.py"] == {10: "unknown"}


# --- the two "unknown" rules ----------------------------------------------

def test_all_unknown_when_edits_were_not_captured(cfg):
    """RFC 8.2 step 6: without the hook, calling every line `human` would be false."""
    repo = make_repo([make_file("app/svc.py", [[
        "total = compute(1) + offset",
        "assert guard_1(ctx) is not None  # by hand",
    ]])])
    session = make_session("s1", [], edits_captured=False)

    result = attribute(repo, joined(session), cfg.attributor)

    assert result.attribution_by_line["app/svc.py"] == {10: "unknown", 11: "unknown"}
    assert result.totals == {"ai": 0, "mixed": 0, "human": 0, "unknown": 2}
    assert result.matched_lines_by_session == {}
    assert any("no edits were captured" in n for n in result.notes)


def test_all_unknown_when_nothing_joined(cfg):
    repo = make_repo([make_file("app/svc.py", [["total = compute(1) + offset"]])])

    result = attribute(repo, [], cfg.attributor)

    assert result.attribution_by_line["app/svc.py"] == {10: "unknown"}
    assert [h.label for h in result.hunk_attribution] == ["unknown"]


# --- missing patch ---------------------------------------------------------

def test_a_file_without_a_patch_is_a_note_not_a_crash(cfg):
    """RFC 0 rule 6: missing evidence is a value."""
    repo = make_repo([
        make_file("app/svc.py", [["total = compute(1) + offset"]]),
        make_file("huge/generated.py", [], patch_present=False),
    ])
    session = make_session("s1", [("/home/dev/repo/app/svc.py", ["total = compute(1) + offset"])])

    result = attribute(repo, joined(session), cfg.attributor)

    assert "huge/generated.py" not in result.attribution_by_line
    assert any("huge/generated.py" in n for n in result.notes)
    assert result.totals["ai"] == 1


# ---------------------------------------------------------------------------
# Windows: the agent reports backslash paths, the PR reports forward slashes.
# Without normalisation every added line is `unknown` and no join reaches `verified`.
# ---------------------------------------------------------------------------

def test_windows_edit_paths_match_pr_files():
    paths = ["src/permissions.js", "app/svc.py"]
    assert match_path(r"C:\Users\HP\Downloads\repo\src\permissions.js", paths) == "src/permissions.js"
    assert match_path(r"D:\work\repo\app\svc.py", paths) == "app/svc.py"
    assert match_path(r"C:\repo\src\other.js", paths) is None


def test_windows_and_posix_agree_on_the_same_file():
    paths = ["src/permissions.js"]
    assert match_path(r"C:\repo\src\permissions.js", paths) == match_path("/home/dev/repo/src/permissions.js", paths)


def test_a_windows_session_attributes_its_lines(cfg):
    """End to end: a Windows-style edit path must still produce `ai` lines."""
    written = ["const cache = new Map()", "cache.set(roleId, perms)"]
    session = make_session("s1", [(r"C:\Users\HP\repo\src\permissions.js", written)])
    session.repo_root = r"C:\Users\HP\repo"
    rf = make_repo([make_file("src/permissions.js", [written])])
    joins = [JoinResult(session_id="s1", session=session, method="trailer",
                        confidence="claimed", matched_commits=[SHA])]
    result = attribute(rf, joins, cfg.attributor)

    labels = list(result.attribution_by_line["src/permissions.js"].values())
    assert labels == ["ai", "ai"], f"Windows paths must attribute, got {labels}"
    assert result.matched_lines_by_session["s1"] == 2
