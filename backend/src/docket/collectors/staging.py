"""`.docket.json`: evidence a repository declares that git cannot prove.

Docket scores a change from what it can verify: the diff, the commits, and the
agent's own telemetry. Some evidence a board needs lives outside git entirely -
the ticket that asked for the change, who approved it and how long they had it
open, what the tests covered. When Docket reads a GitHub pull request it gets
those from the API. When it reads a **local branch** - a laptop, an air-gapped
checkout, a Gerrit or Bitbucket change Docket has no adapter for - there is
nowhere to read them from, so the repository declares them in `.docket.json` at
its root.

Every field is optional, and a repository without the file is the normal case:
it is scored on the diff and the sessions alone, and the evidence it cannot
supply is reported `present = false` rather than guessed.

Anything this file supplies did NOT come from a real system, so the sections
built from it are labelled `provenance = "simulated"` (RFC P8, rule 7) while the
diff, the commits and the agent sessions stay `real`. That split is the point:
a reader can always see which half of the record git proved.

Assumptions taken here (RFC section 0, rule 2 - simplest reading, written down):
  * The loader is tolerant by design. No file is the normal case and returns
    None. A malformed file is logged at DEBUG and returns None too: a broken
    declaration must never stop a run that has a real diff to score.
  * Unknown keys are ignored rather than rejected, so one stray field does not
    throw away the rest of the staged evidence.
  * `url` and `repo` are accepted alongside the fields RFC-side callers use: a
    repository may want to point the change at a ticket instead of `file://`,
    and a checkout at /tmp/build-42 should still read as `acme/storefront`.
  * Coverage is written with the `FileCoverage` field names (`executed` /
    `missing`), which is what a Docket-native file naturally holds. Those are
    normalised on load to the coverage.py spelling (`executed_lines` /
    `missing_lines`) that `ci_coverage.parse_coverage_json` reads, so both
    spellings work and there is still exactly one coverage parser.
  * `review_comments` is a top-level list, mirroring `RepoFragment`. The `line`
    and `body` a comment carries are for the human reading the file; the RFC's
    `ReviewComment` has nowhere to put them, so they are ignored.
  * Timestamps are left exactly as written; the Repo Collector normalises them
    to UTC with the same helper the GitHub collector uses.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("docket.collectors.staging")

STAGING_FILENAME = ".docket.json"
# Accepted silently for repos staged before the rename. Not documented.
LEGACY_FILENAMES = ("docket-scenario.json",)


class StagedIssue(BaseModel):
    """The issue a local branch says it implements."""

    model_config = ConfigDict(extra="ignore")

    number: int = 0
    title: str = ""
    body: str = ""                      # a `Scope:` line in here is parsed by the collector
    url: str = ""


class StagedReview(BaseModel):
    """One staged review.

    `ask_to_approve_s` is the review time the repository declares; the collector
    builds timestamps that make the Record Builder reproduce it exactly (RFC 8.3).
    """

    model_config = ConfigDict(extra="ignore")

    reviewer: str = ""
    state: str = "APPROVED"
    submitted_at: datetime | None = None
    requested_at: datetime | None = None
    ask_to_approve_s: int | None = None
    commit_id: str | None = None        # defaults to head_sha, i.e. a fresh approval
    comments: list[str] = Field(default_factory=list)   # paths this reviewer commented on


class StagedReviewComment(BaseModel):
    """One staged review comment. `line` and `body` are for the reader, not Docket."""

    model_config = ConfigDict(extra="ignore")

    reviewer: str = ""
    path: str = ""
    created_at: datetime | None = None


# `FileCoverage` field name -> the coverage.py spelling `parse_coverage_json` reads.
COVERAGE_KEYS = {"executed": "executed_lines", "missing": "missing_lines"}


def normalise_coverage(block: Any) -> Any:
    """Accept a coverage block written with either spelling of the line keys."""
    if not isinstance(block, dict):
        return block
    files = block.get("files")
    if not isinstance(files, dict):
        return block
    fixed: dict[str, Any] = {}
    for path, entry in files.items():
        if not isinstance(entry, dict):
            fixed[path] = entry
            continue
        copy = dict(entry)
        for short, long in COVERAGE_KEYS.items():
            if short in copy and long not in copy:
                copy[long] = copy.pop(short)
        fixed[path] = copy
    return {**block, "files": fixed}


class StagedEvidence(BaseModel):
    """The whole staged block. Every field is optional."""

    model_config = ConfigDict(extra="ignore")

    pr_number: int = 0
    title: str | None = None
    url: str | None = None
    repo: str | None = None                     # "owner/name" when the checkout is not named after it
    issue: StagedIssue | None = None
    reviews: list[StagedReview] = Field(default_factory=list)
    review_comments: list[StagedReviewComment] = Field(default_factory=list)
    coverage: dict[str, Any] | None = None      # coverage.py JSON, optionally with "ci_status"
    deploy: dict[str, Any] | None = None        # reserved; the record's deploy section is config-driven

    @field_validator("coverage", mode="before")
    @classmethod
    def _normalise_coverage(cls, value: Any) -> Any:
        return normalise_coverage(value)

    # ---- views ---------------------------------------------------------- #

    def staged(self) -> dict[str, Any]:
        """The parts the Repo Collector understands, as a plain dict.

        Only the keys that were actually declared appear, so an empty dict
        means "nothing was staged" and the fragment stays `provenance = "real"`.
        """
        out: dict[str, Any] = {}
        if self.pr_number:
            out["pr_number"] = int(self.pr_number)
        if self.title:
            out["title"] = self.title
        if self.url:
            out["url"] = self.url
        if self.repo:
            out["repo"] = self.repo
        if self.issue is not None:
            out["issue"] = self.issue.model_dump(mode="json")
        if self.reviews:
            out["reviews"] = [r.model_dump(mode="json") for r in self.reviews]
        if self.review_comments:
            out["review_comments"] = [c.model_dump(mode="json") for c in self.review_comments]
        return out


def staging_path(repo_path: str | Path) -> Path:
    """Where the staging file lives, whether or not it exists."""
    return Path(repo_path) / STAGING_FILENAME


def _existing_staging_file(repo_path: str | Path) -> Path | None:
    root = Path(repo_path)
    for name in (STAGING_FILENAME, *LEGACY_FILENAMES):
        candidate = root / name
        if candidate.is_file():
            if name != STAGING_FILENAME:
                log.debug("using legacy staging file %s; rename it to %s", name, STAGING_FILENAME)
            return candidate
    return None


def load_staged_evidence(repo_path: str | Path) -> StagedEvidence | None:
    """Read the staging file from a repo root. Missing or broken gives None."""
    path = _existing_staging_file(repo_path)
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.debug("unreadable %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        log.debug("%s is not a JSON object, ignoring it", path)
        return None
    try:
        return StagedEvidence.model_validate(raw)
    except Exception as exc:                    # pydantic ValidationError and anything odder
        log.debug("malformed %s: %s", path, exc)
        return None
