"""The seal: SHA-256 over the canonical run (RFC 10.5).

Assumptions written down as RFC section 0 rule 2 asks:
  * The sealed document is exactly {record, signals, decision, chain, backout}.
    `advisory` and `outputs` are written after the seal, so they are excluded;
    `rollout_text` and `mode` are not in the RFC's list either, so they are out.
  * Models are dumped with mode="json" so datetimes become fixed ISO strings and
    the hash does not depend on Python object identity or ordering.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from docket.models.run import Run

SEALED_KEYS = ("record", "signals", "decision", "chain", "backout")


def canonical_payload(run: Run) -> dict[str, Any]:
    """The subset of the run that the seal covers."""
    data = run.model_dump(mode="json")
    return {key: data[key] for key in SEALED_KEYS}


def canonical_json(run: Run) -> str:
    return json.dumps(
        canonical_payload(run), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def seal(run: Run) -> str:
    """`sha256:<hex>` of the canonical JSON of the sealed subset."""
    digest = hashlib.sha256(canonical_json(run).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def verify_seal(run: Run) -> bool:
    """True when the stored seal still matches the evidence it covers."""
    return bool(run.seal) and run.seal == seal(run)


# the name this had before the plane-4 integration contract settled
verify = verify_seal
