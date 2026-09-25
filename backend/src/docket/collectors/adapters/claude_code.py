"""Claude Code telemetry -> SessionFragment (RFC 7.1.3).

This module is the ONLY place Claude Code attribute names may appear (RFC
section 0, rule 5). Everything below the mapping tables works on the constants
defined at the top of the file.

Assumptions taken here (rule 2 — simplest reading, written down):
  * A session with neither telemetry nor edits has nothing to assemble, so
    build_session returns None and the caller treats it as a session it never
    received (the Correlator's "ghost"). Rule 6 still holds one level up: the
    absence is a finding the pipeline records, not an exception. A session with
    edits but no telemetry, or telemetry but no edits, DOES yield a fragment —
    partial evidence is evidence.
  * A `tool_result` with no `tool_use_id` keeps its ToolCall under a synthetic
    id `"{session_id}#{n}"`, so blast-radius counting stays honest; nothing can
    join it to an edit, which is the truth of the record.
  * `git_branch` is `vcs.ref.head.name` when present, else
    `tool_parameters.git_branch`, mirroring the rule given for the commit SHA.
  * `decision_source` values outside the four the contract allows are dropped
    to None and logged at DEBUG.
  * `session_meta` may carry `permission_mode` (one value) or
    `permission_modes` (a list). Consecutive modes are turned into
    PermissionModeChange entries only by the telemetry event; the hook's mode
    list is session meta and is not invented into changes here.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Iterable, Mapping

from docket.collectors.otlp import EPOCH, FlatEvent, parse_iso, to_utc
from docket.models.fragments import (
    DetailLevel,
    EditEvent,
    PermissionModeChange,
    SessionFragment,
    ToolCall,
    ToolRejection,
    Turn,
)

log = logging.getLogger("docket.collectors.adapters.claude_code")

AGENT_NAME = "claude-code"
SERVICE_NAME = "claude-code"

# --- event names (RFC 7.1.3, after the `claude_code.` prefix is stripped) ---
EV_USER_PROMPT = "user_prompt"
EV_API_REQUEST = "api_request"
EV_API_ERROR = "api_error"
EV_TOOL_RESULT = "tool_result"
EV_TOOL_DECISION = "tool_decision"
EV_PERMISSION_MODE_CHANGED = "permission_mode_changed"

# --- attribute names -------------------------------------------------------
A_SESSION_ID = "session.id"
A_USER_EMAIL = "user.email"
A_APP_VERSION = "app.version"
A_REPO_URL = "vcs.repository.url.full"
A_PROMPT_ID = "prompt.id"
A_SEQUENCE = "event.sequence"

A_PROMPT = "prompt"
A_PROMPT_LENGTH = "prompt_length"
A_MODEL = "model"
A_ATTEMPT = "attempt"

A_TOOL_NAME = "tool_name"
A_TOOL_USE_ID = "tool_use_id"
A_SUCCESS = "success"
A_DURATION_MS = "duration_ms"
A_DECISION_SOURCE = "decision_source"
A_TOOL_PARAMETERS = "tool_parameters"
A_HEAD_REVISION = "vcs.ref.head.revision"
A_HEAD_NAME = "vcs.ref.head.name"

A_DECISION = "decision"
A_SOURCE = "source"
A_FROM_MODE = "from_mode"
A_TO_MODE = "to_mode"
A_TRIGGER = "trigger"

# --- keys inside the `tool_parameters` JSON string --------------------------
P_BASH_COMMAND = "bash_command"
P_FULL_COMMAND = "full_command"
P_DISABLE_SANDBOX = "dangerouslyDisableSandbox"
P_GIT_COMMIT_ID = "git_commit_id"
P_GIT_BRANCH = "git_branch"
P_MCP_SERVER_NAME = "mcp_server_name"
P_MCP_TOOL_NAME = "mcp_tool_name"

MCP_TOOL_SENTINEL = "mcp_tool"      # what tool_name always is for a user MCP server
REDACTED = "<REDACTED>"
REJECT_DECISION = "reject"

DECISION_SOURCES = {"config", "hook", "user_permanent", "user_temporary"}


# --------------------------------------------------------------------------- #
# small readers
# --------------------------------------------------------------------------- #


def _str(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value != "" else None
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _bool(value: Any, default: bool = False) -> bool:
    """Claude Code sends booleans as the strings "true"/"false"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def parse_tool_parameters(raw: Any) -> dict[str, Any]:
    """`tool_parameters` arrives as a JSON string. Tolerate anything else."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        log.debug("tool_parameters was not JSON; ignoring")
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _sequence(event: FlatEvent) -> int:
    """`event.sequence` for tie-breaking. Unreliable alone across resumes."""
    seq = _int(event.attrs.get(A_SEQUENCE))
    return seq if seq is not None else 0


def order_events(events: Iterable[Any]) -> list[FlatEvent]:
    """By timestamp, then by `event.sequence` for ties (RFC 7.1.3).

    Accepts `FlatEvent`s straight from `flatten()` or the plain
    `{name, timestamp, attrs}` rows the store hands back.
    """
    indexed = list(enumerate(FlatEvent.from_dict(e) for e in events))
    indexed.sort(key=lambda pair: (pair[1].timestamp, _sequence(pair[1]), pair[0]))
    return [event for _, event in indexed]


def display_name_for(tool_name: str | None, mcp_server: str | None, mcp_tool: str | None) -> str:
    """`"{mcp_server}.{mcp_tool}"` for MCP calls, otherwise `tool_name`."""
    if mcp_server and mcp_tool:
        return f"{mcp_server}.{mcp_tool}"
    if mcp_tool:
        return mcp_tool
    return tool_name or ""


# --------------------------------------------------------------------------- #
# the adapter
# --------------------------------------------------------------------------- #


def build_session(
    session_id: str,
    events: Iterable[Any],
    edit_events: Iterable[EditEvent] | None = None,
    session_meta: Mapping[str, Any] | None = None,
) -> SessionFragment | None:
    """Assemble one SessionFragment from stored telemetry, edits and hook meta.

    Returns None when there is nothing usable for this session — no telemetry
    and no edits — rather than raising.
    """
    ordered = order_events(events or [])
    edits = sorted(list(edit_events or []), key=lambda e: e.timestamp)
    meta: Mapping[str, Any] = session_meta or {}

    if not ordered and not edits:
        log.debug("no usable events for session %s", session_id)
        return None

    agent_version: str | None = None
    user_email: str | None = None
    repo_url: str | None = None
    models: list[str] = []
    api_retries = 0

    turns: list[Turn] = []
    turns_by_prompt: dict[str, Turn] = {}
    tool_calls: list[ToolCall] = []
    rejections: list[ToolRejection] = []
    mode_changes: list[PermissionModeChange] = []
    saw_tool_parameters = False

    edits_by_tool_use_id = {e.tool_use_id: e for e in edits if e.tool_use_id}

    for index, event in enumerate(ordered):
        attrs = event.attrs
        ts = to_utc(event.timestamp)

        # every event: fragment header
        agent_version = agent_version or _str(attrs.get(A_APP_VERSION))
        user_email = user_email or _str(attrs.get(A_USER_EMAIL))
        repo_url = repo_url or _str(attrs.get(A_REPO_URL))

        name = event.name
        if name == EV_USER_PROMPT:
            turn = _build_turn(attrs, ts)
            if turn is None:
                continue
            existing = turns_by_prompt.get(turn.prompt_id)
            if existing is None:
                turns.append(turn)
                turns_by_prompt[turn.prompt_id] = turn
            elif existing.prompt_text is None and turn.prompt_text is not None:
                existing.prompt_text = turn.prompt_text
                existing.prompt_length = turn.prompt_length or existing.prompt_length

        elif name == EV_API_REQUEST:
            model = _str(attrs.get(A_MODEL))
            if model and model not in models:
                models.append(model)

        elif name == EV_API_ERROR:
            attempt = _int(attrs.get(A_ATTEMPT))
            if attempt is not None:
                api_retries += max(attempt - 1, 0)

        elif name == EV_TOOL_RESULT:
            params = parse_tool_parameters(attrs.get(A_TOOL_PARAMETERS))
            if params:
                saw_tool_parameters = True
            call = _build_tool_call(session_id, index, attrs, params, ts, edits_by_tool_use_id)
            tool_calls.append(call)
            if call.prompt_id:
                turn = turns_by_prompt.get(call.prompt_id)
                if turn is not None and call.tool_use_id not in turn.tool_use_ids:
                    turn.tool_use_ids.append(call.tool_use_id)

        elif name == EV_TOOL_DECISION:
            decision = (_str(attrs.get(A_DECISION)) or "").strip().lower()
            if decision != REJECT_DECISION:
                continue
            rejections.append(
                ToolRejection(
                    tool_use_id=_str(attrs.get(A_TOOL_USE_ID)) or f"{session_id}#{index}",
                    name=_str(attrs.get(A_TOOL_NAME)) or "",
                    source=_str(attrs.get(A_SOURCE)) or "",
                    timestamp=ts,
                )
            )

        elif name == EV_PERMISSION_MODE_CHANGED:
            mode_changes.append(
                PermissionModeChange(
                    from_mode=_str(attrs.get(A_FROM_MODE)) or "",
                    to_mode=_str(attrs.get(A_TO_MODE)) or "",
                    trigger=_str(attrs.get(A_TRIGGER)),
                    timestamp=ts,
                )
            )

        elif name:
            log.debug("unknown claude_code event %r, keeping the rest", name)

    # Edits whose tool_result never arrived still name their turn.
    for edit in edits:
        if edit.prompt_id and edit.prompt_id in turns_by_prompt:
            turn = turns_by_prompt[edit.prompt_id]
            if edit.tool_use_id and edit.tool_use_id not in turn.tool_use_ids:
                turn.tool_use_ids.append(edit.tool_use_id)

    stamps = [e.timestamp for e in ordered] + [e.timestamp for e in edits]
    started_at = min(stamps) if stamps else _meta_time(meta) or EPOCH
    ended_at = max(stamps) if stamps else started_at

    detail = DetailLevel(
        prompts=any(t.prompt_text for t in turns),
        tool_details=saw_tool_parameters,
        edits=bool(edits),
    )

    return SessionFragment(
        session_id=session_id,
        agent=AGENT_NAME,
        agent_version=agent_version,
        models=models,
        user_email=user_email or _str(meta.get("user_email")),
        repo_url=repo_url,
        repo_root=_str(meta.get("repo_root")) or _str(meta.get("cwd")),
        started_at=started_at,
        ended_at=ended_at,
        turns=turns,
        tool_calls=tool_calls,
        rejections=rejections,
        permission_mode_changes=mode_changes,
        api_retries=api_retries,
        edit_events=edits,
        detail=detail,
        provenance="real",
    )


def _meta_time(meta: Mapping[str, Any]) -> datetime | None:
    for key in ("started_at", "first_seen"):
        value = meta.get(key)
        if isinstance(value, datetime):
            return to_utc(value)
        parsed = parse_iso(value)
        if parsed is not None:
            return parsed
    return None


def _build_turn(attrs: Mapping[str, Any], ts: datetime) -> Turn | None:
    prompt_id = _str(attrs.get(A_PROMPT_ID))
    if not prompt_id:
        log.debug("user_prompt with no prompt.id, skipped")
        return None
    raw_prompt = attrs.get(A_PROMPT)
    text = _str(raw_prompt)
    # A redacted prompt arrives absent or as the literal <REDACTED>.
    if text is not None and text.strip() == REDACTED:
        text = None
    length = _int(attrs.get(A_PROMPT_LENGTH))
    if length is None:
        length = len(text) if text else 0
    return Turn(
        prompt_id=prompt_id,
        started_at=ts,
        prompt_text=text,
        prompt_length=max(length, 0),
        tool_use_ids=[],
    )


def _build_tool_call(
    session_id: str,
    index: int,
    attrs: Mapping[str, Any],
    params: Mapping[str, Any],
    ts: datetime,
    edits_by_tool_use_id: Mapping[str, EditEvent],
) -> ToolCall:
    tool_name = _str(attrs.get(A_TOOL_NAME)) or ""
    tool_use_id = _str(attrs.get(A_TOOL_USE_ID))
    if not tool_use_id:
        log.debug("tool_result with no tool_use_id; using a synthetic id")
        tool_use_id = f"{session_id}#{index}"

    mcp_server = _str(params.get(P_MCP_SERVER_NAME))
    mcp_tool = _str(params.get(P_MCP_TOOL_NAME))
    # A user-configured MCP server always reports tool_name == "mcp_tool".
    if tool_name == MCP_TOOL_SENTINEL and not mcp_tool:
        log.debug("mcp_tool call without mcp_tool_name; display_name stays raw")

    bash_command = _str(params.get(P_FULL_COMMAND)) or _str(params.get(P_BASH_COMMAND))

    decision_source = _str(attrs.get(A_DECISION_SOURCE))
    if decision_source is not None and decision_source not in DECISION_SOURCES:
        log.debug("unknown decision_source %r, dropped", decision_source)
        decision_source = None

    git_commit_sha = _str(attrs.get(A_HEAD_REVISION)) or _str(params.get(P_GIT_COMMIT_ID))
    git_branch = _str(attrs.get(A_HEAD_NAME)) or _str(params.get(P_GIT_BRANCH))

    edit = edits_by_tool_use_id.get(tool_use_id)
    file_path = edit.file_path if edit is not None else None

    return ToolCall(
        tool_use_id=tool_use_id,
        prompt_id=_str(attrs.get(A_PROMPT_ID)),
        name=tool_name,
        display_name=display_name_for(tool_name, mcp_server, mcp_tool),
        mcp_server=mcp_server,
        mcp_tool=mcp_tool,
        bash_command=bash_command,
        file_path=file_path,
        success=_bool(attrs.get(A_SUCCESS), default=True),
        duration_ms=_int(attrs.get(A_DURATION_MS)),
        decision_source=decision_source,
        sandbox_disabled=_bool(params.get(P_DISABLE_SANDBOX), default=False),
        git_commit_sha=git_commit_sha,
        git_branch=git_branch,
        timestamp=ts,
    )
