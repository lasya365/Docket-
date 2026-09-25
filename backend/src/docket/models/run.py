"""The Run: everything Docket knows about one pass over one change (RFC section 6.5)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from docket.models.change_record import ChangeRecord
from docket.models.signals import BackoutPlan, Decision, EvidenceChain, SignalResult


class Advisory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["llm", "template"]
    model: str | None = None
    kind: Literal["brief", "question", "condition", "note"]
    text: str
    evidence_refs: list[str] = Field(default_factory=list)


class RunOutputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freshservice_change_id: int | None = None
    freshservice_url: str | None = None
    freshservice_approval: Literal["none", "requested", "approved", "rejected", "unknown"] = "none"
    github_status_state: str | None = None
    errors: list[str] = Field(default_factory=list)


class Run(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    change_key: str
    mode: Literal["live", "replay"]
    created_at: datetime
    record: ChangeRecord
    signals: list[SignalResult] = Field(default_factory=list)   # always four
    decision: Decision
    chain: EvidenceChain
    backout: BackoutPlan
    rollout_text: str = ""
    seal: str = ""
    advisory: list[Advisory] = Field(default_factory=list)      # never read by decide/
    outputs: RunOutputs = Field(default_factory=RunOutputs)
