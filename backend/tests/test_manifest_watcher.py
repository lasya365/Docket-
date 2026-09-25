"""RFC 15.3 `test_manifest_watcher`.

Asserts: first sighting returns a baseline and no change; an unchanged manifest
returns nothing; a model change is detected; a `tools` change gives `permission`;
and adding a `change_ref` alone is not a change.

Offline (RFC section 0, rule 8): every manifest is written into `tmp_path`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from docket.collectors.watchers.manifest import (
    check,
    changed_keys,
    kind_for,
    load_manifest,
    manifest_hash,
)
from docket.models.manifest import AgentManifest, ManifestSnapshot

NOW = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=1)

BASE: dict = {
    "agent_id": "resolution-agent",
    "model": "vendor-4.1",
    "prompt": {"path": "prompts/system.md", "sha256": ""},
    "tools": ["kb.search", "ticket.update"],
    "permissions": ["tickets:write"],
    "knowledge": [{"id": "kb-2211", "revision": "6"}],
    "workflows": ["incident-triage", "entitlement-provisioning", "knowledge-retrieval"],
    "change_ref": None,
}


def write_manifest(tmp_path: Path, **overrides) -> Path:
    """Write a manifest (plus its prompt file) into tmp_path and return its path."""
    data = json.loads(json.dumps(BASE))
    data.update(overrides)
    path = tmp_path / "agent.manifest.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    prompt = tmp_path / "prompts" / "system.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    if not prompt.exists():
        prompt.write_text("You resolve tickets.\n", encoding="utf-8")
    return path


def snapshot_of(path: Path, *, sequence: int = 1, observed_at: datetime = NOW) -> ManifestSnapshot:
    manifest, digest = load_manifest(path)
    return ManifestSnapshot(
        agent_id=manifest.agent_id,
        manifest_hash=digest,
        manifest=manifest,
        observed_at=observed_at,
        sequence=sequence,
    )


# --- step 1: prompt hashing ------------------------------------------------


def test_load_manifest_hashes_the_prompt_file(tmp_path):
    path = write_manifest(tmp_path)
    (tmp_path / "prompts" / "system.md").write_text("hello\n", encoding="utf-8")

    manifest, digest = load_manifest(path)

    assert manifest.prompt.sha256 == hashlib.sha256(b"hello\n").hexdigest()
    assert digest == manifest_hash(manifest)
    assert len(digest) == 64


def test_missing_prompt_file_is_not_an_exception(tmp_path):
    path = write_manifest(tmp_path)
    (tmp_path / "prompts" / "system.md").unlink()

    manifest, digest = load_manifest(path)

    assert manifest.prompt.sha256 == ""  # left as the file had it (rule 6)
    assert len(digest) == 64


# --- step 4: first sighting ------------------------------------------------


def test_first_sighting_returns_a_baseline_and_no_change(tmp_path):
    path = write_manifest(tmp_path)

    result = check(path, None, NOW)

    assert result.changes == []
    assert result.baseline is not None
    assert result.baseline.agent_id == "resolution-agent"
    assert result.baseline.sequence == 1
    assert result.baseline.observed_at == NOW
    assert result.baseline.manifest_hash == snapshot_of(path).manifest_hash


# --- step 5: nothing moved -------------------------------------------------


def test_unchanged_manifest_returns_nothing(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)

    result = check(path, latest, LATER)

    assert result.baseline is None
    assert result.changes == []


# --- step 6: model change --------------------------------------------------


def test_model_change_is_detected(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)
    write_manifest(tmp_path, model="vendor-4.2")

    result = check(path, latest, LATER)

    assert result.baseline is None
    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.kind == "model_version"
    assert change.changed_keys == ["model"]
    assert change.agent_id == "resolution-agent"
    assert change.before is not None
    assert change.before.manifest.model == "vendor-4.1"
    assert change.after.manifest.model == "vendor-4.2"
    assert change.after.sequence == 2
    assert change.detected_at == LATER
    assert change.after.manifest_hash != change.before.manifest_hash
    assert change.rehearsal is None  # no rehearsal_dir passed


# --- step 6: tools change --------------------------------------------------


def test_tools_change_gives_permission(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)
    write_manifest(tmp_path, tools=["kb.search", "ticket.update", "entitlement_svc.grant"])

    result = check(path, latest, LATER)

    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.kind == "permission"
    assert change.changed_keys == ["tools"]
    assert "entitlement_svc.grant" in change.after.manifest.tools


def test_permissions_change_gives_permission(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)
    write_manifest(tmp_path, permissions=["tickets:write", "entitlements:write"])

    result = check(path, latest, LATER)

    assert result.changes[0].kind == "permission"
    assert result.changes[0].changed_keys == ["permissions"]


def test_one_change_carries_all_changed_keys_and_permission_wins(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)
    write_manifest(
        tmp_path,
        model="vendor-4.2",
        tools=["kb.search"],
        knowledge=[{"id": "kb-2211", "revision": "7"}],
    )

    result = check(path, latest, LATER)

    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.kind == "permission"  # priority, even though model also moved
    assert change.changed_keys == ["model", "tools", "knowledge"]


def test_prompt_and_knowledge_kinds(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)
    (tmp_path / "prompts" / "system.md").write_text("Rewritten prompt.\n", encoding="utf-8")

    result = check(path, latest, LATER)

    assert result.changes[0].kind == "prompt"
    assert result.changes[0].changed_keys == ["prompt"]

    assert kind_for(["knowledge"]) == "knowledge"
    assert kind_for([]) == "knowledge"  # documented fallback


def test_knowledge_revision_change_is_detected(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)
    write_manifest(tmp_path, knowledge=[{"id": "kb-2211", "revision": "7"}])

    result = check(path, latest, LATER)

    assert result.changes[0].kind == "knowledge"
    assert result.changes[0].changed_keys == ["knowledge"]


# --- step 2: change_ref is excluded from the hash --------------------------


def test_change_ref_alone_is_not_a_change(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)
    write_manifest(tmp_path, change_ref="CHG-9001")

    result = check(path, latest, LATER)

    assert result.baseline is None
    assert result.changes == []


def test_manifest_hash_ignores_change_ref_only():
    without = AgentManifest.model_validate(BASE)
    with_ref = AgentManifest.model_validate({**BASE, "change_ref": "CHG-9001"})
    bumped = AgentManifest.model_validate({**BASE, "workflows": ["incident-triage"]})

    assert manifest_hash(without) == manifest_hash(with_ref)
    assert manifest_hash(without) != manifest_hash(bumped)
    assert changed_keys(without, with_ref) == []


# --- step 7: rehearsal attach ----------------------------------------------


def test_rehearsal_fragment_is_attached_when_present(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)
    write_manifest(tmp_path, model="vendor-4.2")
    after_hash = load_manifest(path)[1]

    rehearsal_dir = tmp_path / "rehearsal"
    rehearsal_dir.mkdir()
    fragment = {
        "agent_id": "resolution-agent",
        "manifest_hash": after_hash,
        "cases": 50,
        "identical": 44,
        "changed_acceptable": 4,
        "regressed": 2,
        "regressions": [{"case_id": "INC-20114", "before": "escalate", "after": "resolve"}],
        "provenance": "simulated",
    }
    (rehearsal_dir / f"resolution-agent-{after_hash[:12]}.json").write_text(
        json.dumps(fragment), encoding="utf-8"
    )

    change = check(path, latest, LATER, rehearsal_dir=rehearsal_dir).changes[0]

    assert change.rehearsal is not None
    assert change.rehearsal.cases == 50
    assert change.rehearsal.regressed == 2
    assert change.rehearsal.provenance == "simulated"
    assert change.rehearsal.manifest_hash == after_hash


def test_missing_rehearsal_dir_is_not_an_exception(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)
    write_manifest(tmp_path, model="vendor-4.2")

    change = check(path, latest, LATER, rehearsal_dir=tmp_path / "nope").changes[0]

    assert change.rehearsal is None


# --- the watcher is deterministic and writes nothing -----------------------


def test_check_writes_nothing_and_is_deterministic(tmp_path):
    path = write_manifest(tmp_path)
    latest = snapshot_of(path)
    write_manifest(tmp_path, model="vendor-4.2")
    before_tree = sorted(p.name for p in tmp_path.rglob("*"))

    first = check(path, latest, LATER)
    second = check(path, latest, LATER)

    assert first.model_dump_json() == second.model_dump_json()
    assert sorted(p.name for p in tmp_path.rglob("*")) == before_tree


# --- the shipped demo assets line up (appendix D) --------------------------


@pytest.mark.parametrize(
    "overrides, name",
    [
        ({"model": "vendor-4.2"}, "model bump"),
        (
            {"tools": ["kb.search", "ticket.update", "entitlement_svc.grant"]},
            "permission",
        ),
    ],
)
def test_shipped_rehearsal_files_match_the_demo_manifest(repo_root, cfg, overrides, name):
    """The demo rehearsal filenames must be the real hash of the AFTER bundle."""
    # The BASELINE, not cfg.manifest.path: that file is the demo's input and the
    # documented no-code demos edit it in place, which would turn this red.
    baseline = repo_root / "demo" / "agent.manifest.baseline.json"
    rehearsal_dir = repo_root / cfg.manifest.rehearsal_dir
    manifest, _ = load_manifest(baseline)

    after = manifest.model_copy(deep=True, update=overrides)
    expected = rehearsal_dir / f"{after.agent_id}-{manifest_hash(after)[:12]}.json"

    assert expected.is_file(), f"missing {name} rehearsal fragment: {expected}"
