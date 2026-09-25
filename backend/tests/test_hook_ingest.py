"""RFC 15.3: Edit, Write and MultiEdit payloads become EditEvents.

The route test lives with server.py; this file tests the parsing only.
"""

from __future__ import annotations

from datetime import datetime, timezone

from docket.collectors.hooks_ingest import HookResult, build_edit_event, parse_hook_payload

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
SID = "sess-1"


def payload(**over):
    base = {
        "hook_event_name": "PostToolUse",
        "session_id": SID,
        "prompt_id": "p1",
        "cwd": "/repo/sub",
        "permission_mode": "acceptEdits",
        "tool_use_id": "t1",
    }
    base.update(over)
    return base


def test_edit_payload_becomes_an_edit_event():
    result = parse_hook_payload(
        payload(tool_name="Edit", tool_input={
            "file_path": "provisioning/sync.py",
            "old_string": "a = 1\nb = 2",
            "new_string": "a = 1\nb = 3\nc = 4",
        }),
        now=NOW,
    )
    assert result.ok and result.hook_event_name == "PostToolUse"
    edit = result.edit
    assert edit is not None
    assert edit.session_id == SID
    assert edit.tool_name == "Edit"
    assert edit.tool_use_id == "t1"
    assert edit.prompt_id == "p1"
    assert edit.file_path == "provisioning/sync.py"
    assert edit.written_lines == ["a = 1", "b = 3", "c = 4"]
    assert edit.removed_lines == ["a = 1", "b = 2"]
    assert edit.timestamp == NOW
    assert edit.timestamp.tzinfo is not None


def test_write_payload_becomes_an_edit_event():
    result = parse_hook_payload(
        payload(tool_name="Write", tool_input={"file_path": "a/new.py", "content": "x = 1\ny = 2"}),
        now=NOW,
    )
    edit = result.edit
    assert edit is not None
    assert edit.tool_name == "Write"
    assert edit.file_path == "a/new.py"
    assert edit.written_lines == ["x = 1", "y = 2"]
    assert edit.removed_lines == []


def test_multiedit_payload_concatenates_every_edit_in_order():
    result = parse_hook_payload(
        payload(tool_name="MultiEdit", tool_input={
            "file_path": "a/m.py",
            "edits": [
                {"old_string": "old1", "new_string": "new1a\nnew1b"},
                {"old_string": "old2a\nold2b", "new_string": "new2"},
            ],
        }),
        now=NOW,
    )
    edit = result.edit
    assert edit is not None
    assert edit.tool_name == "MultiEdit"
    assert edit.written_lines == ["new1a", "new1b", "new2"]
    assert edit.removed_lines == ["old1", "old2a", "old2b"]


def test_notebook_edit_uses_notebook_path_and_new_source():
    result = parse_hook_payload(
        payload(tool_name="NotebookEdit", tool_input={
            "notebook_path": "nb/run.ipynb", "new_source": "import os\nos.getcwd()",
        }),
        now=NOW,
    )
    edit = result.edit
    assert edit is not None
    assert edit.file_path == "nb/run.ipynb"
    assert edit.written_lines == ["import os", "os.getcwd()"]


def test_session_start_carries_meta_and_no_edit():
    result = parse_hook_payload(
        {"hook_event_name": "SessionStart", "session_id": SID,
         "cwd": "/repo/sub", "repo_root": "/repo", "permission_mode": "default"},
        now=NOW,
    )
    assert result.is_session_start
    assert result.edit is None
    assert result.meta == {"cwd": "/repo/sub", "repo_root": "/repo",
                           "permission_mode": "default"}
    assert result.session_meta() == result.meta
    assert (result.cwd, result.repo_root, result.permission_mode) == (
        "/repo/sub", "/repo", "default")


def test_agent_id_marks_a_subagent_edit():
    result = parse_hook_payload(
        payload(tool_name="Write", agent_id="sub-7",
                tool_input={"file_path": "a.py", "content": "z"}),
        now=NOW,
    )
    assert result.edit is not None and result.edit.agent_id == "sub-7"


def test_missing_keys_are_tolerated():
    # No tool_input at all.
    assert parse_hook_payload(payload(tool_name="Edit"), now=NOW).edit is None
    # No file path.
    assert parse_hook_payload(
        payload(tool_name="Write", tool_input={"content": "x"}), now=NOW).edit is None
    # No new_string: an empty edit is still recorded, with what exists.
    edit = parse_hook_payload(
        payload(tool_name="Edit", tool_input={"file_path": "a.py"}), now=NOW).edit
    assert edit is not None and edit.written_lines == [] and edit.removed_lines == []
    # No tool_use_id.
    edit = parse_hook_payload(
        {"session_id": SID, "tool_name": "Write", "tool_input": {"file_path": "a.py", "content": "q"}},
        now=NOW).edit
    assert edit is not None and edit.tool_use_id == ""


def test_non_edit_tool_and_junk_payloads_never_raise():
    result = parse_hook_payload(payload(tool_name="Bash", tool_input={"command": "ls"}), now=NOW)
    assert result.ok and result.edit is None and result.tool_name == "Bash"

    assert parse_hook_payload(None) == HookResult()
    assert parse_hook_payload("not a dict") == HookResult()
    assert parse_hook_payload({}).ok is False
    assert parse_hook_payload({}).session_id is None
    assert parse_hook_payload({}).meta == {}
    # A payload with no session_id parses but has nothing to key on.
    assert parse_hook_payload({"hook_event_name": "PostToolUse"}).ok is False
    # Malformed nested shapes.
    assert parse_hook_payload(
        payload(tool_name="MultiEdit", tool_input={"file_path": "a.py", "edits": "nope"}),
        now=NOW).edit.written_lines == []
    assert parse_hook_payload(
        payload(tool_name="Edit", tool_input="nope"), now=NOW).edit is None


def test_timestamp_uses_the_injected_clock_or_the_receive_time():
    edit = build_edit_event(
        {"tool_name": "Write", "tool_input": {"file_path": "a.py", "content": "x"}},
        SID, datetime(1970, 1, 1, tzinfo=timezone.utc),
    )
    assert edit is not None and edit.timestamp.year == 1970
    # A payload that carries its own ISO timestamp wins over `now`.
    result = parse_hook_payload(
        payload(tool_name="Write", timestamp="2026-05-06T07:08:09Z",
                tool_input={"file_path": "a.py", "content": "x"}),
        now=NOW,
    )
    assert result.edit.timestamp == datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone.utc)


def test_hook_result_is_a_pydantic_model_that_round_trips():
    result = parse_hook_payload(
        payload(tool_name="Write", tool_input={"file_path": "a.py", "content": "x"}), now=NOW)
    assert HookResult.model_validate_json(result.model_dump_json()) == result
    assert result.meta == {"cwd": "/repo/sub", "permission_mode": "acceptEdits"}
    assert result.session_id == SID
