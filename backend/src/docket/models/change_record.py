"""The ChangeRecord: the only object plane 3 reads (RFC section 6.4)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from docket.models.common import (
    Attribution,
    JoinConfidence,
    JoinMethod,
    Kind,
    Provenance,
)
from docket.models.fragments import ChangedFile, DetailLevel, FileCoverage, ToolCall
from docket.models.manifest import ManifestSnapshot, RehearsalFragment


class IntentSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool
    source: str | None = None           # "github-issue" | "manifest-change-ref"
    ref: str | None = None              # "#114" or a ticket id
    url: str | None = None
    text: str | None = None
    scope_paths: list[str] = Field(default_factory=list)
    provenance: Provenance = "real"


class SessionSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool                       # False for a ghost session (RFC 8.1)
    session_id: str
    agent: str | None = None
    model: str | None = None
    turns: int = 0
    retries: int = 0
    files_written: list[str] = Field(default_factory=list)
    files_discarded: list[str] = Field(default_factory=list)
    lines_written: int = 0
    lines_discarded: int = 0
    tools_used: list[ToolCall] = Field(default_factory=list)
    permission_modes: list[str] = Field(default_factory=list)
    approvals_by: dict[str, int] = Field(default_factory=dict)
    sandbox_disabled_calls: int = 0
    prompt_excerpts: list[str] = Field(default_factory=list)
    detail: DetailLevel | None = None
    join_method: JoinMethod = "none"
    join_confidence: JoinConfidence = "unmatched"
    matched_commits: list[str] = Field(default_factory=list)
    provenance: Provenance = "real"


class JoinSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: JoinMethod = "none"         # best method among the sessions
    confidence: JoinConfidence = "unmatched"
    human_declared: bool = False        # the PR carries the human-declared label
    notes: list[str] = Field(default_factory=list)


class HunkAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hunk_id: str
    label: Attribution
    counts: dict[Attribution, int] = Field(default_factory=dict)
    session_id: str | None = None
    prompt_id: str | None = None        # the turn that wrote most of this hunk


class DiffSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool
    files: list[ChangedFile] = Field(default_factory=list)
    lines_added: int = 0
    attribution_by_line: dict[str, dict[int, Attribution]] = Field(default_factory=dict)
    hunk_attribution: list[HunkAttribution] = Field(default_factory=list)
    totals: dict[Attribution, int] = Field(default_factory=dict)
    provenance: Provenance = "real"


class ApproverInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str
    approved_at: datetime
    approved_commit: str
    ask_to_approve_s: int               # upper bound on reading time (RFC 9.1)


class ReviewSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool                       # False when no pull request exists at all
    approvers: list[ApproverInfo] = Field(default_factory=list)
    stale_approval: bool = False
    files_changed: int = 0
    files_commented: list[str] = Field(default_factory=list)
    comments: int = 0
    provenance: Provenance = "real"


class VerifySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool
    ci_status: str = "unknown"
    coverage_by_line: dict[str, FileCoverage] = Field(default_factory=dict)
    tests_authored_by: Literal["ai", "human", "mixed", "none", "unknown"] = "unknown"
    rehearsal: RehearsalFragment | None = None      # no-code kinds only
    provenance: Provenance = "real"


class DeploySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool
    target: str | None = None
    window: str | None = None
    provenance: Provenance = "real"


class ManifestSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool
    agent_id: str | None = None
    changed_keys: list[str] = Field(default_factory=list)
    before: ManifestSnapshot | None = None
    after: ManifestSnapshot | None = None
    previous_available: bool = False


class ChangeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "3.0"
    change_key: str                     # "pr-{owner}-{repo}-{n}" | "mf-{agent_id}-{hash12}"
    kind: Kind
    title: str
    source_url: str | None = None
    created_at: datetime
    intent: IntentSection
    sessions: list[SessionSection] = Field(default_factory=list)
    join: JoinSection
    diff: DiffSection
    review: ReviewSection
    verify: VerifySection
    deploy: DeploySection
    manifest: ManifestSection
