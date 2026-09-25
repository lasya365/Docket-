"""Loads config.json and the environment (RFC sections 5.4, 5.5).

Nothing else in Docket reads config.json or os.environ. decide/ never imports
this module: the pipeline passes it the plain config objects.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docket.models.config import DocketConfig, GateConfig

log = logging.getLogger("docket.settings")

# backend/src/docket/settings.py -> backend/src/docket -> backend/src -> backend -> <repo root>
REPO_ROOT = Path(__file__).resolve().parents[3]


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_hash_of(gate_block: dict, signals_block: dict) -> str:
    """sha256 of the canonical JSON of the gate + signals config blocks (RFC 9.5 step 7)."""
    payload = _canonical({"gate": gate_block, "signals": signals_block})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Settings:
    config: DocketConfig
    raw: dict = field(default_factory=dict)
    config_path: Path = REPO_ROOT / "config.json"
    repo_root: Path = REPO_ROOT

    # environment (RFC 5.5)
    ingest_token: str = ""
    api_token: str = ""
    github_token: str = ""
    freshservice_api_key: str = ""
    anthropic_api_key: str = ""
    db_path: str = "./docket.sqlite"
    mode: str = "live"
    simulate_coverage: bool = False
    replay_writes: bool = False

    # ---- convenience views -------------------------------------------------

    def gate_config(self, threshold_override: float | None = None) -> GateConfig:
        """The gate config as decide/ wants it: sensitive paths folded in, hash filled."""
        gate = self.config.gate.model_copy(deep=True)
        gate.sensitive_paths = list(self.config.sensitive.paths)
        gate.config_hash = self.config.gate.config_hash
        if threshold_override is not None:
            gate.threshold = float(threshold_override)
        return gate

    def path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (self.repo_root / p)

    # ---- invariant ---------------------------------------------------------

    def check_invariants(self) -> list[str]:
        """RFC 9.5: an unmatched, undeclared code change must always be held."""
        warnings: list[str] = []
        g = self.config.gate
        if g.unmatched_policy == "penalize":
            # worst realistic floor: two not-computable signals, one degraded, one at 0
            worst = (g.not_computable_score * 2 + g.degraded_floor + 0.0) / 4
            if worst <= g.threshold:
                warnings.append(
                    "config breaks the unmatched invariant: an unmatched, undeclared code change "
                    f"can score {worst:.2f}, which does not exceed the threshold of {g.threshold}."
                )
        total = sum(g.weights.values())
        if total <= 0:
            warnings.append("gate.weights sum to zero.")
        for w in warnings:
            log.warning(w)
        return warnings


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(config_path: str | Path | None = None, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    # An empty value (e.g. `DOCKET_CONFIG=` in a sourced .env) means "not set".
    path = Path(config_path) if config_path else Path(env.get("DOCKET_CONFIG") or REPO_ROOT / "config.json")
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    cfg = DocketConfig.model_validate(raw)
    cfg.gate.sensitive_paths = list(cfg.sensitive.paths)
    cfg.gate.config_hash = config_hash_of(raw.get("gate", {}), raw.get("signals", {}))

    s = Settings(
        config=cfg,
        raw=raw,
        config_path=Path(path),
        # Always the Docket install, never the config file's folder: DOCKET_CONFIG may point
        # anywhere, but frontend/, demo/ and every relative path in config.json belong to Docket.
        repo_root=REPO_ROOT,
        ingest_token=env.get("DOCKET_INGEST_TOKEN", ""),
        api_token=env.get("DOCKET_API_TOKEN", ""),
        github_token=env.get("GITHUB_TOKEN", ""),
        freshservice_api_key=env.get("FRESHSERVICE_API_KEY", ""),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
        db_path=env.get("DOCKET_DB") or "./docket.sqlite",
        mode=(env.get("DOCKET_MODE") or "live").strip(),
        simulate_coverage=_truthy(env.get("DOCKET_SIMULATE_COVERAGE", "")),
        replay_writes=_truthy(env.get("DOCKET_REPLAY_WRITES", "")),
    )
    s.check_invariants()
    return s


_cached: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _cached
    if _cached is None or reload:
        _cached = load_settings()
    return _cached
