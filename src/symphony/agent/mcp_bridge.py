"""Provider-native tracker tools for Claude Code, over MCP (SPEC 10.5, 11.5).

SPEC 11.5 routes ticket mutations — state transitions, comments, PR links —
through host-side adapter tools rather than through the orchestrator, so that
*the coding agent never holds a tracker credential*. The Codex backend gets this
by advertising tool specs on the app-server session and executing calls in the
Symphony process. Claude Code has no equivalent channel: its only extension
point for host-supplied tools is MCP.

The transport choice is the whole design, and it is a security decision rather
than a convenience one:

**HTTP, not stdio.** An MCP stdio server configured via ``--mcp-config`` is
launched *by Claude Code* as its own child, so it would have to carry the
tracker credential itself — putting the token inside the agent's process tree,
which is exactly what SPEC 15.3 and the `secret_env_names` scrubbing exist to
prevent. An HTTP server hosted **in the Symphony process** inverts that: the
credential never moves, the agent gets a loopback URL, and tool results cross
the boundary instead of secrets.

The endpoint binds loopback on an ephemeral port and requires a per-session
bearer token. Loopback alone is not an authorization boundary — any local
process could otherwise mutate tracker state through it — and these tools are
explicitly allowed to write (``ToolSpec.mutates_tracker``).
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from symphony.trackers.base import ToolResult, ToolSpec

__all__ = [
    "TrackerToolBridge",
    "MCP_SERVER_NAME",
    "MCP_PROTOCOL_VERSION",
    "mcp_tool_name",
]

#: The MCP server name Claude Code namespaces tools under. A tool named
#: ``linear_set_issue_state`` is advertised to the model as
#: ``mcp__symphony__linear_set_issue_state``.
MCP_SERVER_NAME = "symphony"

#: Echoed back to the client when it does not request a specific version.
MCP_PROTOCOL_VERSION = "2025-06-18"

#: ``(name, arguments) -> ToolResult`` — the same executor the Codex backend
#: receives from ``agent/runner.py``.
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]


def mcp_tool_name(tool: str, server: str = MCP_SERVER_NAME) -> str:
    """Namespaced name Claude Code exposes an MCP tool under."""
    return f"mcp__{server}__{tool}"


class TrackerToolBridge:
    """In-process MCP server exposing one adapter's tools to Claude Code.

    Lifetime is one agent attempt: started before the first turn, stopped when
    the client stops. The bound issue travels as tool-execution context
    (SPEC 10.5) and never as tool input, so the model cannot retarget a
    mutation at another ticket by writing a different id.
    """

    def __init__(
        self,
        tool_specs: Sequence[ToolSpec],
        tool_executor: ToolExecutor,
        *,
        server_name: str = MCP_SERVER_NAME,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.tool_specs = list(tool_specs)
        self._executor = tool_executor
        self.server_name = server_name
        self._host = host
        self._requested_port = port
        self._token = secrets.token_urlsafe(32)
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None
        self._port: int | None = None
        #: Every tool call made through this bridge, for observability and tests.
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._port is not None

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def url(self) -> str:
        if self._port is None:
            raise RuntimeError("bridge is not running")
        return f"http://{self._host}:{self._port}/mcp"

    @property
    def token(self) -> str:
        return self._token

    def mcp_config(self) -> dict[str, Any]:
        """The ``--mcp-config`` payload Claude Code consumes."""
        return {
            "mcpServers": {
                self.server_name: {
                    "type": "http",
                    "url": self.url,
                    "headers": {"Authorization": f"Bearer {self._token}"},
                }
            }
        }

    def allowed_tool_patterns(self) -> list[str]:
        """Explicit ``--allowedTools`` entries for the advertised tools.

        Named individually rather than with a wildcard so a workflow's tool
        policy stays auditable: what the agent may call is a list, not a glob
        that silently widens when the adapter gains a tool.
        """
        return [mcp_tool_name(spec.name, self.server_name) for spec in self.tool_specs]

    async def start(self) -> None:
        """Bind loopback on an ephemeral port and serve until stopped."""
        if self.running or not self.tool_specs:
            return

        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Route

        app = Starlette(routes=[Route("/mcp", self._handle, methods=["POST", "GET", "DELETE"])])
        config = uvicorn.Config(
            app, host=self._host, port=self._requested_port, log_level="error", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._task = asyncio.ensure_future(self._server.serve())

        # Wait for the ephemeral port to be assigned before handing out a URL.
        for _ in range(200):
            servers = getattr(self._server, "servers", None)
            if servers:
                sockets = getattr(servers[0], "sockets", None)
                if sockets:
                    self._port = int(sockets[0].getsockname()[1])
                    return
            if self._task.done():  # pragma: no cover - bind failure
                await self._task
                raise RuntimeError("MCP bridge failed to start")
            await asyncio.sleep(0.02)
        raise TimeoutError("MCP bridge did not bind within the startup budget")

    async def stop(self) -> None:
        """Shut down. Idempotent (SPEC 16.5 calls cleanup on every exit path)."""
        server, task = self._server, self._task
        self._server = self._task = None
        self._port = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):  # pragma: no cover
                task.cancel()
            except Exception:  # pragma: no cover - shutdown must not raise
                pass

    # -- MCP protocol ------------------------------------------------------

    async def _handle(self, request: Any) -> Any:
        from starlette.responses import JSONResponse, Response

        auth = request.headers.get("authorization", "")
        expected = f"Bearer {self._token}"
        # Constant-time: this guards tracker *mutations* reachable from any
        # local process, so a timing oracle on the token is worth closing.
        if not secrets.compare_digest(auth, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(_error(None, -32700, "parse error"), status_code=400)

        method = str(body.get("method") or "")
        request_id = body.get("id")

        # Notifications carry no id and expect no result.
        if method.startswith("notifications/") or request_id is None:
            return Response(status_code=202)

        if method == "initialize":
            requested = (body.get("params") or {}).get("protocolVersion")
            return JSONResponse(
                _ok(
                    request_id,
                    {
                        "protocolVersion": requested or MCP_PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": self.server_name, "version": "1"},
                    },
                )
            )

        if method == "tools/list":
            return JSONResponse(_ok(request_id, {"tools": [_as_mcp(s) for s in self.tool_specs]}))

        if method == "tools/call":
            return JSONResponse(await self._call(request_id, body.get("params") or {}))

        if method == "ping":
            return JSONResponse(_ok(request_id, {}))

        return JSONResponse(_error(request_id, -32601, f"unknown method {method}"))

    async def _call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        raw_args = params.get("arguments")
        arguments = raw_args if isinstance(raw_args, dict) else {}
        self.calls.append((name, dict(arguments)))

        known = {spec.name for spec in self.tool_specs}
        if name not in known:
            # SPEC 10.5: an unsupported tool returns a structured failure and
            # the session continues; it never stalls or raises.
            failure = _tool_failure(f"unsupported tool: {name}", supported=sorted(known))
            return _ok(request_id, failure)

        try:
            result = await self._executor(name, dict(arguments))
        except Exception as exc:
            return _ok(request_id, _tool_failure(f"{type(exc).__name__}: {exc}"))

        ok = bool(getattr(result, "ok", False))
        content = getattr(result, "content", None)
        error = getattr(result, "error", None)
        payload = content if ok else {"error": error, "detail": content}
        return _ok(request_id, _tool_content(payload, is_error=not ok))


# --------------------------------------------------------------------------
# JSON-RPC / MCP payload helpers
# --------------------------------------------------------------------------


def _ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _as_mcp(spec: ToolSpec) -> dict[str, Any]:
    """Adapter tool spec -> MCP tool descriptor.

    The mutation flag is surfaced in the description rather than dropped: the
    model choosing between a read and a write should be able to tell which is
    which, and MCP has no first-class field for it.
    """
    description = spec.description
    if spec.mutates_tracker:
        description = f"{description} (mutates tracker state)"
    return {"name": spec.name, "description": description, "inputSchema": spec.input_schema}


def _tool_content(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    try:
        text = json.dumps(payload, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        text = str(payload)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _tool_failure(message: str, **extra: Any) -> dict[str, Any]:
    return _tool_content({"error": message, **extra}, is_error=True)
