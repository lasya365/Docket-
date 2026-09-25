"""The Decision Gate (RFC section 9.5).

Pure: standard library, pathspec and docket.models only. No clock, no I/O, no LLM.

Assumptions written down as RFC section 0 rule 2 asks:
  * `weights_used` reports the renormalised weight of each *included* signal, which
    is what the composite actually used. Excluded signals are listed separately in
    `excluded_signals`.
  * A signal with no weight in the config contributes weight 0; if every included
    weight is 0 the composite is 0.
  * `config_hash` uses `GateConfig.config_hash` when settings.py filled it (it alone
    can see the signals block, RFC 9.5 step 7). Otherwise the gate hashes the
    canonical JSON of the gate config it was handed, so the field is never empty.
  * A hard-stop rule with an unknown `type` is ignored rather than raising: a config
    from a newer Docket must not break a decision (RFC 0 rule 6).
"""

from __future__ import annotations

import hashlib
import json

import pathspec

from docket.models.change_record import ChangeRecord
from docket.models.config import GateConfig
from docket.models.signals import Decision, HardStopHit, SignalResult

POLICY_NEUTRAL = "neutral"
POLICY_PENALIZE = "penalize"
POLICY_NA = "n/a"


def _policy(record: ChangeRecord, cfg: GateConfig) -> str:
    """Step 1."""
    if record.kind != "code":
        return POLICY_NA
    if record.join.confidence != "unmatched":
        return POLICY_NA
    return POLICY_NEUTRAL if record.join.human_declared else cfg.unmatched_policy


def _effective(signal: SignalResult, policy: str, cfg: GateConfig) -> float | None:
    """Step 2. None means the signal is excluded."""
    if signal.status == "computed":
        return float(signal.score)
    if signal.status == "degraded":
        if policy == POLICY_NEUTRAL:
            return float(signal.score)
        return max(float(signal.score), float(cfg.degraded_floor))
    # not_computable
    if policy == POLICY_NEUTRAL:
        return None
    return float(cfg.not_computable_score)


def _composite(
    effective: dict[str, float], cfg: GateConfig
) -> tuple[float, dict[str, float]]:
    """Step 3."""
    raw = {name: float(cfg.weights.get(name, 0.0)) for name in effective}
    total = sum(raw.values())
    if not effective or total <= 0:
        return 0.0, {name: 0.0 for name in effective}
    used = {name: raw[name] / total for name in effective}
    composite = sum(effective[name] * used[name] for name in effective)
    return round(composite, 2), {name: round(w, 6) for name, w in used.items()}


def _sensitive_paths_touched(record: ChangeRecord, cfg: GateConfig) -> list[str]:
    if not cfg.sensitive_paths:
        return []
    spec = pathspec.PathSpec.from_lines("gitwildmatch", list(cfg.sensitive_paths))
    return sorted({f.path for f in record.diff.files if spec.match_file(f.path)})


def _hard_stops(
    record: ChangeRecord,
    signals: list[SignalResult],
    effective: dict[str, float],
    cfg: GateConfig,
) -> list[HardStopHit]:
    """Step 4, in config order."""
    hits: list[HardStopHit] = []
    by_name = {s.signal: s for s in signals}
    for rule in cfg.hard_stops:
        if rule.type == "signal_at_or_above":
            if rule.value is None:
                continue
            names = (
                # worst first, so the reason names the signal that fired it
                sorted(effective, key=lambda n: (-effective[n], n))
                if rule.signal in (None, "*")
                else [rule.signal]
            )
            for name in names:
                if name not in effective:
                    continue
                if effective[name] >= float(rule.value):
                    label = by_name[name].label if name in by_name else name
                    hits.append(
                        HardStopHit(
                            id=rule.id,
                            reason=(
                                f"{label} scored {effective[name]:.2f}, at or above "
                                f"{float(rule.value):.0f}."
                            ),
                        )
                    )
                    break
        elif rule.type == "non_code_without_previous_manifest":
            if record.kind != "code" and not record.manifest.previous_available:
                hits.append(
                    HardStopHit(
                        id=rule.id,
                        reason=(
                            "No earlier bundle is on record for "
                            f"{record.manifest.agent_id or 'this agent'}, so there is "
                            "nothing to restore."
                        ),
                    )
                )
        elif rule.type == "unmatched_and_sensitive_path":
            if record.kind == "code" and record.join.confidence == "unmatched":
                touched = _sensitive_paths_touched(record, cfg)
                if touched:
                    hits.append(
                        HardStopHit(
                            id=rule.id,
                            reason=(
                                "No agent session was matched to this change and it "
                                f"touches a sensitive path: {', '.join(touched)}."
                            ),
                        )
                    )
        # an unknown rule type is ignored on purpose (RFC 0 rule 6)
    return hits


def _risk_level(composite: float, cfg: GateConfig) -> str:
    bands = cfg.risk_bands
    if composite < bands.low:
        return "low"
    if composite < bands.medium:
        return "medium"
    if composite < bands.high:
        return "high"
    return "very_high"


def _config_hash(cfg: GateConfig) -> str:
    """Step 7, with the fallback described at the top of this file."""
    if cfg.config_hash:
        return cfg.config_hash
    payload = cfg.model_dump(mode="json", exclude={"config_hash"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decide(
    record: ChangeRecord, signals: list[SignalResult], cfg: GateConfig
) -> Decision:
    """RFC 9.5 steps 1 to 7. Deterministic: same input, same Decision."""
    policy = _policy(record, cfg)

    effective: dict[str, float] = {}
    excluded: list[str] = []
    for signal in signals:
        value = _effective(signal, policy, cfg)
        if value is None:
            excluded.append(signal.signal)
        else:
            effective[signal.signal] = value

    composite, weights_used = _composite(effective, cfg)
    hits = _hard_stops(record, signals, effective, cfg)
    decision = "HOLD" if (composite > cfg.threshold or hits) else "APPROVE"

    return Decision(
        decision=decision,  # type: ignore[arg-type]
        composite=composite,
        threshold=float(cfg.threshold),
        risk_level=_risk_level(composite, cfg),  # type: ignore[arg-type]
        weights_used=weights_used,  # type: ignore[arg-type]
        excluded_signals=excluded,  # type: ignore[arg-type]
        hard_stops_fired=hits,
        unmatched_policy_applied=policy,
        config_hash=_config_hash(cfg),
    )


def regate(run, cfg: GateConfig) -> Decision:
    """Re-run steps 1 to 7 over a stored run. What the live threshold slider calls.

    Typed loosely on purpose: `run` is a `docket.models.run.Run`, but plane 3 only
    needs its `record` and `signals`, and taking them by attribute keeps the gate
    usable from a replay that holds them apart.
    """
    return decide(run.record, list(run.signals), cfg)
