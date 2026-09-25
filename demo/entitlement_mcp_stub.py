#!/usr/bin/env python3
"""A stub MCP server named `entitlement_svc`, for the Docket permission demo.

This is DEMO SCAFFOLDING. It is not part of Docket and nothing in
`backend/src/docket/` imports it. Its only job is to exist so that a real Claude
Code session in the sample governed repo can really call `entitlement_svc.grant`,
so the call really appears in telemetry and Docket's blast-radius signal sees a
sensitive tool (`config.json` -> `sensitive.tools` matches `entitlement_svc.*`
and `*.grant`).

It grants nothing. `grant()` returns the string "granted (stub)" and touches no
system whatsoever.

Run it:

    ./.venv/bin/python demo/entitlement_mcp_stub.py          # stdio transport

Register it with Claude Code in the SAMPLE REPO (demo/sample-repo), not here:

    claude mcp add entitlement_svc -- /abs/path/to/.venv/bin/python \
        /abs/path/to/demo/entitlement_mcp_stub.py

or equivalently, `demo/sample-repo/.mcp.json`:

    {
      "mcpServers": {
        "entitlement_svc": {
          "command": "/abs/path/to/.venv/bin/python",
          "args": ["/abs/path/to/demo/entitlement_mcp_stub.py"]
        }
      }
    }

Claude Code then exposes the tool as `mcp__entitlement_svc__grant`; the Claude
Code adapter (RFC 6.x / 7.1) normalises that to `entitlement_svc.grant`.

Built against the installed `mcp` Python SDK 2.x, where FastMCP is `MCPServer`.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="entitlement_svc",
    instructions=(
        "Demo entitlement service. Grants nothing; every call is a no-op stub "
        "used to exercise Docket's sensitive-tool detection."
    ),
    version="0.1.0",
)


@server.tool(
    name="grant",
    title="Grant a role (stub)",
    description=(
        "Grant `role` to `user`. THIS IS A STUB: it changes nothing anywhere and "
        "always returns 'granted (stub)'."
    ),
)
def grant(user: str, role: str) -> str:
    """Pretend to grant `role` to `user`. Returns a fixed string."""
    return "granted (stub)"


if __name__ == "__main__":
    server.run(transport="stdio")
