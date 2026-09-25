"""Outputs of plane 1 (RFC section 6.2). Vendor shapes die before these objects."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from docket.models.common import Provenance


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_id: str                      # OTel prompt.id == hook prompt_id
    started_at: datetime
    prompt_text: str | None = None      # None when the agent redacted it
    prompt_length: int = 0
    tool_use_ids: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_use_id: str
    prompt_id: str | None = None
    name: str                           # raw tool_name: "Edit", "Bash", "mcp_tool", ...
    display_name: str                   # "Edit", "Bash", or "server.tool" for MCP
    mcp_server: str | None = None
    mcp_tool: str | None = None
    bash_command: str | None = None
    file_path: str | None = None
    success: bool
    duration_ms: int | None = None
    decision_source: Literal["config", "hook", "user_permanent", "user_temporary"] | None = None
    sandbox_disabled: bool = False
    git_commit_sha: str | None = None   # set when this call was a successful git commit
    git_branch: str | None = None
    timestamp: datetime


class ToolRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_use_id: str
    name: str
    source: str                         # user_reject | user_abort | config | hook
    timestamp: datetime


class PermissionModeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_mode: str
    to_mode: str
    trigger: str | None = None
    timestamp: datetime


class EditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    tool_use_id: str
    prompt_id: str | None = None
    tool_name: Literal["Edit", "Write", "MultiEdit", "NotebookEdit"]
    file_path: str                      # as received; normalised by the attributor
    written_lines: list[str] = Field(default_factory=list)
    removed_lines: list[str] = Field(default_factory=list)
    agent_id: str | None = None         # set when a subagent made the edit
    timestamp: datetime


class DetailLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompts: bool = False               # prompt text arrived
    tool_details: bool = False          # tool_parameters arrived
    edits: bool = False                 # at least one EditEvent arrived


class SessionFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    agent: str = "claude-code"
    agent_version: str | None = None
    models: list[str] = Field(default_factory=list)
    user_email: str | None = None
    repo_url: str | None = None         # OTel vcs.repository.url.full
    repo_root: str | None = None        # from the SessionStart hook
    started_at: datetime
    ended_at: datetime                  # timestamp of the last event seen
    turns: list[Turn] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    rejections: list[ToolRejection] = Field(default_factory=list)
    permission_mode_changes: list[PermissionModeChange] = Field(default_factory=list)
    api_retries: int = 0
    edit_events: list[EditEvent] = Field(default_factory=list)
    detail: DetailLevel
    provenance: Provenance = "real"


class DiffLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: int                        # line number in the NEW file
    text: str


class Hunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hunk_id: str                        # "{path}#{index}"
    path: str
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    added: list[DiffLine] = Field(default_factory=list)
    removed_count: int = 0


class ChangedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    status: str                         # added | modified | removed | renamed
    additions: int = 0
    deletions: int = 0
    patch_present: bool = True          # GitHub omits the patch for very large files
    hunks: list[Hunk] = Field(default_factory=list)


class CommitRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha: str
    message: str
    author_login: str | None = None
    author_email: str | None = None
    committed_at: datetime
    trailers: dict[str, list[str]] = Field(default_factory=dict)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str
    state: str                          # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED
    submitted_at: datetime
    commit_id: str                      # the commit the review was made against


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str
    requested_at: datetime


class ReviewComment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str
    path: str
    created_at: datetime


class LinkedIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    title: str
    body: str
    url: str
    scope_paths: list[str] = Field(default_factory=list)   # parsed from a "Scope:" line


class RepoFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str                           # "owner/name"
    pr_number: int
    title: str
    body: str = ""
    url: str
    author_login: str
    labels: list[str] = Field(default_factory=list)
    base_ref: str
    head_ref: str
    head_sha: str
    created_at: datetime
    merged: bool = False
    commits: list[CommitRef] = Field(default_factory=list)
    files: list[ChangedFile] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    review_requests: list[ReviewRequest] = Field(default_factory=list)
    review_comments: list[ReviewComment] = Field(default_factory=list)
    linked_issue: LinkedIssue | None = None
    provenance: Provenance = "real"


class FileCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executed: list[int] = Field(default_factory=list)
    missing: list[int] = Field(default_factory=list)


class CoverageFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    head_sha: str
    ci_status: Literal["success", "failure", "pending", "unknown"] = "unknown"
    files: dict[str, FileCoverage] = Field(default_factory=dict)
    present: bool = False               # False when no coverage could be found
    source: Literal["github-artifact", "posted", "simulated", "none"] = "none"
    provenance: Provenance = "real"
