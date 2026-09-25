"""Fixture generator (RFC section 15.1).

Builds the four cases from compact specs so the golden numbers in RFC 15.2 are
exact. Two outputs from one spec, which is what keeps them consistent:

  fragments(name) -> plane-1 objects (RepoFragment, SessionFragment[], CoverageFragment,
                     NonCodeChange). These are what `demo/seed/` holds and what the
                     replay pipeline runs through the real planes 2, 3 and 4.
  record(name)    -> a ChangeRecord built directly from the spec, so decide/ can be
                     unit-tested with no dependency on plane 2. The integration test
                     asserts the plane-2 path reproduces the same numbers.

Assumptions written down as RFC section 0 rule 2 asks:
  * Synthetic added lines use the RFC's shape f"line_{path}_{i} = compute({i})" for
    AI-written lines. Human-written lines use a different shape
    (f"assert guard_{i}(ctx) is not None  # hand-written") so that a human line can
    never fuzzy-match a leftover AI pool entry; with the same shape, sibling lines
    match at ratio 0.95 and the attributor's counts would not be reproducible.
  * A `mixed` line is modelled as the agent's draft differing from the merged line by
    one space before the closing paren: ratio 0.989 against its own draft versus 0.955
    against any sibling, so the fuzzy step is deterministic.
  * Test fixtures carry provenance="real" (the golden table in RFC 15.2 reads the raw
    scores, so no signal may be degraded). `demo/seed/` copies are rewritten to
    provenance="simulated" as RFC section 14 requires.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docket.models.change_record import (
    ApproverInfo,
    ChangeRecord,
    DeploySection,
    DiffSection,
    HunkAttribution,
    IntentSection,
    JoinSection,
    ManifestSection,
    ReviewSection,
    SessionSection,
    VerifySection,
)
from docket.models.fragments import (
    ChangedFile,
    CommitRef,
    CoverageFragment,
    DetailLevel,
    DiffLine,
    EditEvent,
    FileCoverage,
    Hunk,
    LinkedIssue,
    PermissionModeChange,
    RepoFragment,
    Review,
    ReviewComment,
    ReviewRequest,
    SessionFragment,
    ToolCall,
    Turn,
)
from docket.models.manifest import (
    AgentManifest,
    KnowledgeRef,
    ManifestSnapshot,
    NonCodeChange,
    PromptRef,
    Regression,
    RehearsalFragment,
)

T0 = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)

FIXTURE_DIR = Path(__file__).resolve().parent / "data"
NAMES = ("held", "cleared", "model-bump", "unmatched")


# ---------------------------------------------------------------------------
# line shapes
# ---------------------------------------------------------------------------

def ai_line(path: str, i: int) -> str:
    return f"line_{path}_{i} = compute({i})"


def draft_line(path: str, i: int) -> str:
    """What the agent wrote before a person tidied it: one extra space."""
    return f"line_{path}_{i} = compute({i} )"


def human_line(path: str, i: int) -> str:
    return f"assert guard_{i}(ctx) is not None  # hand-written"


# ---------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------

@dataclass
class FileSpec:
    path: str
    in_scope: bool
    added: int
    ai: int
    mixed: int
    human: int
    ai_executable: int      # AI lines that coverage knows about
    ai_covered: int         # of those, executed

    @property
    def ai_lines(self) -> int:
        return self.ai + self.mixed


@dataclass
class CodeSpec:
    name: str
    repo: str
    pr_number: int
    title: str
    files: list[FileSpec]
    issue_number: int | None
    issue_title: str
    issue_scope: list[str]
    approvers: list[tuple[str, int]]            # (login, ask_to_approve_s)
    comments: list[tuple[str, str]]             # (reviewer, path)
    tool_calls_rule: int
    tool_calls_human: int
    sensitive_tool: str | None
    labels: list[str] = field(default_factory=list)
    sessions: int = 1
    discarded_lines: int = 0
    discarded_file: str | None = None
    ci_status: str = "success"
    coverage_present: bool = True
    story: str = ""

    @property
    def lines_added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def ai_total(self) -> int:
        return sum(f.ai_lines for f in self.files)


HELD = CodeSpec(
    name="held",
    repo="acme/provisioning-service",
    pr_number=4471,
    title="Rework provisioning sync and entitlement grants",
    files=[
        FileSpec("provisioning/sync.py", True, 250, 180, 20, 50, 200, 130),
        FileSpec("provisioning/backoff.py", True, 72, 64, 0, 8, 64, 57),
        FileSpec("entitlement/grants.py", False, 210, 200, 10, 0, 210, 0),
        FileSpec("cache/reconcile.py", False, 130, 120, 10, 0, 130, 0),
        FileSpec("provisioning/config.py", True, 150, 0, 0, 150, 0, 0),
    ],
    issue_number=114,
    issue_title="Provisioning sync retries drop entitlements",
    issue_scope=["provisioning/**"],
    approvers=[("rdolan", 112), ("mkeane", 109)],
    comments=[],
    tool_calls_rule=43,
    tool_calls_human=2,
    sensitive_tool="entitlement_svc.grant",
    discarded_lines=12,
    discarded_file="provisioning/legacy_sync.py",
    story=(
        "AI wrote 604 of 812 lines, 340 of them outside the ticket's scope, approved in "
        "under two minutes, 31% coverage on AI lines, the agent called entitlement_svc.grant"
    ),
)

CLEARED = CodeSpec(
    name="cleared",
    repo="acme/provisioning-service",
    pr_number=4482,
    title="Round invoice tax to the customer's currency",
    files=[
        FileSpec("billing/invoice.py", True, 30, 24, 2, 4, 20, 19),
        FileSpec("billing/tax.py", True, 20, 14, 2, 4, 12, 12),
        FileSpec("billing/report.py", True, 14, 8, 0, 6, 8, 7),
    ],
    issue_number=131,
    issue_title="Invoice tax rounds to two decimals in every currency",
    issue_scope=["billing/**"],
    approvers=[("rdolan", 1320)],
    comments=[("rdolan", "billing/invoice.py"), ("rdolan", "billing/invoice.py"), ("rdolan", "billing/tax.py")],
    tool_calls_rule=6,
    tool_calls_human=2,
    sensitive_tool=None,
    story="small AI-assisted change, in scope, reviewed for 22 minutes with comments, 95% coverage on AI lines",
)

UNMATCHED = CodeSpec(
    name="unmatched",
    repo="acme/provisioning-service",
    pr_number=4600,
    title="Widen entitlement role lookup",
    files=[
        FileSpec("entitlement/roles.py", False, 40, 0, 0, 40, 0, 0),
        FileSpec("provisioning/hooks.py", False, 20, 0, 0, 20, 0, 0),
    ],
    issue_number=None,
    issue_title="",
    issue_scope=[],
    approvers=[("mkeane", 900)],
    comments=[],
    tool_calls_rule=0,
    tool_calls_human=0,
    sensitive_tool=None,
    sessions=0,
    ci_status="success",
    coverage_present=False,
    story="a PR touching entitlement/ with no session and no label",
)

CODE_SPECS = {s.name: s for s in (HELD, CLEARED, UNMATCHED)}


# ---------------------------------------------------------------------------
# layout helpers
# ---------------------------------------------------------------------------

def layout_hunks(n_added: int, start: int = 12, hunk_size: int = 40, gap: int = 7) -> list[list[int]]:
    """New-file line numbers for the added lines, grouped into hunks."""
    hunks: list[list[int]] = []
    placed = 0
    cur = start
    while placed < n_added:
        take = min(hunk_size, n_added - placed)
        hunks.append(list(range(cur, cur + take)))
        placed += take
        cur += take + gap
    return hunks


def file_lines(spec: FileSpec) -> tuple[list[list[int]], dict[int, str]]:
    """Returns (hunks of line numbers, line_no -> label) with ai first, then mixed, then human."""
    hunks = layout_hunks(spec.added)
    flat = [n for h in hunks for n in h]
    labels: dict[int, str] = {}
    for idx, n in enumerate(flat):
        if idx < spec.ai:
            labels[n] = "ai"
        elif idx < spec.ai + spec.mixed:
            labels[n] = "mixed"
        else:
            labels[n] = "human"
    return hunks, labels


def line_text(path: str, n: int, label: str) -> str:
    return human_line(path, n) if label == "human" else ai_line(path, n)


# ---------------------------------------------------------------------------
# plane-1 fragments
# ---------------------------------------------------------------------------

def _changed_files(spec: CodeSpec) -> tuple[list[ChangedFile], dict[str, dict[int, str]]]:
    files: list[ChangedFile] = []
    labels_by_path: dict[str, dict[int, str]] = {}
    for fs in spec.files:
        hunks, labels = file_lines(fs)
        labels_by_path[fs.path] = labels
        hunk_models: list[Hunk] = []
        for idx, nums in enumerate(hunks):
            hunk_models.append(
                Hunk(
                    hunk_id=f"{fs.path}#{idx}",
                    path=fs.path,
                    old_start=max(nums[0] - 3, 1),
                    old_lines=3,
                    new_start=nums[0],
                    new_lines=len(nums) + 3,
                    added=[DiffLine(line_no=n, text=line_text(fs.path, n, labels[n])) for n in nums],
                    removed_count=1 if idx == 0 else 0,
                )
            )
        files.append(
            ChangedFile(
                path=fs.path,
                status="modified",
                additions=fs.added,
                deletions=sum(h.removed_count for h in hunk_models),
                patch_present=True,
                hunks=hunk_models,
            )
        )
    return files, labels_by_path


def _commits(spec: CodeSpec, session_ids: list[str]) -> list[CommitRef]:
    commits: list[CommitRef] = []
    for i in range(2):
        sha = f"{spec.pr_number:04d}a91f3c{i}d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8" [:40]
        trailers: dict[str, list[str]] = {}
        if session_ids and i == 1:
            trailers = {"DocketSession-Id": [session_ids[0]]}
        commits.append(
            CommitRef(
                sha=sha,
                message=f"{spec.title} (part {i + 1})"
                + ("\n\nDocketSession-Id: " + session_ids[0] if trailers else ""),
                author_login="tkoenig",
                author_email="tkoenig@acme.example",
                committed_at=T0 + timedelta(minutes=30 + i * 20),
                trailers=trailers,
            )
        )
    return commits


def _session_ids(spec: CodeSpec) -> list[str]:
    return [f"{spec.name}-session-{i + 1}" for i in range(spec.sessions)]


def build_repo_fragment(spec: CodeSpec) -> RepoFragment:
    files, _ = _changed_files(spec)
    session_ids = _session_ids(spec)
    commits = _commits(spec, session_ids)
    head_sha = commits[-1].sha
    created_at = T0

    issue = None
    if spec.issue_number is not None:
        scope_line = ("Scope: " + ", ".join(spec.issue_scope)) if spec.issue_scope else ""
        issue = LinkedIssue(
            number=spec.issue_number,
            title=spec.issue_title,
            body=(
                f"{spec.issue_title}.\n\n"
                "Retries must not drop an entitlement that was already granted.\n"
                f"{scope_line}\n"
            ),
            url=f"https://github.com/{spec.repo}/issues/{spec.issue_number}",
            scope_paths=list(spec.issue_scope),
        )

    # Approvals: each reviewer is asked at a fixed time and approves ask_to_approve_s later,
    # after the newest commit, so the Record Builder recomputes exactly that number.
    requested_at = commits[-1].committed_at + timedelta(minutes=5)
    reviews: list[Review] = []
    requests: list[ReviewRequest] = []
    for login, ask_s in spec.approvers:
        requests.append(ReviewRequest(reviewer=login, requested_at=requested_at))
        reviews.append(
            Review(
                reviewer=login,
                state="APPROVED",
                submitted_at=requested_at + timedelta(seconds=ask_s),
                commit_id=head_sha,
            )
        )
    comments = [
        ReviewComment(reviewer=r, path=p, created_at=requested_at + timedelta(seconds=30 + i * 10))
        for i, (r, p) in enumerate(spec.comments)
    ]

    return RepoFragment(
        repo=spec.repo,
        pr_number=spec.pr_number,
        title=spec.title,
        body=(f"Closes #{spec.issue_number}\n\n" if spec.issue_number else "") + spec.story,
        url=f"https://github.com/{spec.repo}/pull/{spec.pr_number}",
        author_login="tkoenig",
        labels=list(spec.labels),
        base_ref="main",
        head_ref=f"feature/{spec.name}",
        head_sha=head_sha,
        created_at=created_at,
        merged=False,
        commits=commits,
        files=files,
        reviews=reviews,
        review_requests=requests,
        review_comments=comments,
        linked_issue=issue,
    )


def build_sessions(spec: CodeSpec) -> list[SessionFragment]:
    if spec.sessions == 0:
        return []
    session_ids = _session_ids(spec)
    commits = _commits(spec, session_ids)
    _, labels_by_path = _changed_files(spec)
    sid = session_ids[0]
    started = T0 - timedelta(minutes=90)

    turns = [
        Turn(
            prompt_id=f"{sid}-p{i + 1}",
            started_at=started + timedelta(minutes=i * 12),
            prompt_text=text,
            prompt_length=len(text),
            tool_use_ids=[],
        )
        for i, text in enumerate(
            [
                "Fix the provisioning sync so retries do not drop entitlements. See issue 114.",
                "Add exponential backoff to the sync loop and cover it with tests.",
                "The grants table also needs the new retry key, update it too.",
                "Reconcile the cache after a failed grant.",
                "Run the tests and commit.",
            ]
            if spec.name == "held"
            else [
                "Round invoice tax to two decimals in the customer's currency. Issue 131.",
                "Add a regression test for JPY, which has no minor unit.",
            ]
        )
    ]

    # edits: one EditEvent per file, carrying what the agent wrote
    edits: list[EditEvent] = []
    t = started + timedelta(minutes=3)
    for idx, fs in enumerate(spec.files):
        labels = labels_by_path[fs.path]
        written: list[str] = []
        for n, label in labels.items():
            if label == "ai":
                written.append(ai_line(fs.path, n))
            elif label == "mixed":
                written.append(draft_line(fs.path, n))
        if not written:
            continue
        edits.append(
            EditEvent(
                session_id=sid,
                tool_use_id=f"{sid}-edit-{idx}",
                prompt_id=turns[min(idx, len(turns) - 1)].prompt_id,
                tool_name="Edit",
                file_path=f"/home/dev/repo/{fs.path}",
                written_lines=written,
                removed_lines=[],
                timestamp=t + timedelta(minutes=idx),
            )
        )

    # work the agent did that never reached the diff
    if spec.discarded_lines:
        first = spec.files[0]
        edits.append(
            EditEvent(
                session_id=sid,
                tool_use_id=f"{sid}-edit-discarded",
                prompt_id=turns[0].prompt_id,
                tool_name="Edit",
                file_path=f"/home/dev/repo/{first.path}",
                written_lines=[ai_line(first.path, 9000 + i) for i in range(spec.discarded_lines)],
                timestamp=t + timedelta(minutes=20),
            )
        )
    if spec.discarded_file:
        edits.append(
            EditEvent(
                session_id=sid,
                tool_use_id=f"{sid}-edit-legacy",
                prompt_id=turns[0].prompt_id,
                tool_name="Write",
                file_path=f"/home/dev/repo/{spec.discarded_file}",
                written_lines=[ai_line(spec.discarded_file, i) for i in range(25)],
                timestamp=t + timedelta(minutes=22),
            )
        )

    # tool calls
    calls: list[ToolCall] = []
    n_calls = spec.tool_calls_rule + spec.tool_calls_human
    for i in range(n_calls):
        human = i >= spec.tool_calls_rule
        is_sensitive = spec.sensitive_tool is not None and i == 0
        if is_sensitive:
            server, tool = spec.sensitive_tool.split(".", 1)
            name, display = "mcp_tool", spec.sensitive_tool
        else:
            server = tool = None
            name = display = ["Edit", "Read", "Bash", "Grep"][i % 4]
        calls.append(
            ToolCall(
                tool_use_id=f"{sid}-call-{i}",
                prompt_id=turns[i % len(turns)].prompt_id,
                name=name,
                display_name=display,
                mcp_server=server,
                mcp_tool=tool,
                bash_command="pytest -q" if name == "Bash" else None,
                file_path=None,
                success=True,
                duration_ms=120 + i,
                decision_source="user_permanent" if human else "config",
                sandbox_disabled=False,
                timestamp=started + timedelta(minutes=2 + i),
            )
        )
    # the commit the agent made itself: an abbreviated SHA, which is how method "sha" joins
    calls.append(
        ToolCall(
            tool_use_id=f"{sid}-call-commit",
            prompt_id=turns[-1].prompt_id,
            name="Bash",
            display_name="Bash",
            bash_command="git commit -m 'sync retries'",
            success=True,
            duration_ms=340,
            decision_source="config",
            git_commit_sha=commits[-1].sha[:8],
            git_branch=f"feature/{spec.name}",
            timestamp=commits[-1].committed_at,
        )
    )

    return [
        SessionFragment(
            session_id=sid,
            agent="claude-code",
            agent_version="2.1.269",
            models=["claude-sonnet-5"],
            user_email="tkoenig@acme.example",
            repo_url=f"https://github.com/{spec.repo}.git",
            repo_root="/home/dev/repo",
            started_at=started,
            ended_at=commits[-1].committed_at + timedelta(minutes=2),
            turns=turns,
            tool_calls=calls,
            rejections=[],
            permission_mode_changes=[
                PermissionModeChange(
                    from_mode="default",
                    to_mode="acceptEdits",
                    trigger="user",
                    timestamp=started + timedelta(minutes=1),
                )
            ],
            api_retries=2 if spec.name == "held" else 0,
            edit_events=edits,
            detail=DetailLevel(prompts=True, tool_details=True, edits=True),
            provenance="real",
        )
    ]


def build_coverage(spec: CodeSpec) -> CoverageFragment:
    if not spec.coverage_present:
        return CoverageFragment(
            head_sha=build_repo_fragment(spec).head_sha,
            ci_status=spec.ci_status,
            files={},
            present=False,
            source="none",
            provenance="real",
        )

    files: dict[str, FileCoverage] = {}
    for fs in spec.files:
        _, labels = file_lines(fs)
        ai_nums = [n for n, lab in labels.items() if lab in ("ai", "mixed")]
        human_nums = [n for n, lab in labels.items() if lab == "human"]

        executed: list[int] = []
        missing: list[int] = []
        # AI lines: the first `ai_executable` are known to coverage, `ai_covered` of them executed
        known = ai_nums[: fs.ai_executable]
        executed.extend(known[: fs.ai_covered])
        missing.extend(known[fs.ai_covered :])
        # human-written added lines are exercised by the existing tests
        executed.extend(human_nums)
        # Pre-existing lines of the file, nearly all covered. This is the file average
        # a dashboard shows today, and the contrast the untested signal exists to show:
        # the file reads as ~90% covered while the AI-written lines inside it do not.
        # The file is deliberately several times larger than the change.
        added = set(labels)
        top = (max(labels) if labels else 0) + 3 * fs.added + 60
        for n in range(1, top + 1):
            if n in added:
                continue
            (missing if n % 20 == 0 else executed).append(n)
        files[fs.path] = FileCoverage(executed=sorted(executed), missing=sorted(missing))

    return CoverageFragment(
        head_sha=build_repo_fragment(spec).head_sha,
        ci_status=spec.ci_status,
        files=files,
        present=True,
        source="github-artifact",
        provenance="real",
    )


# ---------------------------------------------------------------------------
# model-bump: the no-code case
# ---------------------------------------------------------------------------

def _manifest(model: str, tools: list[str], change_ref: str | None = None) -> AgentManifest:
    return AgentManifest(
        agent_id="resolution-agent",
        model=model,
        prompt=PromptRef(path="prompts/system.md", sha256="a" * 64),
        tools=tools,
        permissions=["tickets:write"],
        knowledge=[KnowledgeRef(id="kb-2211", revision="6")],
        workflows=["incident-triage", "entitlement-provisioning", "knowledge-retrieval"],
        change_ref=change_ref,
    )


def build_non_code_change() -> NonCodeChange:
    tools = ["kb.search", "ticket.update"]
    before = ManifestSnapshot(
        agent_id="resolution-agent",
        manifest_hash="b" * 64,
        manifest=_manifest("vendor-4.1", tools),
        observed_at=T0 - timedelta(days=9),
        sequence=1,
    )
    after = ManifestSnapshot(
        agent_id="resolution-agent",
        manifest_hash="c" * 64,
        manifest=_manifest("vendor-4.2", tools),
        observed_at=T0,
        sequence=2,
    )
    rehearsal = RehearsalFragment(
        agent_id="resolution-agent",
        manifest_hash=after.manifest_hash,
        cases=50,
        identical=44,
        changed_acceptable=4,
        regressed=2,
        regressions=[
            Regression(case_id="INC-20114", before="escalate", after="resolve"),
            Regression(case_id="INC-20337", before="entitlement.check", after="ticket.close"),
        ],
        provenance="simulated",
    )
    return NonCodeChange(
        agent_id="resolution-agent",
        kind="model_version",
        changed_keys=["model"],
        before=before,
        after=after,
        rehearsal=rehearsal,
        detected_at=T0,
        provenance="real",
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def fragments(name: str) -> dict:
    if name == "model-bump":
        return {"non_code_change": build_non_code_change()}
    spec = CODE_SPECS[name]
    return {
        "repo_fragment": build_repo_fragment(spec),
        "sessions": build_sessions(spec),
        "coverage": build_coverage(spec),
    }


def change_key_for(name: str) -> str:
    if name == "model-bump":
        nc = build_non_code_change()
        return f"mf-{nc.agent_id}-{nc.after.manifest_hash[:12]}"
    spec = CODE_SPECS[name]
    owner, repo = spec.repo.split("/")
    return f"pr-{owner}-{repo}-{spec.pr_number}"


def record(name: str) -> ChangeRecord:
    """A ChangeRecord built straight from the spec (RFC 15.1), for decide/ unit tests."""
    if name == "model-bump":
        return _record_non_code()
    return _record_code(CODE_SPECS[name])


def _record_code(spec: CodeSpec) -> ChangeRecord:
    files, labels_by_path = _changed_files(spec)
    repo_fragment = build_repo_fragment(spec)
    coverage = build_coverage(spec)
    sessions_frag = build_sessions(spec)
    has_sessions = bool(sessions_frag)

    attribution_by_line: dict[str, dict[int, str]] = {}
    totals = {"ai": 0, "human": 0, "mixed": 0, "unknown": 0}
    hunk_attribution: list[HunkAttribution] = []
    for fs in spec.files:
        labels = labels_by_path[fs.path]
        if has_sessions:
            per_line = dict(labels)
        else:
            per_line = {n: "unknown" for n in labels}
        attribution_by_line[fs.path] = per_line
        for lab in per_line.values():
            totals[lab] += 1
        hunks, _ = file_lines(fs)
        for idx, nums in enumerate(hunks):
            counts: dict[str, int] = {}
            for n in nums:
                counts[per_line[n]] = counts.get(per_line[n], 0) + 1
            top = max(counts, key=lambda k: counts[k])
            label = top if counts[top] >= 0.8 * len(nums) else "mixed"
            if set(counts) == {"unknown"}:
                label = "unknown"
            hunk_attribution.append(
                HunkAttribution(
                    hunk_id=f"{fs.path}#{idx}",
                    label=label,
                    counts=counts,
                    session_id=sessions_frag[0].session_id if has_sessions else None,
                    prompt_id=sessions_frag[0].turns[0].prompt_id if has_sessions else None,
                )
            )

    sessions: list[SessionSection] = []
    for sf in sessions_frag:
        approvals = {"rule": spec.tool_calls_rule + 1, "hook": 0, "human": spec.tool_calls_human}
        sessions.append(
            SessionSection(
                present=True,
                session_id=sf.session_id,
                agent=sf.agent,
                model=sf.models[0] if sf.models else None,
                turns=len(sf.turns),
                retries=sf.api_retries,
                files_written=[f.path for f in spec.files if f.ai_lines]
                + ([spec.discarded_file] if spec.discarded_file else []),
                files_discarded=[spec.discarded_file] if spec.discarded_file else [],
                lines_written=sum(len(e.written_lines) for e in sf.edit_events),
                lines_discarded=spec.discarded_lines,
                tools_used=list(sf.tool_calls),
                permission_modes=["default", "acceptEdits"],
                approvals_by=approvals,
                sandbox_disabled_calls=0,
                prompt_excerpts=[t.prompt_text or "" for t in sf.turns],
                detail=sf.detail,
                join_method="sha",
                join_confidence="verified",
                matched_commits=[repo_fragment.commits[-1].sha],
                provenance="real",
            )
        )

    intent = IntentSection(present=False)
    if repo_fragment.linked_issue is not None:
        issue = repo_fragment.linked_issue
        intent = IntentSection(
            present=True,
            source="github-issue",
            ref=f"#{issue.number}",
            url=issue.url,
            text=f"{issue.title}\n\n{issue.body}",
            scope_paths=list(issue.scope_paths),
        )

    approvers = [
        ApproverInfo(
            reviewer=r.reviewer,
            approved_at=r.submitted_at,
            approved_commit=r.commit_id,
            ask_to_approve_s=ask_s,
        )
        for r, (_login, ask_s) in zip(repo_fragment.reviews, spec.approvers)
    ]

    ai_test_lines = 0  # no test files in any fixture
    tests_authored_by = "none" if ai_test_lines == 0 else "unknown"

    owner, repo_name = spec.repo.split("/")
    return ChangeRecord(
        change_key=f"pr-{owner}-{repo_name}-{spec.pr_number}",
        kind="code",
        title=spec.title,
        source_url=repo_fragment.url,
        created_at=repo_fragment.created_at,
        intent=intent,
        sessions=sessions,
        join=JoinSection(
            method="sha" if has_sessions else "none",
            confidence="verified" if has_sessions else "unmatched",
            human_declared="docket:human-authored" in spec.labels,
            notes=[],
        ),
        diff=DiffSection(
            present=True,
            files=files,
            lines_added=spec.lines_added,
            attribution_by_line=attribution_by_line,
            hunk_attribution=hunk_attribution,
            totals=totals,
        ),
        review=ReviewSection(
            present=True,
            approvers=approvers,
            stale_approval=False,
            files_changed=len(spec.files),
            files_commented=sorted({p for _r, p in spec.comments}),
            comments=len(spec.comments),
        ),
        verify=VerifySection(
            present=coverage.present or coverage.ci_status != "unknown",
            ci_status=coverage.ci_status,
            coverage_by_line=coverage.files,
            tests_authored_by=tests_authored_by,
            rehearsal=None,
            provenance=coverage.provenance,
        ),
        deploy=DeploySection(present=False),
        manifest=ManifestSection(present=False),
    )


def _record_non_code() -> ChangeRecord:
    nc = build_non_code_change()
    before_model = nc.before.manifest.model if nc.before else "?"
    after_model = nc.after.manifest.model
    return ChangeRecord(
        change_key=f"mf-{nc.agent_id}-{nc.after.manifest_hash[:12]}",
        kind=nc.kind,
        title=f"{nc.agent_id}: model {before_model} → {after_model}",
        source_url=None,
        created_at=nc.detected_at,
        intent=IntentSection(present=False, source="manifest-change-ref"),
        sessions=[],
        join=JoinSection(method="none", confidence="unmatched"),
        diff=DiffSection(present=False),
        review=ReviewSection(present=False),
        verify=VerifySection(
            present=nc.rehearsal is not None,
            ci_status="unknown",
            rehearsal=nc.rehearsal,
            provenance=nc.rehearsal.provenance if nc.rehearsal else "real",
        ),
        deploy=DeploySection(present=False),
        manifest=ManifestSection(
            present=True,
            agent_id=nc.agent_id,
            changed_keys=list(nc.changed_keys),
            before=nc.before,
            after=nc.after,
            previous_available=nc.before is not None,
        ),
    )


# ---------------------------------------------------------------------------
# writing seeds
# ---------------------------------------------------------------------------

def _dump(obj, path: Path, simulated: bool) -> None:
    data = json.loads(obj.model_dump_json())
    if simulated and isinstance(data, dict) and "provenance" in data:
        data["provenance"] = "simulated"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_seeds(out_dir: Path, simulated: bool) -> list[str]:
    written: list[str] = []
    for name in NAMES:
        target = out_dir / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        parts = fragments(name)
        if "non_code_change" in parts:
            _dump(parts["non_code_change"], target / "non_code_change.json", simulated)
        else:
            _dump(parts["repo_fragment"], target / "repo_fragment.json", simulated)
            _dump(parts["coverage"], target / "coverage.json", simulated)
            for sf in parts["sessions"]:
                _dump(sf, target / "sessions" / f"{sf.session_id}.json", simulated)
        written.append(name)
    return written


def main() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fixtures = write_seeds(FIXTURE_DIR, simulated=False)
    demo = write_seeds(repo_root / "demo" / "seed", simulated=True)
    print(f"fixtures: {', '.join(fixtures)} -> {FIXTURE_DIR}")
    print(f"demo seeds (simulated): {', '.join(demo)} -> {repo_root / 'demo' / 'seed'}")


if __name__ == "__main__":
    main()
