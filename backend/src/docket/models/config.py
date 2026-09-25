"""Pydantic mirrors of the config.json blocks (RFC section 6.6).

decide/ receives these objects as arguments, which is how it stays free of I/O.
`GateConfig.config_hash` is filled by settings.py from the raw gate + signals
blocks, because the gate itself never sees the signals block (RFC 9.5 step 7);
when it is empty the gate hashes what it does hold.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from docket.models.common import Kind, SignalName


class ReviewDepthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds_per_line: float = 2.0
    speed_weight: float = 0.7
    engagement_weight: float = 0.3
    stale_penalty: float = 25


class UnattributedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    always_in_scope: list[str] = Field(default_factory=list)


class UntestedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code_globs: list[str] = Field(default_factory=list)
    test_globs: list[str] = Field(default_factory=list)


class BlastRadiusPoints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sensitive_tool: float = 40
    sensitive_path: float = 25
    sensitive_path_cap: float = 50
    sensitive_bash: float = 20
    sensitive_bash_cap: float = 40
    bypass_mode: float = 20
    sandbox_disabled: float = 10
    mostly_auto_approved: float = 10


class BlastRadiusConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: BlastRadiusPoints = Field(default_factory=BlastRadiusPoints)
    auto_approved_ratio: float = 0.9
    auto_approved_min_calls: int = 10
    non_code_base: dict[str, float] = Field(default_factory=dict)


class RehearsalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regression_weight: float = 10
    changed_weight: float = 1


class SignalsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_depth: ReviewDepthConfig = Field(default_factory=ReviewDepthConfig)
    unattributed: UnattributedConfig = Field(default_factory=UnattributedConfig)
    untested: UntestedConfig = Field(default_factory=UntestedConfig)
    blast_radius: BlastRadiusConfig = Field(default_factory=BlastRadiusConfig)
    rehearsal: RehearsalConfig = Field(default_factory=RehearsalConfig)


class SensitiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    bash_patterns: list[str] = Field(default_factory=list)


class HardStopRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    signal: str | None = None
    value: float | None = None


class RiskBands(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: float = 25
    medium: float = 50
    high: float = 75


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: float = 45
    weights: dict[SignalName, float] = Field(default_factory=dict)
    not_computable_score: float = 80
    degraded_floor: float = 40
    unmatched_policy: str = "penalize"
    human_declared_label: str = "docket:human-authored"
    hard_stops: list[HardStopRule] = Field(default_factory=list)
    risk_bands: RiskBands = Field(default_factory=RiskBands)
    # Carried so the gate can evaluate HS3 without importing anything (RFC 9.5.4).
    sensitive_paths: list[str] = Field(default_factory=list)
    config_hash: str = ""


class CorrelatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trailer_key: str = "DocketSession-Id"
    allow_time_match: bool = True
    time_match_grace_minutes: int = 30
    verify_min_lines: int = 3


class AttributorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mixed_similarity: float = 0.75
    min_line_length: int = 4
    max_fuzzy_lines_per_file: int = 2000


class GithubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_repo: str = "owner/name"
    status_context: str = "docket/gate"
    status_states: dict[str, str] = Field(default_factory=dict)
    coverage_artifact_name: str = "coverage-json"


class FreshserviceDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: int = 1
    impact: int = 1
    status: int = 1
    change_type: int = 2


class FreshserviceCustomFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = "docket_decision"
    score: str = "docket_score"
    seal: str = "docket_seal"
    source_url: str = "docket_source_url"


class FreshserviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    domain: str = ""
    requester_id: int = 0
    defaults: FreshserviceDefaults = Field(default_factory=FreshserviceDefaults)
    risk_codes: dict[str, int] = Field(default_factory=dict)
    planned_start_offset_hours: int = 24
    planned_window_hours: int = 1
    custom_fields: FreshserviceCustomFields = Field(default_factory=FreshserviceCustomFields)
    approval_status_map: dict[str, list[int]] = Field(default_factory=dict)


class DeployConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_target: str | None = None
    default_window: str | None = None


class ManifestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = "demo/agent.manifest.json"
    rehearsal_dir: str = "demo/rehearsal"
    poll_seconds: int = 30


class BriefConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    model: str = "claude-sonnet-5"
    timeout_seconds: int = 20
    prompt_excerpt_chars: int = 280


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_base_url: str = "http://localhost:8000"


class DocketConfig(BaseModel):
    """The whole of config.json. Only settings.py builds one of these."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "3.0"
    gate: GateConfig = Field(default_factory=GateConfig)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    sensitive: SensitiveConfig = Field(default_factory=SensitiveConfig)
    correlator: CorrelatorConfig = Field(default_factory=CorrelatorConfig)
    attributor: AttributorConfig = Field(default_factory=AttributorConfig)
    github: GithubConfig = Field(default_factory=GithubConfig)
    freshservice: FreshserviceConfig = Field(default_factory=FreshserviceConfig)
    deploy: DeployConfig = Field(default_factory=DeployConfig)
    manifest: ManifestConfig = Field(default_factory=ManifestConfig)
    brief: BriefConfig = Field(default_factory=BriefConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    def non_code_base(self, kind: Kind) -> float:
        return float(self.signals.blast_radius.non_code_base.get(kind, 0.0))
