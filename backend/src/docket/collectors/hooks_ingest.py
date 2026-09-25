"""Claude Code hook payloads -> EditEvent + session meta (RFC 7.2).

PURE PARSING ONLY. This module defines no routes: `server.py` calls
`parse_hook_payload` and decides what to store.

Assumptions taken here (RFC section 0, rule 2 — simplest reading, written down):
  * A payload with no `session_id` still parses; `HookResult.session_id` is
    None and `ok` is False, so the route can answer 200 and store nothing.
    Missing evidence is a value, not an exception (rule 6).
  * `written_lines` for MultiEdit is every `edits[].new_string` split on "\n"
    and concatenated in order, which is the simplest reading of the table.
  * The hook payload carries no timestamp of its own, so the edit is stamped
    with the receive time. `now` is injectable so every test is deterministic;
    when it is absent the clock is read once, here. (Rule 3 forbids a clock
    read inside `decide/`, not inside a collector — the receive time is the
    honest value for an event that carries none.)
  * `tool_name` outside the four the contract allows yields no EditEvent; the
    payload is logged at DEBUG and the session meta is still returned.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from docket.collectors.otlp import parse_iso, to_utc
from docket.models.fragments import EditEvent

log = logging.getLogger("docket.collectors.hooks_ingest")


# --- payload field names (RFC 7.2) -----------------------------------------
F_HOOK_EVENT_NAME = "hook_event_name"
F_SESSION_ID = "session_id"
F_PROMPT_ID = "prompt_id"
F_CWD = "cwd"
F_REPO_ROOT = "repo_root"          # added by agent-setup/.claude/hooks/docket_session_start.sh
F_PERMISSION_MODE = "permission_mode"
F_TOOL_NAME = "tool_name"
F_TOOL_USE_ID = "tool_use_id"
F_TOOL_INPUT = "tool_input"
F_AGENT_ID = "agent_id"
F_TIMESTAMP = "timestamp"          # not documented; used when a payload carries one

HOOK_SESSION_START = "SessionStart"
HOOK_POST_TOOL_USE = "PostToolUse"

# --- tool_input keys, per tool (RFC 7.2 table) ------------------------------
I_FILE_PATH = "file_path"
I_NOTEBOOK_PATH = "notebook_path"
I_NEW_STRING = "new_string"
I_OLD_STRING = "old_string"
I_CONTENT = "content"
I_NEW_SOURCE = "new_source"
I_EDITS = "edits"

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


class HookResult(BaseModel):
    """What one hook payload told Docket. Nothing here writes anywhere."""

    model_config = ConfigDict(extra="forbid")

    hook_event_name: str = ""
    session_id: str | None = None
    edit: EditEvent | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    # the rest of the payload's fields, kept so a route can log or index them
    prompt_id: str | None = None
    agent_id: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None

    @property
    def ok(self) -> bool:
        """True when the payload named a session Docket can key on."""
        return bool(self.session_id)

    @property
    def is_session_start(self) -> bool:
        return self.hook_event_name == HOOK_SESSION_START

    @property
    def cwd(self) -> str | None:
        return self.meta.get("cwd")

    @property
    def repo_root(self) -> str | None:
        return self.meta.get("repo_root")

    @property
    def permission_mode(self) -> str | None:
        return self.meta.get("permission_mode")

    def session_meta(self) -> dict[str, Any]:
        """The fields table `session_meta` keeps (RFC 7.2)."""
        return dict(self.meta)


def _str(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value != "" else None
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    return str(value)


def _split(text: Any) -> list[str]:
    """Raw text the agent wrote, split on "\\n". Anything else gives []."""
    if isinstance(text, str):
        return text.split("\n")
    if text is None:
        return []
    log.debug("tool_input text field was %s, not a string", type(text).__name__)
    return []


def _payload_time(payload: Mapping[str, Any], now: datetime | None) -> datetime:
    stamp = payload.get(F_TIMESTAMP)
    if isinstance(stamp, datetime):
        return to_utc(stamp)
    parsed = parse_iso(stamp)
    if parsed is not None:
        return parsed
    if now is not None:
        return to_utc(now)
    return datetime.now(timezone.utc)


def build_edit_event(
    payload: Mapping[str, Any],
    session_id: str,
    timestamp: datetime,
) -> EditEvent | None:
    """`tool_input` -> `EditEvent`, per the table in RFC 7.2. Tolerant of gaps."""
    tool_name = _str(payload.get(F_TOOL_NAME))
    if tool_name not in EDIT_TOOLS:
        if tool_name:
            log.debug("hook tool_name %r is not an edit tool; no EditEvent", tool_name)
        return None

    raw_input = payload.get(F_TOOL_INPUT)
    tool_input: Mapping[str, Any] = raw_input if isinstance(raw_input, Mapping) else {}
    if not isinstance(raw_input, Mapping) and raw_input is not None:
        log.debug("hook tool_input was %s, not an object", type(raw_input).__name__)

    written: list[str] = []
    removed: list[str] = []

    if tool_name == "Edit":
        file_path = _str(tool_input.get(I_FILE_PATH))
        written = _split(tool_input.get(I_NEW_STRING))
        removed = _split(tool_input.get(I_OLD_STRING))
    elif tool_name == "Write":
        file_path = _str(tool_input.get(I_FILE_PATH))
        written = _split(tool_input.get(I_CONTENT))
    elif tool_name == "MultiEdit":
        file_path = _str(tool_input.get(I_FILE_PATH))
        raw_edits = tool_input.get(I_EDITS)
        if isinstance(raw_edits, list):
            for entry in raw_edits:
                if not isinstance(entry, Mapping):
                    log.debug("skipping malformed MultiEdit entry")
                    continue
                written.extend(_split(entry.get(I_NEW_STRING)))
                removed.extend(_split(entry.get(I_OLD_STRING)))
        elif raw_edits is not None:
            log.debug("MultiEdit edits was %s, not a list", type(raw_edits).__name__)
    else:   # NotebookEdit
        file_path = _str(tool_input.get(I_NOTEBOOK_PATH))
        written = _split(tool_input.get(I_NEW_SOURCE))

    if not file_path:
        log.debug("hook %s payload carried no file path; no EditEvent", tool_name)
        return None

    return EditEvent(
        session_id=session_id,
        tool_use_id=_str(payload.get(F_TOOL_USE_ID)) or "",
        prompt_id=_str(payload.get(F_PROMPT_ID)),
        tool_name=tool_name,
        file_path=file_path,
        written_lines=written,
        removed_lines=removed,
        agent_id=_str(payload.get(F_AGENT_ID)),
        timestamp=timestamp,
    )


def parse_hook_payload(
    payload: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> HookResult:
    """One Claude Code hook payload -> HookResult. Never raises.

    `now` is optional and only exists so tests are deterministic; callers pass
    `parse_hook_payload(payload)` and get the receive time.
    """
    if not isinstance(payload, Mapping):
        log.debug("hook payload was not an object; ignored")
        return HookResult()

    meta: dict[str, Any] = {}
    for field_name, key in ((F_CWD, "cwd"), (F_REPO_ROOT, "repo_root"),
                            (F_PERMISSION_MODE, "permission_mode")):
        value = _str(payload.get(field_name))
        if value:
            meta[key] = value

    session_id = _str(payload.get(F_SESSION_ID))
    result = HookResult(
        hook_event_name=_str(payload.get(F_HOOK_EVENT_NAME)) or "",
        session_id=session_id,
        meta=meta,
        prompt_id=_str(payload.get(F_PROMPT_ID)),
        agent_id=_str(payload.get(F_AGENT_ID)),
        tool_name=_str(payload.get(F_TOOL_NAME)),
        tool_use_id=_str(payload.get(F_TOOL_USE_ID)),
    )
    if not session_id:
        log.debug("hook payload carried no session_id; nothing to key on")
        return result

    try:
        result.edit = build_edit_event(payload, session_id, _payload_time(payload, now))
    except Exception as exc:   # a malformed record is skipped, never a 5xx
        log.debug("could not build an EditEvent from a hook payload: %s", exc)
        result.edit = None
    return result
