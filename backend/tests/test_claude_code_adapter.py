"""RFC 15.3: shuffled events give ordered turns; `mcp_tool` becomes
`server.tool`; an abbreviated `git_commit_id` is kept."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from docket.collectors.adapters import ADAPTERS, get_adapter
from docket.collectors.adapters.claude_code import build_session, display_name_for
from docket.collectors.otlp import FlatEvent
from docket.models.fragments import EditEvent

T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
SID = "sess-1"


def ev(name, offset_s, seq=None, **attrs):
    base = {"session.id": SID}
    if seq is not None:
        base["event.sequence"] = seq
    base.update(attrs)
    return FlatEvent(name=name, timestamp=T0 + timedelta(seconds=offset_s), attrs=base)


def prompt(pid, offset_s, text="do the thing", seq=None):
    attrs = {"prompt.id": pid, "prompt_length": len(text)}
    if text is not None:
        attrs["prompt"] = text
    return ev("user_prompt", offset_s, seq=seq, **attrs)


def tool(tuid, offset_s, tool_name="Edit", params=None, **attrs):
    extra = dict(attrs)
    if params is not None:
        extra["tool_parameters"] = json.dumps(params)
    return ev("tool_result", offset_s, tool_name=tool_name, tool_use_id=tuid,
              success="true", **extra)


# --------------------------------------------------------------------------- #


def test_shuffled_events_produce_ordered_turns():
    events = [prompt("p1", 0), prompt("p2", 10), prompt("p3", 20), prompt("p4", 30)]
    shuffled = events[:]
    random.Random(7).shuffle(shuffled)
    assert [e.attrs["prompt.id"] for e in shuffled] != ["p1", "p2", "p3", "p4"]

    frag = build_session(SID, shuffled, [], {})
    assert [t.prompt_id for t in frag.turns] == ["p1", "p2", "p3", "p4"]
    assert [t.started_at for t in frag.turns] == sorted(t.started_at for t in frag.turns)
    assert frag.started_at == T0
    assert frag.ended_at == T0 + timedelta(seconds=30)


def test_event_sequence_breaks_timestamp_ties():
    # Same timestamp, sequence out of order in the input.
    events = [prompt("p2", 0, seq=2), prompt("p1", 0, seq=1), prompt("p3", 0, seq=3)]
    frag = build_session(SID, events, [], {})
    assert [t.prompt_id for t in frag.turns] == ["p1", "p2", "p3"]


def test_mcp_tool_becomes_server_dot_tool():
    params = {"mcp_server_name": "entitlement_svc", "mcp_tool_name": "grant"}
    frag = build_session(SID, [tool("t1", 1, tool_name="mcp_tool", params=params)], [], {})
    call = frag.tool_calls[0]
    assert call.name == "mcp_tool"                     # raw name kept
    assert call.display_name == "entitlement_svc.grant"
    assert call.mcp_server == "entitlement_svc"
    assert call.mcp_tool == "grant"
    assert frag.detail.tool_details is True

    # A plain tool keeps its own name.
    plain = build_session(SID, [tool("t2", 1, tool_name="Bash")], [], {}).tool_calls[0]
    assert plain.display_name == "Bash"
    assert display_name_for("Bash", None, None) == "Bash"


def test_abbreviated_git_commit_id_is_kept_as_is():
    params = {"git_commit_id": "a91f3c1", "git_branch": "feature/x"}
    frag = build_session(SID, [tool("t1", 1, tool_name="Bash", params=params)], [], {})
    call = frag.tool_calls[0]
    assert call.git_commit_sha == "a91f3c1"            # not padded, not rejected
    assert call.git_branch == "feature/x"


def test_vcs_head_revision_wins_over_tool_parameters():
    full = "a91f3c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b"
    frag = build_session(
        SID,
        [tool("t1", 1, tool_name="Bash", params={"git_commit_id": "a91f3c1"},
              **{"vcs.ref.head.revision": full, "vcs.ref.head.name": "main"})],
        [], {},
    )
    assert frag.tool_calls[0].git_commit_sha == full
    assert frag.tool_calls[0].git_branch == "main"


def test_tool_parameters_that_are_not_json_are_tolerated():
    e = ev("tool_result", 1, tool_name="Bash", tool_use_id="t1", success="false",
           tool_parameters="{not json")
    frag = build_session(SID, [e], [], {})
    call = frag.tool_calls[0]
    assert call.success is False
    assert call.bash_command is None
    assert frag.detail.tool_details is False           # nothing usable arrived


def test_bash_command_prefers_full_command_and_reads_sandbox_flag():
    params = {"bash_command": "terraform", "full_command": "terraform apply -auto-approve",
              "dangerouslyDisableSandbox": True}
    call = build_session(SID, [tool("t1", 1, tool_name="Bash", params=params)], [], {}).tool_calls[0]
    assert call.bash_command == "terraform apply -auto-approve"
    assert call.sandbox_disabled is True


def test_header_models_retries_rejections_and_mode_changes():
    events = [
        ev("api_request", 1, model="claude-sonnet-5", **{"app.version": "2.1.269",
           "user.email": "dev@example.com", "vcs.repository.url.full": "https://github.com/o/n"}),
        ev("api_request", 2, model="claude-sonnet-5"),
        ev("api_request", 3, model="claude-haiku-5"),
        ev("api_error", 4, attempt="3"),
        ev("api_error", 5, attempt=1),
        ev("tool_decision", 6, decision="reject", tool_name="Bash", tool_use_id="t9", source="user_reject"),
        ev("tool_decision", 7, decision="accept", tool_name="Bash", tool_use_id="t8", source="config"),
        ev("permission_mode_changed", 8, from_mode="default", to_mode="acceptEdits", trigger="user"),
        ev("something_new_from_a_future_version", 9),
    ]
    frag = build_session(SID, events, [], {})
    assert frag.models == ["claude-sonnet-5", "claude-haiku-5"]      # unique, in order
    assert frag.agent_version == "2.1.269"
    assert frag.user_email == "dev@example.com"
    assert frag.repo_url == "https://github.com/o/n"
    assert frag.api_retries == 2                                     # (3-1) + (1-1)
    assert [r.tool_use_id for r in frag.rejections] == ["t9"]        # only decision == reject
    assert frag.permission_mode_changes[0].to_mode == "acceptEdits"


def test_redacted_prompt_gives_no_text_and_detail_prompts_false():
    redacted = ev("user_prompt", 0, **{"prompt.id": "p1", "prompt": "<REDACTED>", "prompt_length": 42})
    absent = ev("user_prompt", 1, **{"prompt.id": "p2", "prompt_length": 7})
    frag = build_session(SID, [redacted, absent], [], {})
    assert [t.prompt_text for t in frag.turns] == [None, None]
    assert frag.turns[0].prompt_length == 42
    assert frag.detail.prompts is False

    real = ev("user_prompt", 2, **{"prompt.id": "p3", "prompt": "hello", "prompt_length": 5})
    assert build_session(SID, [redacted, real], [], {}).detail.prompts is True


def test_edits_attach_file_paths_and_turns():
    edit = EditEvent(session_id=SID, tool_use_id="t1", prompt_id="p1", tool_name="Edit",
                     file_path="provisioning/sync.py", written_lines=["a"], timestamp=T0 + timedelta(seconds=2))
    events = [prompt("p1", 0), tool("t1", 2, tool_name="Edit", **{"prompt.id": "p1"})]
    frag = build_session(SID, events, [edit], {"repo_root": "/repo", "cwd": "/repo/sub"})
    assert frag.tool_calls[0].file_path == "provisioning/sync.py"
    assert frag.turns[0].tool_use_ids == ["t1"]
    assert frag.detail.edits is True
    assert frag.repo_root == "/repo"
    assert frag.edit_events == [edit]


def test_edit_without_a_tool_result_still_names_its_turn():
    edit = EditEvent(session_id=SID, tool_use_id="t5", prompt_id="p1", tool_name="Write",
                     file_path="a.py", written_lines=["x"], timestamp=T0 + timedelta(seconds=3))
    frag = build_session(SID, [prompt("p1", 0)], [edit], {})
    assert frag.turns[0].tool_use_ids == ["t5"]
    assert frag.tool_calls == []


def test_unknown_decision_source_is_dropped_known_one_is_kept():
    kept = build_session(SID, [tool("t1", 1, decision_source="config")], [], {}).tool_calls[0]
    assert kept.decision_source == "config"
    dropped = build_session(SID, [tool("t2", 1, decision_source="martian")], [], {}).tool_calls[0]
    assert dropped.decision_source is None


def test_a_session_with_nothing_usable_returns_none():
    # Nothing to assemble: the caller treats this as a session never received.
    assert build_session("ghost", [], [], {}) is None
    assert build_session("ghost", [], [], None) is None
    assert build_session("ghost", None, None, None) is None


def test_partial_evidence_still_yields_a_fragment():
    # Telemetry but no edits.
    frag = build_session(SID, [tool("t1", 1)], [], {})
    assert frag is not None
    assert frag.tool_calls and frag.edit_events == []
    assert frag.detail.edits is False
    assert frag.provenance == "real"

    # Edits but no telemetry (the hook arrived, the exporter did not).
    edit = EditEvent(session_id=SID, tool_use_id="t9", prompt_id=None, tool_name="Write",
                     file_path="a.py", written_lines=["x"], timestamp=T0)
    frag = build_session(SID, [], [edit], {"repo_root": "/repo"})
    assert frag is not None
    assert frag.turns == [] and frag.tool_calls == []
    assert frag.edit_events == [edit]
    assert frag.detail.edits is True and frag.detail.prompts is False
    assert frag.started_at == frag.ended_at == T0
    assert frag.repo_root == "/repo"


def test_fragment_round_trips_through_json():
    frag = build_session(SID, [prompt("p1", 0), tool("t1", 1)], [], {})
    assert frag.model_validate(json.loads(frag.model_dump_json())) == frag


def test_adapter_seam_registers_only_claude_code():
    assert list(ADAPTERS) == ["claude-code"]
    assert ADAPTERS["claude-code"] is build_session
    assert get_adapter("claude-code") is build_session
    assert get_adapter("some-other-agent") is None


def test_build_session_accepts_store_rows_as_well_as_flat_events():
    """`store.load_session_parts` hands back {name, timestamp, attrs} dicts."""
    rows = [
        {"name": "user_prompt", "timestamp": "2026-03-01T12:00:10+00:00",
         "attrs": {"prompt.id": "p2", "prompt": "second", "prompt_length": 6}},
        {"name": "user_prompt", "timestamp": "2026-03-01T12:00:00+00:00",
         "attrs": {"prompt.id": "p1", "prompt": "first", "prompt_length": 5}},
    ]
    frag = build_session(SID, rows, [], {})
    assert [t.prompt_id for t in frag.turns] == ["p1", "p2"]
    assert frag.started_at == T0
    assert frag.detail.prompts is True
    # A row that is not a dict at all is read as an empty event, never a crash.
    assert build_session(SID, ["junk"], [], {}).turns == []
