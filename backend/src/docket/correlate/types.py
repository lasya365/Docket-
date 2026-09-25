"""Plane-2 internal types (RFC sections 6.6, 8.1, 8.2).

These three objects never leave plane 2: only `models/` objects cross the
boundary out (RFC section 6, P2).

Assumptions written down as RFC section 0 rule 2 asks:
  * RFC 8.1 method 3 says an ambiguous time match "joins none and adds the note
    `ambiguous: N sessions overlap`", but `correlate()` returns only
    `list[JoinResult]`, so there is nowhere else for a note to live. The simplest
    reading that keeps the signature: the correlator may return a *note-only*
    JoinResult (`JoinResult.note_only(...)`) with an empty `session_id`,
    `method="none"` and `confidence="unmatched"`. `is_join` is False for it, the
    Record Builder never makes a SessionSection from it, and its note is folded
    into `join.notes`. Nothing "joins" because of it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from docket.models.common import Attribution, JoinConfidence, JoinMethod
from docket.models.change_record import HunkAttribution
from docket.models.fragments import SessionFragment


class JoinResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    session: SessionFragment | None = None      # None for a ghost session
    method: JoinMethod = "none"
    confidence: JoinConfidence = "unmatched"    # the attributor may raise it to "verified"
    matched_commits: list[str] = Field(default_factory=list)
    note: str = ""

    @property
    def is_ghost(self) -> bool:
        """A commit named a session Docket never received telemetry for."""
        return bool(self.session_id) and self.session is None

    @property
    def is_join(self) -> bool:
        """False for a note-only result, which joins nothing."""
        return bool(self.session_id)

    @classmethod
    def note_only(cls, note: str) -> "JoinResult":
        return cls(session_id="", session=None, method="none", confidence="unmatched", note=note)


class DiscardInfo(BaseModel):
    """Text a session wrote that never reached the final diff (RFC 8.2 step 8)."""

    model_config = ConfigDict(extra="forbid")

    lines: int = 0
    files: list[str] = Field(default_factory=list)


class AttributionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribution_by_line: dict[str, dict[int, Attribution]] = Field(default_factory=dict)
    hunk_attribution: list[HunkAttribution] = Field(default_factory=list)
    totals: dict[Attribution, int] = Field(default_factory=dict)
    matched_lines_by_session: dict[str, int] = Field(default_factory=dict)   # non-trivial only
    discarded_by_session: dict[str, DiscardInfo] = Field(default_factory=dict)
    files_written_by_session: dict[str, list[str]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)   # e.g. files whose patch was omitted
