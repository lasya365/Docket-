"""Line Attributor: match what the agent wrote against what survived (RFC 8.2).

Docket never tries to detect AI code by how it looks. Attribution is text
matching, nothing more. No I/O: everything arrives as arguments.

Assumptions written down as RFC section 0 rule 2 asks:
  * RFC 8.2 step 5 says files with `patch_present = false` are "recorded in a
    note" but does not name the note's home. The simplest reading: the note goes
    into `AttributionResult.notes`, which the Record Builder folds into
    `join.notes`. Such a file gets no key in `attribution_by_line` (it has no
    lines to label).
  * "the pool for this path has at most max_fuzzy_lines_per_file entries" is read
    as the entries still unmatched at that moment.
  * `HunkAttribution.session_id` is the session credited with the most lines in
    the hunk, mirroring the rule the RFC gives for `prompt_id`.
  * RFC 8.2 step 6 ("when no edits were captured") is read as: at least one
    non-ghost session joined, and *none* of them has `detail.edits`.
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import defaultdict, deque

from docket.models.common import Attribution
from docket.models.change_record import HunkAttribution
from docket.models.config import AttributorConfig
from docket.models.fragments import EditEvent, RepoFragment, SessionFragment
from docket.correlate.types import AttributionResult, DiscardInfo, JoinResult

log = logging.getLogger("docket.correlate.line_attributor")

_WS = re.compile(r"\s+")

#: A line made only of these characters carries no meaning (RFC 8.2).
_PUNCTUATION_ONLY = set("{}()[];,:")

#: Exact normalised forms that are structural, not authored content (RFC 8.2).
_TRIVIAL_FORMS = {
    "else:", "pass", "return", "break", "continue",
    "try:", "finally:", '"""', "'''", "end",
}

_LABELS: tuple[Attribution, ...] = ("ai", "human", "mixed", "unknown")


def norm(s: str) -> str:
    """Strip, then collapse every run of whitespace to one space (RFC 8.2)."""
    return _WS.sub(" ", s.strip())


def is_trivial(s: str, cfg: AttributorConfig) -> bool:
    n = norm(s)
    if len(n) < cfg.min_line_length:
        return True
    if n in _TRIVIAL_FORMS:
        return True
    return all(ch in _PUNCTUATION_ONLY or ch.isspace() for ch in n)


def posix_sep(path: str) -> str:
    """Windows agents report `C:\\repo\\src\\app.py`; a PR reports `src/app.py`.

    Both sides are compared in POSIX form so an edit made on Windows still matches the
    file it changed. Without this every added line on a Windows machine is labelled
    `unknown`, and the join can never reach `verified`. A backslash inside a genuine
    POSIX filename is treated as a separator here, which is the rarer case by far.
    """
    return path.replace("\\", "/")


def match_path(file_path: str, pr_paths: list[str]) -> str | None:
    """RFC 8.2: an EditEvent path belongs to PR file `p` when it equals `p` or ends
    with "/" + p. If several match, the longest wins. Separators are normalised first."""
    candidate = posix_sep(file_path)
    best: str | None = None
    for p in pr_paths:
        target = posix_sep(p)
        if candidate == target or candidate.endswith("/" + target):
            if best is None or len(p) > len(best):
                best = p
    return best


def _repo_relative(file_path: str, pr_paths: list[str], repo_root: str | None) -> str:
    """The path as the PR would name it, for reporting written/discarded files."""
    hit = match_path(file_path, pr_paths)
    if hit is not None:
        return hit
    if repo_root:
        root = posix_sep(repo_root).rstrip("/")
        posix_path = posix_sep(file_path)
        if posix_path.startswith(root + "/"):
            return posix_path[len(root) + 1 :]
    return file_path


class _Pool:
    """A multiset of (normalised text, session, prompt) the agent wrote for one file."""

    __slots__ = ("entries", "alive", "_by_text", "_remaining")

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str | None]] = []
        self.alive: list[bool] = []
        self._by_text: dict[str, deque[int]] = defaultdict(deque)
        self._remaining = 0

    def add(self, text: str, session_id: str, prompt_id: str | None) -> None:
        self._by_text[text].append(len(self.entries))
        self.entries.append((text, session_id, prompt_id))
        self.alive.append(True)
        self._remaining += 1

    @property
    def remaining(self) -> int:
        return self._remaining

    def take_exact(self, text: str) -> tuple[str, str | None] | None:
        queue = self._by_text.get(text)
        while queue:
            idx = queue.popleft()
            if self.alive[idx]:
                self.alive[idx] = False
                self._remaining -= 1
                _t, sid, pid = self.entries[idx]
                return sid, pid
        return None

    def take_best(self, text: str, floor: float) -> tuple[str, str | None] | None:
        best_idx = -1
        best_ratio = 0.0
        for idx, ok in enumerate(self.alive):
            if not ok:
                continue
            ratio = difflib.SequenceMatcher(None, text, self.entries[idx][0]).ratio()
            if ratio > best_ratio:
                best_ratio, best_idx = ratio, idx
        if best_idx < 0 or best_ratio < floor:
            return None
        self.alive[best_idx] = False
        self._remaining -= 1
        _t, sid, pid = self.entries[best_idx]
        return sid, pid

    def leftovers_by_session(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for idx, ok in enumerate(self.alive):
            if ok:
                out[self.entries[idx][1]] += 1
        return dict(out)


def _hunk_label(counts: dict[Attribution, int], non_trivial: int) -> Attribution:
    """RFC 8.2 step 7."""
    if non_trivial <= 0:
        return "unknown"
    if counts.get("unknown", 0) == non_trivial:
        return "unknown"
    if counts.get("ai", 0) >= 0.8 * non_trivial:
        return "ai"
    if counts.get("human", 0) >= 0.8 * non_trivial:
        return "human"
    return "mixed"


def attribute(
    repo_fragment: RepoFragment,
    joins: list[JoinResult],
    cfg: AttributorConfig,
) -> AttributionResult:
    pr_paths = [f.path for f in repo_fragment.files]
    sessions: list[SessionFragment] = [j.session for j in joins if j.is_join and j.session is not None]

    notes: list[str] = []
    missing_patch = [f.path for f in repo_fragment.files if not f.patch_present]
    if missing_patch:
        notes.append(
            "no patch available for " + ", ".join(sorted(missing_patch)) + "; those lines are not labelled"
        )

    # RFC 8.2 steps 1 and 6: nothing to match against, so nothing may be called human.
    edits_captured = any(s.detail.edits for s in sessions)
    if not sessions or not edits_captured:
        if sessions and not edits_captured:
            notes.append("no edits were captured for the joined sessions; every added line is unknown")
        return _label_everything_unknown(repo_fragment, notes)

    # --- step 2: the pools ------------------------------------------------
    pools: dict[str, _Pool] = {p: _Pool() for p in pr_paths}
    files_written: dict[str, list[str]] = {}
    wrote_path: dict[str, set[str]] = {}    # session -> PR paths it wrote into

    pairs: list[tuple[EditEvent, SessionFragment]] = [
        (e, s) for s in sessions for e in s.edit_events
    ]
    pairs.sort(key=lambda pair: (pair[0].timestamp, pair[0].tool_use_id))

    for event, session in pairs:
        sid = session.session_id
        shown = _repo_relative(event.file_path, pr_paths, session.repo_root)
        written = files_written.setdefault(sid, [])
        if shown not in written:
            written.append(shown)
        target = match_path(event.file_path, pr_paths)
        if target is None:
            log.debug("edit to %s is not part of PR #%s", event.file_path, repo_fragment.pr_number)
            continue
        wrote_path.setdefault(sid, set()).add(target)
        for raw in event.written_lines:
            if is_trivial(raw, cfg):
                continue
            pools[target].add(norm(raw), sid, event.prompt_id)

    # --- steps 3, 4, 7: walk the diff ------------------------------------
    attribution_by_line: dict[str, dict[int, Attribution]] = {}
    hunk_attribution: list[HunkAttribution] = []
    totals: dict[Attribution, int] = {label: 0 for label in _LABELS}
    matched_lines: dict[str, int] = {s.session_id: 0 for s in sessions}
    matched_in_path: dict[str, set[str]] = {}   # session -> PR paths with >= 1 matched line

    for changed in repo_fragment.files:
        if not changed.patch_present:
            continue
        path = changed.path
        per_line: dict[int, Attribution] = {}
        attribution_by_line[path] = per_line
        pool = pools[path]

        for hunk in changed.hunks:
            labels: list[Attribution | None] = []
            credits: list[tuple[str, str | None] | None] = []

            for line in hunk.added:
                if is_trivial(line.text, cfg):
                    labels.append(None)          # filled in by step 4
                    credits.append(None)
                    continue
                text = norm(line.text)
                hit = pool.take_exact(text)
                if hit is not None:
                    labels.append("ai")
                    credits.append(hit)
                elif pool.remaining <= cfg.max_fuzzy_lines_per_file and (
                    fuzzy := pool.take_best(text, cfg.mixed_similarity)
                ) is not None:
                    labels.append("mixed")
                    credits.append(fuzzy)
                else:
                    labels.append("human")
                    credits.append(None)

            for sid, _pid in (c for c in credits if c is not None):
                matched_lines[sid] = matched_lines.get(sid, 0) + 1
                matched_in_path.setdefault(sid, set()).add(path)

            # step 4: a trivial line takes the nearest non-trivial label in the hunk
            resolved = _inherit_trivial(labels)

            counts: dict[Attribution, int] = {}
            by_turn: dict[tuple[str, str | None], int] = defaultdict(int)
            for i, line in enumerate(hunk.added):
                per_line[line.line_no] = resolved[i]
                totals[resolved[i]] += 1
                if labels[i] is not None:
                    counts[labels[i]] = counts.get(labels[i], 0) + 1
                if credits[i] is not None:
                    by_turn[credits[i]] += 1

            non_trivial = sum(1 for lab in labels if lab is not None)
            top = max(by_turn, key=lambda k: by_turn[k]) if by_turn else None
            hunk_attribution.append(
                HunkAttribution(
                    hunk_id=hunk.hunk_id,
                    label=_hunk_label(counts, non_trivial),
                    counts=counts,
                    session_id=top[0] if top else None,
                    prompt_id=top[1] if top else None,
                )
            )

    # --- step 8: discarded work ------------------------------------------
    discarded: dict[str, DiscardInfo] = {}
    leftover: dict[str, int] = defaultdict(int)
    for pool in pools.values():
        for sid, n in pool.leftovers_by_session().items():
            leftover[sid] += n
    for sid in {s.session_id for s in sessions}:
        written = files_written.get(sid, [])
        matched_paths = matched_in_path.get(sid, set())
        files = [p for p in written if p not in matched_paths]
        discarded[sid] = DiscardInfo(lines=leftover.get(sid, 0), files=files)

    # --- step 9: raise confidence ----------------------------------------
    # done by the caller via `raise_confidence`, which mutates the joins in place.

    return AttributionResult(
        attribution_by_line=attribution_by_line,
        hunk_attribution=hunk_attribution,
        totals=totals,
        matched_lines_by_session=matched_lines,
        discarded_by_session=discarded,
        files_written_by_session=files_written,
        notes=notes,
    )


def _inherit_trivial(labels: list[Attribution | None]) -> list[Attribution]:
    """RFC 8.2 step 4: previous non-trivial label first, then the next one."""
    out: list[Attribution] = []
    for i, label in enumerate(labels):
        if label is not None:
            out.append(label)
            continue
        found: Attribution = "unknown"
        for j in range(i - 1, -1, -1):
            if labels[j] is not None:
                found = labels[j]
                break
        else:
            for j in range(i + 1, len(labels)):
                if labels[j] is not None:
                    found = labels[j]
                    break
        out.append(found)
    return out


def _label_everything_unknown(repo_fragment: RepoFragment, notes: list[str]) -> AttributionResult:
    attribution_by_line: dict[str, dict[int, Attribution]] = {}
    hunk_attribution: list[HunkAttribution] = []
    totals: dict[Attribution, int] = {label: 0 for label in _LABELS}
    for changed in repo_fragment.files:
        if not changed.patch_present:
            continue
        per_line: dict[int, Attribution] = {}
        attribution_by_line[changed.path] = per_line
        for hunk in changed.hunks:
            for line in hunk.added:
                per_line[line.line_no] = "unknown"
                totals["unknown"] += 1
            hunk_attribution.append(
                HunkAttribution(
                    hunk_id=hunk.hunk_id,
                    label="unknown",
                    counts={"unknown": len(hunk.added)} if hunk.added else {},
                )
            )
    return AttributionResult(
        attribution_by_line=attribution_by_line,
        hunk_attribution=hunk_attribution,
        totals=totals,
        notes=notes,
    )


def raise_confidence(joins: list[JoinResult], attribution: AttributionResult, min_lines: int) -> None:
    """RFC 8.2 step 9. Mutates `joins` in place: a join whose session's own edits were
    found inside the diff is `verified`, whatever method found it."""
    for join in joins:
        if not join.is_join or join.session is None:
            continue
        if attribution.matched_lines_by_session.get(join.session_id, 0) >= min_lines:
            join.confidence = "verified"
