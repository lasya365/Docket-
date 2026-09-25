"""Shared test setup. No test may touch the network (RFC section 0, rule 8)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))
sys.path.insert(0, str(REPO_ROOT / "backend" / "tests"))

from docket.settings import load_settings  # noqa: E402


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


# The operator edits config.json to connect a real Freshservice account. Tests must
# not follow it (RFC section 0 rule 8), so its freshservice block is reset to these.
OFFLINE_FRESHSERVICE = {
    "enabled": False,
    "domain": "yourcompany.freshservice.com",
    "requester_id": 0,
    "custom_fields": {
        "decision": "docket_decision",
        "score": "docket_score",
        "seal": "docket_seal",
        "source_url": "docket_source_url",
    },
}


def offline_config(raw: dict) -> dict:
    """config.json as parsed JSON, with the account-specific Freshservice values reset."""
    raw["freshservice"].update(json.loads(json.dumps(OFFLINE_FRESHSERVICE)))
    return raw


@pytest.fixture()
def settings(tmp_path):
    """Settings loaded from the shipped config.json, with no environment secrets."""
    raw = offline_config(json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8")))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_settings(path, env={})


@pytest.fixture()
def cfg(settings):
    return settings.config


@pytest.fixture()
def gate_cfg(settings):
    return settings.gate_config()
