"""Shared literal vocabularies (RFC section 6.1).

`Attribution` uses the vocabulary of the Agent Trace draft standard so Docket's
output can later be exchanged with other tools.
"""

from typing import Literal

Provenance = Literal["real", "simulated"]
Kind = Literal["code", "model_version", "prompt", "knowledge", "permission"]
Attribution = Literal["ai", "human", "mixed", "unknown"]
JoinMethod = Literal["sha", "trailer", "time", "none"]
JoinConfidence = Literal["verified", "claimed", "inferred", "unmatched"]
SignalName = Literal["unattributed", "review_depth", "untested", "blast_radius"]
SignalStatus = Literal["computed", "degraded", "not_computable"]
DecisionValue = Literal["APPROVE", "HOLD"]
RiskLevel = Literal["low", "medium", "high", "very_high"]
LinkName = Literal["intent", "session", "diff", "review", "verify", "deploy"]
EvidenceSource = Literal["github", "session", "hook", "ci", "manifest", "config"]

SIGNAL_ORDER: tuple[str, ...] = ("unattributed", "review_depth", "untested", "blast_radius")
CHAIN_ORDER: tuple[str, ...] = ("intent", "session", "diff", "review", "verify", "deploy")
JOIN_CONFIDENCE_RANK: dict[str, int] = {
    "unmatched": 0,
    "inferred": 1,
    "claimed": 2,
    "verified": 3,
}
