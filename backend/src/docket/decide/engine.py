"""The Signal Engine (RFC section 9).

Four pure functions over one ChangeRecord, always returned in the RFC's order:
unattributed, review_depth, untested, blast_radius.

Pure: standard library, pathspec and docket.models only. No clock, no I/O, no LLM.
"""

from __future__ import annotations

from docket.models.change_record import ChangeRecord
from docket.models.config import SensitiveConfig, SignalsConfig
from docket.models.signals import SignalResult

from docket.decide.signals import blast_radius, review_depth, unattributed, untested


def run_signals(
    record: ChangeRecord, cfg: SignalsConfig, sensitive: SensitiveConfig
) -> list[SignalResult]:
    """Always four results, in the order the RFC lists them."""
    return [
        unattributed.compute(record, cfg.unattributed),
        review_depth.compute(record, cfg.review_depth),
        untested.compute(record, cfg.untested, cfg.rehearsal),
        blast_radius.compute(record, cfg.blast_radius, sensitive),
    ]
