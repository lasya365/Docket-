"""The Agent Manifest and the no-code change (RFC section 6.3)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from docket.models.common import Kind, Provenance


class PromptRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    sha256: str = ""


class KnowledgeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    revision: str


class AgentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    model: str
    prompt: PromptRef
    tools: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    knowledge: list[KnowledgeRef] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)   # where this agent runs; feeds blast radius
    change_ref: str | None = None                        # ticket or issue that asked for this change


class ManifestSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    manifest_hash: str                  # sha256 of the canonical manifest JSON
    manifest: AgentManifest
    observed_at: datetime
    sequence: int                       # 1, 2, 3 ... per agent_id


class Regression(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    before: str                         # the decision the old bundle made
    after: str                          # the decision the new bundle made


class RehearsalFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    manifest_hash: str                  # the "after" bundle these results belong to
    cases: int
    identical: int
    changed_acceptable: int
    regressed: int
    regressions: list[Regression] = Field(default_factory=list)
    provenance: Provenance             # "simulated" for the stored 50-case corpus


class NonCodeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    kind: Kind                          # never "code"
    changed_keys: list[str] = Field(default_factory=list)
    before: ManifestSnapshot | None = None   # None the first time an agent is seen
    after: ManifestSnapshot
    rehearsal: RehearsalFragment | None = None
    detected_at: datetime
    provenance: Provenance = "real"
