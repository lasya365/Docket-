"""Outputs of plane 3 (RFC section 6.5)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from docket.models.common import (
    DecisionValue,
    EvidenceSource,
    LinkName,
    Provenance,
    RiskLevel,
    SignalName,
    SignalStatus,
)


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str                             # "ev:{signal}:{n}", unique within a run
    kind: str                           # file | tool_call | review | coverage | manifest | note
    text: str                           # one plain sentence a board member can read
    data: dict = Field(default_factory=dict)
    source: EvidenceSource
    provenance: Provenance


class SignalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal: SignalName
    label: str                          # display name, varies by kind (RFC section 9)
    score: float                        # 0 to 100, higher is riskier, 2 decimals
    status: SignalStatus
    headline: str                       # short value for the card, e.g. "340 / 604"
    summary: str
    evidence: list[EvidenceItem] = Field(default_factory=list)


class HardStopHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    reason: str


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: DecisionValue
    composite: float
    threshold: float
    risk_level: RiskLevel
    weights_used: dict[SignalName, float] = Field(default_factory=dict)
    excluded_signals: list[SignalName] = Field(default_factory=list)
    hard_stops_fired: list[HardStopHit] = Field(default_factory=list)
    unmatched_policy_applied: str = "n/a"   # penalize | neutral | n/a
    config_hash: str = ""


class ChainLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: LinkName
    present: bool
    summary: str
    detail: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class EvidenceChain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    links: list[ChainLink] = Field(default_factory=list)   # always six, always in order
    missing_count: int = 0


class BackoutStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str                            # "model", "prompt", "tools", "knowledge", "code"
    from_value: str
    to_value: str


class BackoutPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    summary: str
    steps: list[BackoutStep] = Field(default_factory=list)
    naive_plan: str | None = None
    why_naive_fails: str | None = None
    text: str
