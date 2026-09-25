"""One run across the four planes (RFC sections 5.2 and 10.1).

This is the only module that imports from every plane. `Deps` carries the
injected clients so every step can be faked in a test.

Order is RFC 5.2, exactly:
    collect -> correlate -> decide -> seal -> brief -> store -> freshservice -> github status
The seal covers everything decided before it; the brief is written after it and
is never read by decide/ (P10).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from docket.collectors.adapters.claude_code import build_session
from docket.collectors.ci_coverage import added_lines_of, parse_coverage_json, simulate_coverage
from docket.collectors.local_git import LocalGitCollector, staged_section_provenance
from docket.collectors.staging import StagedEvidence, load_staged_evidence
from docket.correlate.correlator import correlate
from docket.correlate.line_attributor import attribute
from docket.correlate.record_builder import build_code, build_non_code
from docket.decide.backout_planner import plan_backout, rollout_text
from docket.decide.engine import run_signals
from docket.decide.evidence_chain import build_chain
from docket.decide.gate import decide
from docket.models.change_record import ChangeRecord
from docket.models.fragments import CoverageFragment, RepoFragment, SessionFragment
from docket.models.manifest import NonCodeChange
from docket.models.run import Run, RunOutputs
from docket.record.seal import seal as seal_run
from docket.settings import Settings

log = logging.getLogger("docket.pipeline")

CANDIDATE_WINDOW_DAYS = 7


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Deps:
    """Injected clients. Tests fake all of them (RFC 10.1)."""

    settings: Settings
    store: Any
    github: Any = None          # collectors.github.GithubCollector
    local_git: Any = None       # collectors.local_git.LocalGitCollector
    coverage: Any = None        # collectors.ci_coverage.CoverageCollector
    freshservice: Any = None    # record.freshservice.FreshserviceWriter
    status: Any = None          # record.github_status.GithubStatusWriter
    brief: Any = None           # record.brief_claude.BoardBrief
    clock: Callable[[], datetime] = utcnow
    mode: str = "live"
    writes_enabled: bool = True
    errors: list[str] = field(default_factory=list)


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def change_key_for_pr(repo: str, pr_number: int) -> str:
    """pr-{owner}-{repo}-{n}, lowercase, non-alphanumerics replaced by - (RFC 8.3)."""
    owner, _, name = repo.partition("/")
    return re.sub(r"[^a-z0-9]+", "-", f"pr-{owner}-{name}-{pr_number}".lower()).strip("-")


def change_key_for_local(repo: str, head_sha: str) -> str:
    """local-{repo-slug}-{head_sha[:12]}, same normalisation as the PR key.

    A local branch has no PR number, so the head commit identifies the change.
    Re-running the same branch without committing updates the same change; a new
    commit is a new change, which is what a new push would be.
    """
    return re.sub(r"[^a-z0-9]+", "-", f"local-{repo}-{head_sha[:12]}".lower()).strip("-")


# ---------------------------------------------------------------------------
# session assembly: a SessionFragment is built from stored events at run time
# ---------------------------------------------------------------------------

def assemble_session(store: Any, session_id: str) -> SessionFragment | None:
    events, edit_events, meta = store.load_session_parts(session_id)
    try:
        return build_session(session_id, events, edit_events, meta)
    except Exception as exc:                                  # never crash a run on one session
        log.warning("could not assemble session %s: %s", session_id, exc)
        return None


def candidate_sessions(store: Any, repo_fragment: RepoFragment) -> list[SessionFragment]:
    times = [c.committed_at for c in repo_fragment.commits] or [repo_fragment.created_at]
    start = min(times) - timedelta(days=CANDIDATE_WINDOW_DAYS)
    end = max(times) + timedelta(days=CANDIDATE_WINDOW_DAYS)
    sessions: list[SessionFragment] = []
    for row in store.sessions_between(start, end):
        session_id = row if isinstance(row, str) else row["session_id"]
        fragment = assemble_session(store, session_id)
        if fragment is not None:
            sessions.append(fragment)
    return sessions


# ---------------------------------------------------------------------------
# planes 3 and 4, shared by both entry points
# ---------------------------------------------------------------------------

def _decide_and_record(record: ChangeRecord, deps: Deps, run_id: str) -> Run:
    cfg = deps.settings.config
    deps.store.set_status(run_id, "deciding", "scoring the evidence")

    threshold_override = deps.store.get_override("threshold")
    gate_cfg = deps.settings.gate_config(threshold_override)

    signals = run_signals(record, cfg.signals, cfg.sensitive)
    decision = decide(record, signals, gate_cfg)
    chain = build_chain(record, signals)
    backout = plan_backout(record)

    run = Run(
        run_id=run_id,
        change_key=record.change_key,
        mode="replay" if deps.mode == "replay" else "live",
        created_at=deps.clock(),
        record=record,
        signals=signals,
        decision=decision,
        chain=chain,
        backout=backout,
        rollout_text=rollout_text(record),
        seal="",
        advisory=[],
        outputs=RunOutputs(errors=list(deps.errors)),
    )
    run.seal = seal_run(run)

    deps.store.set_status(run_id, "recording", "filing the decision")

    # Advisory, written after the seal and never read by decide/ (P10).
    if deps.brief is not None and cfg.brief.enabled:
        try:
            deps.brief.write(run)
        except Exception as exc:                              # a writer never fails the run
            log.warning("board brief failed: %s", exc)
            run.outputs.errors.append(f"brief: {exc}")

    deps.store.save_run(run)

    if deps.writes_enabled:
        for writer, label in ((deps.freshservice, "freshservice"), (deps.status, "github_status")):
            if writer is None:
                continue
            try:
                writer.write(run)
            except Exception as exc:                          # RFC 10: a writer that fails records the error
                log.warning("%s writer failed: %s", label, exc)
                run.outputs.errors.append(f"{label}: {exc}")
        deps.store.save_run(run)

    deps.store.set_status(run_id, "done", run.decision.decision)
    return run


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def run_code_change(repo: str, pr_number: int, deps: Deps, run_id: str | None = None) -> Run:
    run_id = run_id or new_run_id()
    cfg = deps.settings.config
    deps.store.set_status(run_id, "collecting", f"reading {repo}#{pr_number}")

    repo_fragment = deps.github.collect(repo, pr_number)
    coverage = _collect_coverage(repo, repo_fragment, deps)
    return run_from_fragments(repo_fragment, coverage, candidate_sessions(deps.store, repo_fragment), deps, run_id)


def _collect_coverage(repo: str, repo_fragment: RepoFragment, deps: Deps) -> CoverageFragment:
    if deps.coverage is None:
        return CoverageFragment(head_sha=repo_fragment.head_sha, present=False, source="none")
    added_lines = {
        f.path: [line.line_no for h in f.hunks for line in h.added] for f in repo_fragment.files
    }
    posted = deps.store.posted_coverage(repo_fragment.head_sha)
    try:
        return deps.coverage.collect(
            repo,
            repo_fragment.head_sha,
            posted=posted,
            simulate=deps.settings.simulate_coverage,
            added_lines=added_lines,
        )
    except Exception as exc:                                  # a collector that finds nothing is not an error
        log.warning("coverage collection failed: %s", exc)
        deps.errors.append(f"coverage: {exc}")
        return CoverageFragment(head_sha=repo_fragment.head_sha, present=False, source="none")


def run_from_fragments(
    repo_fragment: RepoFragment,
    coverage: CoverageFragment,
    sessions: list[SessionFragment],
    deps: Deps,
    run_id: str | None = None,
    change_key: str | None = None,
    section_provenance: dict[str, str] | None = None,
) -> Run:
    """Planes 2 to 4 over plane-1 objects. Live runs and replayed seeds share this path.

    `change_key` overrides the key the Record Builder derives from the fragment.
    Only the local-git entry point uses it: RFC 8.3 keys a code change by its PR
    number, and a local branch has none (see `change_key_for_local`).

    `section_provenance` labels the sections whose evidence was staged, so a run
    that has a real diff and a staged reviewer says exactly that (RFC P8).
    """
    run_id = run_id or new_run_id()
    cfg = deps.settings.config

    deps.store.set_status(run_id, "correlating", "matching sessions to commits")
    joins = correlate(repo_fragment, sessions, cfg.correlator)
    attribution = attribute(repo_fragment, joins, cfg.attributor)
    record = build_code(repo_fragment, joins, attribution, coverage, cfg, section_provenance)
    if change_key:
        record.change_key = change_key
    return _decide_and_record(record, deps, run_id)


# ---------------------------------------------------------------------------
# local git: the same four planes over a branch on a laptop, with no PR
# ---------------------------------------------------------------------------

def run_local_change(
    repo_path: str | Path,
    base: str,
    head: str,
    deps: Deps,
    run_id: str | None = None,
) -> Run:
    """A local branch -> a scored Run. Only plane 1's source differs (RFC 7.3)."""
    run_id = run_id or new_run_id()
    deps.store.set_status(run_id, "collecting", f"reading {repo_path} {base}..{head}")

    spec = load_staged_evidence(repo_path)
    staged = spec.staged() if spec is not None else None
    collector = deps.local_git or LocalGitCollector()
    repo_fragment = collector.collect(repo_path, base, head, staged=staged)

    coverage = _local_coverage(repo_fragment, spec, deps)
    return run_from_fragments(
        repo_fragment,
        coverage,
        candidate_sessions(deps.store, repo_fragment),
        deps,
        run_id,
        change_key=change_key_for_local(repo_fragment.repo, repo_fragment.head_sha),
        # The diff is real git; only the sections the staging file propped up are not.
        section_provenance=staged_section_provenance(staged) or None,
    )


def _local_coverage(
    repo_fragment: RepoFragment, spec: StagedEvidence | None, deps: Deps
) -> CoverageFragment:
    """Staged coverage, then the simulated fallback, then nothing (RFC 7.4 step 4).

    There is no CI to ask, so neither branch here is ever `provenance = "real"`:
    a staging file is declared evidence and the fallback is generated. A run with
    neither says so with `present = false` instead of guessing (RFC 0 rule 6).
    """
    head_sha = repo_fragment.head_sha

    if spec is not None and spec.coverage:
        files = parse_coverage_json(spec.coverage)
        if files:
            status = str(spec.coverage.get("ci_status") or "unknown").lower()
            if status not in ("success", "failure", "pending", "unknown"):
                log.debug("unknown staged ci_status %r", status)
                status = "unknown"
            return CoverageFragment(
                head_sha=head_sha,
                ci_status=status,
                files=files,
                present=True,
                source="simulated",
                provenance="simulated",
            )
        log.debug("staged coverage held no readable files")

    if deps.settings.simulate_coverage:
        files = simulate_coverage(added_lines_of(repo_fragment.files))
        if files:
            return CoverageFragment(
                head_sha=head_sha,
                ci_status="unknown",
                files=files,
                present=True,
                source="simulated",
                provenance="simulated",
            )

    return CoverageFragment(head_sha=head_sha, present=False, source="none")


def run_non_code_change(change: NonCodeChange, deps: Deps, run_id: str | None = None) -> Run:
    run_id = run_id or new_run_id()
    cfg = deps.settings.config
    deps.store.set_status(run_id, "collecting", f"manifest change on {change.agent_id}")
    record = build_non_code(change, cfg)
    run = _decide_and_record(record, deps, run_id)
    # The pipeline, not the watcher, appends the new snapshot to the ledger (RFC 7.5).
    deps.store.append_snapshot(change.after)
    return run


# ---------------------------------------------------------------------------
# replay (RFC section 14)
# ---------------------------------------------------------------------------

def load_seed(seed_dir: Path) -> dict:
    """A seed is a folder of stored plane-1 outputs."""
    seed_dir = Path(seed_dir)
    parts: dict = {}
    non_code = seed_dir / "non_code_change.json"
    if non_code.exists():
        parts["non_code_change"] = NonCodeChange.model_validate_json(non_code.read_text(encoding="utf-8"))
        return parts
    repo_file = seed_dir / "repo_fragment.json"
    if not repo_file.exists():
        raise FileNotFoundError(f"{seed_dir} holds no repo_fragment.json and no non_code_change.json")
    parts["repo_fragment"] = RepoFragment.model_validate_json(repo_file.read_text(encoding="utf-8"))
    cov_file = seed_dir / "coverage.json"
    parts["coverage"] = (
        CoverageFragment.model_validate_json(cov_file.read_text(encoding="utf-8"))
        if cov_file.exists()
        else CoverageFragment(head_sha=parts["repo_fragment"].head_sha, present=False, source="none")
    )
    sessions_dir = seed_dir / "sessions"
    parts["sessions"] = [
        SessionFragment.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(sessions_dir.glob("*.json"))
    ] if sessions_dir.is_dir() else []
    return parts


def run_seed(seed_dir: Path, deps: Deps, run_id: str | None = None) -> Run:
    parts = load_seed(seed_dir)
    replay_deps = Deps(
        settings=deps.settings,
        store=deps.store,
        github=deps.github,
        coverage=deps.coverage,
        freshservice=deps.freshservice,
        status=deps.status,
        brief=deps.brief,
        clock=deps.clock,
        mode="replay",
        # Freshservice and GitHub writers run in dry-run for replayed runs unless
        # DOCKET_REPLAY_WRITES=1 (RFC section 14).
        writes_enabled=deps.settings.replay_writes,
    )
    if "non_code_change" in parts:
        return run_non_code_change(parts["non_code_change"], replay_deps, run_id)
    return run_from_fragments(parts["repo_fragment"], parts["coverage"], parts["sessions"], replay_deps, run_id)


def seed_dirs(settings: Settings) -> list[Path]:
    root = settings.repo_root / "demo" / "seed"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())
