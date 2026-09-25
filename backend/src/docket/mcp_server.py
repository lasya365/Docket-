"""The Docket MCP server (RFC section 13).

Read-only, over the same SQLite file, stdio transport. It makes Docket something
agents use, not only something that judges them: a board member's assistant can
ask why a change is held and get the evidence back.

Run it with:  ./.venv/bin/python -m docket.mcp_server
Register it with Claude Code:
  claude mcp add docket -- /abs/path/.venv/bin/python -m docket.mcp_server
"""

from __future__ import annotations

import logging
from typing import Any

from docket.settings import get_settings

log = logging.getLogger("docket.mcp")


def _store() -> Any:
    from docket.record.store import Store

    settings = get_settings()
    return Store(settings.path(settings.db_path))


# ---------------------------------------------------------------------------
# the three tools, as plain functions so they can be unit-tested with no SDK
# ---------------------------------------------------------------------------

def list_held(store: Any | None = None) -> list[dict]:
    """Held changes: change_key, title, composite, risk_level, hard stops."""
    store = store or _store()
    out: list[dict] = []
    for row in store.list_changes():
        if row.get("decision") != "HOLD":
            continue
        run = store.latest_run(row["change_key"])
        out.append(
            {
                "change_key": row["change_key"],
                "title": row.get("title"),
                "composite": row.get("composite"),
                "risk_level": row.get("risk_level"),
                "hard_stops": [
                    {"id": h.id, "reason": h.reason} for h in (run.decision.hard_stops_fired if run else [])
                ],
            }
        )
    return out


def get_change(change_key: str, store: Any | None = None) -> dict:
    """Decision, composite, the four signals, the chain, backout text, Freshservice id."""
    store = store or _store()
    run = store.latest_run(change_key)
    if run is None:
        return {"error": f"no change called {change_key}"}
    return {
        "change_key": run.change_key,
        "title": run.record.title,
        "kind": run.record.kind,
        "decision": run.decision.decision,
        "composite": run.decision.composite,
        "threshold": run.decision.threshold,
        "risk_level": run.decision.risk_level,
        "hard_stops": [{"id": h.id, "reason": h.reason} for h in run.decision.hard_stops_fired],
        "signals": [
            {
                "signal": s.signal,
                "label": s.label,
                "score": s.score,
                "status": s.status,
                "headline": s.headline,
                "summary": s.summary,
            }
            for s in run.signals
        ],
        "chain": [
            {"name": l.name, "present": l.present, "summary": l.summary} for l in run.chain.links
        ],
        "backout": run.backout.text,
        "freshservice_change_id": run.outputs.freshservice_change_id,
        "seal": run.seal,
    }


def explain_signal(change_key: str, signal: str, store: Any | None = None) -> dict:
    """That signal's full evidence list."""
    store = store or _store()
    run = store.latest_run(change_key)
    if run is None:
        return {"error": f"no change called {change_key}"}
    for s in run.signals:
        if s.signal == signal:
            return {
                "change_key": change_key,
                "signal": s.signal,
                "label": s.label,
                "score": s.score,
                "status": s.status,
                "headline": s.headline,
                "summary": s.summary,
                "evidence": [
                    {
                        "id": e.id,
                        "kind": e.kind,
                        "text": e.text,
                        "data": e.data,
                        "source": e.source,
                        "provenance": e.provenance,
                    }
                    for e in s.evidence
                ],
            }
    known = ", ".join(s.signal for s in run.signals)
    return {"error": f"no signal called {signal}. Known signals: {known}"}


TOOLS = {
    "list_held": {
        "description": "Every change Docket is currently holding, with its score and the hard stops that fired.",
        "schema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda args: list_held(),
    },
    "get_change": {
        "description": "The decision, the four signals, the evidence chain and the backout plan for one change.",
        "schema": {
            "type": "object",
            "properties": {"change_key": {"type": "string"}},
            "required": ["change_key"],
        },
        "fn": lambda args: get_change(args["change_key"]),
    },
    "explain_signal": {
        "description": "The full evidence list behind one signal of one change.",
        "schema": {
            "type": "object",
            "properties": {
                "change_key": {"type": "string"},
                "signal": {
                    "type": "string",
                    "enum": ["unattributed", "review_depth", "untested", "blast_radius"],
                },
            },
            "required": ["change_key", "signal"],
        },
        "fn": lambda args: explain_signal(args["change_key"], args["signal"]),
    },
}


def build_server():
    """Wire the three tools onto the installed MCP SDK.

    The installed SDK is mcp 2.2.0, where FastMCP was renamed to MCPServer and the
    decorator API is `@server.tool()`. Checked with the installed package, as RFC 13 asks.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="docket",
        version="3.0.0",
        instructions=(
            "Read-only access to Docket, the change-governance record. "
            "Ask list_held for what is currently blocked, get_change for one change's "
            "decision and evidence chain, explain_signal for the evidence behind one score."
        ),
    )

    @server.tool(name="list_held", description=TOOLS["list_held"]["description"])
    def docket_list_held() -> list[dict]:
        return list_held()

    @server.tool(name="get_change", description=TOOLS["get_change"]["description"])
    def docket_get_change(change_key: str) -> dict:
        return get_change(change_key)

    @server.tool(name="explain_signal", description=TOOLS["explain_signal"]["description"])
    def docket_explain_signal(change_key: str, signal: str) -> dict:
        return explain_signal(change_key, signal)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
