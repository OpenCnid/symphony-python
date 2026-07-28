"""Symphony HTTP extension (SPEC 13.7) — OPTIONAL.

This package is an observability and control surface. Nothing here is required
for orchestrator correctness, and a fault inside it must never reach the
scheduling loop (SPEC 13.7, 14.2).

The extension is enabled by a CLI ``--port`` argument or a ``server.port`` key
in ``WORKFLOW.md`` front matter, with the CLI winning when both are present.
:func:`build_http_server` returns ``None`` when neither is set, which is the
normal not-enabled result rather than an error.
"""

from __future__ import annotations

from symphony.http.api import (
    ApiResponse,
    JsonApi,
    RefreshCoordinator,
    SnapshotSource,
    snapshot_source,
)
from symphony.http.dashboard import render_dashboard
from symphony.http.server import (
    DEFAULT_BIND_HOST,
    BindTarget,
    HttpServer,
    build_http_server,
    create_app,
    create_state_app,
    extension_enabled,
    resolve_bind,
    resolve_port,
)

__all__ = [
    "ApiResponse",
    "BindTarget",
    "DEFAULT_BIND_HOST",
    "HttpServer",
    "JsonApi",
    "RefreshCoordinator",
    "SnapshotSource",
    "build_http_server",
    "create_app",
    "create_state_app",
    "extension_enabled",
    "render_dashboard",
    "resolve_bind",
    "resolve_port",
    "snapshot_source",
]
