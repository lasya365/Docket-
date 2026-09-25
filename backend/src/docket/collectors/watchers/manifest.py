"""Manifest Watcher: turns bundle changes that produce no diff into `NonCodeChange`s (RFC 7.5).

Assumptions taken where RFC 7.5 is ambiguous (RFC section 0, rule 2 — simplest reading):

* RFC 7.5 step 6 says a different hash always yields one `NonCodeChange`, but the
  `kind` priority list only covers `model`, `prompt`, `tools`, `permissions` and
  `knowledge`. A manifest whose hash moved for some other reason (today only
  `workflows`, since `change_ref` is excluded from the hash) therefore has an empty
  `changed_keys`. We still emit the change — otherwise the pipeline never stores the
  new snapshot and the same delta is re-detected forever — and give it the lowest
  severity kind in the priority list, `knowledge`.
* "the file exists next to the manifest" (step 1) is read as: `prompt.path` is
  resolved relative to the directory holding the manifest.
* List fields are compared in order, because the canonical JSON that feeds
  `manifest_hash` is order-sensitive too. Reordering `tools` is a change.

The watcher writes nothing and reads no clock: `now` and the latest snapshot are
arguments, so the module is deterministic in tests. The pipeline appends the new
snapshot to the ledger after the run is stored.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from docket.models.common import Kind
from docket.models.manifest import (
    AgentManifest,
    ManifestSnapshot,
    NonCodeChange,
    RehearsalFragment,
)

log = logging.getLogger("docket.collectors.watchers.manifest")

# RFC 7.5 step 6: the only keys a NonCodeChange may report.
COMPARED_KEYS: tuple[str, ...] = ("model", "prompt", "tools", "permissions", "knowledge")

# RFC 7.5 step 6: first match wins.
PERMISSION_KEYS: frozenset[str] = frozenset({"tools", "permissions"})
KIND_PRIORITY: tuple[tuple[str, Kind], ...] = (
    ("model", "model_version"),
    ("prompt", "prompt"),
    ("knowledge", "knowledge"),
)
FALLBACK_KIND: Kind = "knowledge"

# RFC 7.5 step 2: adding a ticket reference is not itself a change.
HASH_EXCLUDED: tuple[str, ...] = ("change_ref",)


class WatchResult(BaseModel):
    """What one `check()` saw (RFC 7.5)."""

    model_config = ConfigDict(extra="forbid")

    baseline: ManifestSnapshot | None = None
    changes: list[NonCodeChange] = Field(default_factory=list)


def _canonical(obj: object) -> str:
    """Canonical JSON: sorted keys, no whitespace (RFC 7.5 step 2)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_hash(manifest: AgentManifest) -> str:
    """SHA-256 of the canonical manifest JSON with `change_ref` removed (RFC 7.5 step 2)."""
    payload = manifest.model_dump(mode="json")
    for key in HASH_EXCLUDED:
        payload.pop(key, None)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def load_manifest(path: Path) -> tuple[AgentManifest, str]:
    """Read the manifest, fill `prompt.sha256` from disk, and hash it (RFC 7.5 steps 1-2).

    Returns `(manifest, manifest_hash)`. A missing prompt file is not an error
    (RFC section 0, rule 6): `prompt.sha256` is left as it was in the file.
    """
    path = Path(path)
    manifest = AgentManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))

    if manifest.prompt.path:
        prompt_file = path.parent / manifest.prompt.path
        if prompt_file.is_file():
            digest = hashlib.sha256(prompt_file.read_bytes()).hexdigest()
            manifest.prompt.sha256 = digest
        else:
            log.debug("prompt file %s does not exist next to %s", manifest.prompt.path, path)

    return manifest, manifest_hash(manifest)


def changed_keys(before: AgentManifest, after: AgentManifest) -> list[str]:
    """The keys among `COMPARED_KEYS` whose values differ (RFC 7.5 step 6)."""
    return [
        key
        for key in COMPARED_KEYS
        if getattr(before, key) != getattr(after, key)
    ]


def kind_for(keys: list[str]) -> Kind:
    """First match in the RFC 7.5 step 6 priority order."""
    changed = set(keys)
    if changed & PERMISSION_KEYS:
        return "permission"
    for key, kind in KIND_PRIORITY:
        if key in changed:
            return kind
    return FALLBACK_KIND


def rehearsal_path(rehearsal_dir: Path, agent_id: str, after_hash: str) -> Path:
    """`{rehearsal_dir}/{agent_id}-{manifest_hash[:12]}.json` (RFC 7.5 step 7)."""
    return Path(rehearsal_dir) / f"{agent_id}-{after_hash[:12]}.json"


def load_rehearsal(
    rehearsal_dir: Path | None, agent_id: str, after_hash: str
) -> RehearsalFragment | None:
    """The stored rehearsal corpus for the "after" bundle, if one was shipped (RFC 7.5 step 7)."""
    if rehearsal_dir is None:
        return None
    candidate = rehearsal_path(rehearsal_dir, agent_id, after_hash)
    if not candidate.is_file():
        log.debug("no rehearsal fragment at %s", candidate)
        return None
    try:
        return RehearsalFragment.model_validate(
            json.loads(candidate.read_text(encoding="utf-8"))
        )
    except Exception:  # missing evidence is a value, not an exception (rule 6)
        log.debug("rehearsal fragment at %s could not be read", candidate, exc_info=True)
        return None


def check(
    path: Path,
    latest_snapshot: ManifestSnapshot | None,
    now: datetime,
    rehearsal_dir: Path | None = None,
) -> WatchResult:
    """Compare the manifest on disk with the latest snapshot in the ledger (RFC 7.5).

    `latest_snapshot` is read from the ledger by the pipeline and passed in; `now`
    is the observation time. This function writes nothing.
    """
    manifest, after_hash = load_manifest(path)

    after = ManifestSnapshot(
        agent_id=manifest.agent_id,
        manifest_hash=after_hash,
        manifest=manifest,
        observed_at=now,
        sequence=(latest_snapshot.sequence + 1) if latest_snapshot is not None else 1,
    )

    # Step 4: first sighting. Baseline only, so the next change has something to roll back to.
    if latest_snapshot is None:
        return WatchResult(baseline=after, changes=[])

    # Step 5: nothing moved.
    if latest_snapshot.manifest_hash == after_hash:
        return WatchResult(baseline=None, changes=[])

    # Step 6: one NonCodeChange carrying every changed key.
    keys = changed_keys(latest_snapshot.manifest, manifest)
    if not keys:
        log.debug(
            "manifest hash for %s moved with no compared key changed; reporting as %s",
            manifest.agent_id,
            FALLBACK_KIND,
        )

    change = NonCodeChange(
        agent_id=manifest.agent_id,
        kind=kind_for(keys),
        changed_keys=keys,
        before=latest_snapshot,
        after=after,
        # Step 7: attach the stored rehearsal corpus for the "after" bundle, if present.
        rehearsal=load_rehearsal(rehearsal_dir, manifest.agent_id, after_hash),
        detected_at=now,
    )
    return WatchResult(baseline=None, changes=[change])
