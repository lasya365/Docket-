"""The adapter seam (RFC 7.1.4).

`ADAPTERS` is keyed by the value of the resource attribute `service.name`.
Only `claude-code` is registered; an unknown service is stored and never
assembled. Do not build a second adapter.
"""

from __future__ import annotations

from typing import Callable

from docket.collectors.adapters.claude_code import SERVICE_NAME, build_session

ADAPTERS: dict[str, Callable] = {
    SERVICE_NAME: build_session,
}

DEFAULT_SERVICE = SERVICE_NAME


def get_adapter(service_name: str | None) -> Callable | None:
    """The adapter for a `service.name`, or None when nothing is registered.

    An empty service name means the exporter did not say who it was; the
    telemetry Docket receives today is Claude Code's, so it takes the default.
    """
    if not service_name:
        return ADAPTERS.get(DEFAULT_SERVICE)
    return ADAPTERS.get(service_name.strip())
