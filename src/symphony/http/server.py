"""OPTIONAL HTTP server extension — SPEC 13.7.

Assembles the SPEC 13.7.1 dashboard and the SPEC 13.7.2 JSON API into one ASGI
application, and owns the bind policy and process lifecycle.

Framing constraint, which outranks the endpoint list: this is an
observability/control surface and MUST NOT become REQUIRED for orchestrator
correctness (SPEC 13.7). Two consequences are visible throughout this module:

* Nothing here can crash the host. Handler exceptions, snapshot provider faults
  and dashboard render errors all become HTTP responses (SPEC 14.2).
* The listener is created once and never rebound. SPEC 6.2 explicitly permits
  restart-required behaviour for extension-owned listeners, so there is no
  hot-rebind machinery to go wrong during a workflow reload.

Routing note: the two literal ``/api/v1`` routes are resolved before any path is
read as an issue identifier, so ``/api/v1/refresh`` is never shadowed and always
answers ``405`` — not ``404`` — for a wrong verb.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from symphony.errors import ConfigValidationError
from symphony.http.api import (
    API_PREFIX,
    ApiResponse,
    JsonApi,
    RefreshCoordinator,
    SnapshotSource,
    error_payload,
    snapshot_source,
)
from symphony.http.dashboard import (
    DEFAULT_REFRESH_SECONDS,
    render_dashboard,
    render_error_page,
)

__all__ = [
    "DASHBOARD_METHODS",
    "DEFAULT_BIND_HOST",
    "BindTarget",
    "HttpServer",
    "build_http_server",
    "create_app",
    "create_state_app",
    "extension_enabled",
    "open_listener",
    "resolve_bind",
    "resolve_port",
]

# SPEC 13.7: "Implementations SHOULD bind loopback by default (127.0.0.1 or host
# equivalent) unless explicitly configured otherwise."
DEFAULT_BIND_HOST = "127.0.0.1"

DASHBOARD_METHODS = ("GET", "HEAD")

_MAX_PORT = 65535
_ALL_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_STARTUP_POLL_SECONDS = 0.005
_STARTUP_TIMEOUT_SECONDS = 30.0
_SHUTDOWN_TIMEOUT_SECONDS = 10.0

Clock = Callable[[], datetime]


# --------------------------------------------------------------------------
# Bind policy (SPEC 13.7 "Extension config" / "Enablement")
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BindTarget:
    """A resolved listen address."""

    host: str
    port: int

    @property
    def is_ephemeral(self) -> bool:
        """SPEC 13.7: ``0`` requests an ephemeral port."""
        return self.port == 0

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "::1", "localhost"}

    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def _validate_port(port: int, *, origin: str) -> int:
    if isinstance(port, bool) or not isinstance(port, int):
        raise ConfigValidationError(
            f"{origin} must be an integer", origin=origin, kind=type(port).__name__
        )
    if port < 0 or port > _MAX_PORT:
        raise ConfigValidationError(
            f"{origin} must be between 0 and {_MAX_PORT}", origin=origin, port=port
        )
    return port


def resolve_port(cli_port: int | None = None, config_port: int | None = None) -> int | None:
    """Apply the SPEC 13.7 port precedence.

    CLI ``--port`` overrides ``server.port`` when both are present. ``None``
    means the extension is not enabled: neither source supplied a port.

    ``0`` is a real value, not "absent" — it requests an ephemeral port — so the
    precedence test is ``is not None``, never truthiness.
    """
    if cli_port is not None:
        return _validate_port(cli_port, origin="--port")
    if config_port is not None:
        return _validate_port(config_port, origin="server.port")
    return None


def resolve_bind(
    cli_port: int | None = None,
    config_port: int | None = None,
    *,
    host: str | None = None,
) -> BindTarget | None:
    """Resolve the listener, or ``None`` when the extension is not enabled.

    SPEC 13.7 enablement: start the server when CLI ``--port`` is provided, or
    when ``server.port`` is present in ``WORKFLOW.md`` front matter. The default
    bind host is loopback unless a host is explicitly configured.
    """
    port = resolve_port(cli_port, config_port)
    if port is None:
        return None
    resolved_host = host.strip() if isinstance(host, str) and host.strip() else DEFAULT_BIND_HOST
    return BindTarget(host=resolved_host, port=port)


def extension_enabled(cli_port: int | None = None, config_port: int | None = None) -> bool:
    """SPEC 13.7 enablement predicate, for callers that only need the yes/no."""
    return resolve_port(cli_port, config_port) is not None


def open_listener(bind: BindTarget, *, backlog: int = 128) -> socket.socket:
    """Bind and listen, resolving an ephemeral port immediately.

    The socket is created here rather than inside uvicorn so an ephemeral
    (port ``0``) request has a knowable port the moment ``start()`` returns —
    no polling the server for its address, and no race in tests.
    """
    family = socket.AF_INET6 if ":" in bind.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            # On Windows SO_REUSEADDR permits stealing a live listener, so it is
            # deliberately not set there.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind.host, bind.port))
        sock.listen(backlog)
    except OSError:
        sock.close()
        raise
    return sock


def _bound_target(sock: socket.socket) -> BindTarget:
    host, port = sock.getsockname()[:2]
    return BindTarget(host=str(host), port=int(port))


# --------------------------------------------------------------------------
# ASGI application (SPEC 13.7.1 + 13.7.2)
# --------------------------------------------------------------------------


def _to_response(reply: ApiResponse, *, method: str) -> Response:
    try:
        response = JSONResponse(
            reply.payload, status_code=reply.status, headers=reply.headers or None
        )
    except (TypeError, ValueError) as exc:
        # A snapshot provider can hand us a value json cannot encode. That is an
        # observability fault, not an orchestrator fault (SPEC 14.1 class 5), so
        # it becomes a response rather than an unhandled ASGI exception.
        payload = error_payload("internal_error", f"response is not JSON-serializable: {exc}")
        response = JSONResponse(payload, status_code=500)
    if method == "HEAD":
        # Preserve status and headers, drop the body.
        response.body = b""
    return response


def _request_path(request: Request) -> str:
    """The already-percent-decoded path, per the ASGI spec.

    Read from the raw scope rather than ``request.url`` so an identifier
    containing ``/``, a space or ``#`` survives verbatim instead of being
    re-parsed as URL structure.
    """
    return str(request.scope.get("path", "/"))


def create_app(
    source: SnapshotSource,
    *,
    refresh: RefreshCoordinator | None = None,
    on_refresh: Callable[[], Any] | None = None,
    clock: Clock | None = None,
    dashboard_refresh_seconds: int | None = DEFAULT_REFRESH_SECONDS,
) -> Starlette:
    """Build the ASGI app: dashboard at ``/``, JSON API under ``/api/v1``.

    A :class:`~symphony.http.api.RefreshCoordinator` is created when none is
    supplied, so ``POST /api/v1/refresh`` always has somewhere to record the
    request; reach it afterwards via ``app.state.refresh``.
    """
    coordinator = refresh if refresh is not None else RefreshCoordinator()
    api = JsonApi(source, refresh=coordinator, on_refresh=on_refresh, clock=clock)

    async def dashboard_endpoint(request: Request) -> Response:
        """SPEC 13.7.1. Render failures degrade to a page, never an exception."""
        method = request.method
        if method not in DASHBOARD_METHODS:
            reply = api.method_not_allowed(method, DASHBOARD_METHODS)
            return _to_response(reply, method=method)
        try:
            snapshot = source.snapshot()
            html = render_dashboard(snapshot, refresh_seconds=dashboard_refresh_seconds)
            status = 200
        except Exception as exc:  # SPEC 14.2: dashboard failures must not crash
            html = render_error_page(f"{type(exc).__name__}: {exc}")
            status = 503
        response = HTMLResponse(html, status_code=status)
        if method == "HEAD":
            # RFC 7231: HEAD keeps the Content-Length GET would have sent.
            response.body = b""
        return response

    async def api_endpoint(request: Request) -> Response:
        """SPEC 13.7.2 dispatch. See :func:`symphony.http.api.api_target`."""
        method = request.method
        reply = await api.handle(method, _request_path(request))
        return _to_response(reply, method=method)

    async def catch_all(request: Request) -> Response:
        path = _request_path(request)
        if path == "/":
            return await dashboard_endpoint(request)
        if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
            return await api_endpoint(request)
        reply = api.not_found(path)
        return _to_response(reply, method=request.method)

    async def on_unhandled(request: Request, exc: Exception) -> Response:
        """Last line of defence (SPEC 14.2).

        Every reachable path above already converts faults into responses; this
        exists so that a future one that does not still cannot escape as an
        unhandled ASGI exception.
        """
        payload = error_payload("internal_error", f"{type(exc).__name__}: {exc}")
        return JSONResponse(payload, status_code=500)

    app = Starlette(
        routes=[Route("/{path:path}", catch_all, methods=list(_ALL_METHODS))],
        exception_handlers={Exception: on_unhandled},
    )
    app.state.api = api
    app.state.refresh = coordinator
    app.state.source = source
    return app


def create_state_app(state: Any, **kwargs: Any) -> Starlette:
    """Convenience wiring against a live :class:`OrchestratorState`."""
    return create_app(snapshot_source(state), **kwargs)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def _hosted_server(config: Any) -> Any:
    """Instantiate uvicorn with signal capture disabled.

    ``uvicorn.Server.serve`` installs SIGINT/SIGTERM handlers when it runs on the
    main thread. Symphony's host process owns shutdown (SPEC 17.7), and an
    OPTIONAL observability extension must not take that over.

    The class is built on first use so importing this module does not import
    uvicorn.
    """
    global _HOSTED_SERVER_CLASS
    if _HOSTED_SERVER_CLASS is None:
        import uvicorn

        class _Hosted(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self) -> Any:
                yield

        _HOSTED_SERVER_CLASS = _Hosted
    return _HOSTED_SERVER_CLASS(config)


_HOSTED_SERVER_CLASS: Any = None


class HttpServer:
    """Runs the ASGI app on a pre-bound socket for the lifetime of the host.

    ``start()`` returns once the listener is accepting; :attr:`bound` is valid
    immediately after, including for ephemeral (port ``0``) binds.

    No hot-rebind: SPEC 6.2 permits extension-owned listeners to require a
    restart, so a port change means a new :class:`HttpServer`.
    """

    __slots__ = ("_app", "_bind", "_bound", "_log_level", "_server", "_socket", "_task")

    def __init__(self, app: Any, bind: BindTarget, *, log_level: str = "warning") -> None:
        self._app = app
        self._bind = bind
        self._log_level = log_level
        self._socket: socket.socket | None = None
        self._bound: BindTarget | None = None
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None

    @property
    def app(self) -> Any:
        return self._app

    @property
    def requested(self) -> BindTarget:
        """What was asked for — port may be ``0``."""
        return self._bind

    @property
    def bound(self) -> BindTarget | None:
        """What was actually bound, with the ephemeral port resolved."""
        return self._bound

    @property
    def port(self) -> int:
        if self._bound is None:
            raise RuntimeError("HttpServer is not started")
        return self._bound.port

    @property
    def base_url(self) -> str:
        if self._bound is None:
            raise RuntimeError("HttpServer is not started")
        return self._bound.url()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Bind, then serve until :meth:`stop`."""
        if self._task is not None:
            raise RuntimeError("HttpServer already started")

        import uvicorn

        sock = open_listener(self._bind)
        self._socket = sock
        self._bound = _bound_target(sock)

        config = uvicorn.Config(
            self._app,
            log_level=self._log_level,
            # The orchestrator owns logging (SPEC 13.1/13.2); do not let uvicorn
            # reconfigure the process-wide logging tree.
            log_config=None,
            access_log=False,
            lifespan="off",
        )
        server = _hosted_server(config)
        self._server = server
        self._task = asyncio.get_running_loop().create_task(server.serve(sockets=[sock]))

        deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT_SECONDS
        while not server.started:
            if self._task.done():
                await self._task  # re-raise the startup failure
                raise RuntimeError("HTTP server exited before it started")
            if asyncio.get_running_loop().time() > deadline:
                await self.stop()
                raise RuntimeError(f"HTTP server failed to start on {self._bind}")
            await asyncio.sleep(_STARTUP_POLL_SECONDS)

    async def stop(self) -> None:
        """Ask uvicorn to exit, then release the listener. Idempotent."""
        if self._server is not None:
            self._server.should_exit = True
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except asyncio.CancelledError:  # pragma: no cover - shutdown race
                raise
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
            self._socket = None
        self._server = None

    async def aclose(self) -> None:
        await self.stop()

    async def __aenter__(self) -> HttpServer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()


# --------------------------------------------------------------------------
# One-call assembly for the CLI / orchestrator host
# --------------------------------------------------------------------------


def build_http_server(
    source: SnapshotSource,
    *,
    cli_port: int | None = None,
    config_port: int | None = None,
    host: str | None = None,
    refresh: RefreshCoordinator | None = None,
    on_refresh: Callable[[], Any] | None = None,
    clock: Clock | None = None,
    dashboard_refresh_seconds: int | None = DEFAULT_REFRESH_SECONDS,
    log_level: str = "warning",
) -> HttpServer | None:
    """Build the extension, or return ``None`` when it is not enabled.

    ``None`` is the normal, non-exceptional result for a deployment that
    configured neither CLI ``--port`` nor ``server.port`` (SPEC 13.7).
    """
    bind = resolve_bind(cli_port, config_port, host=host)
    if bind is None:
        return None
    app = create_app(
        source,
        refresh=refresh,
        on_refresh=on_refresh,
        clock=clock,
        dashboard_refresh_seconds=dashboard_refresh_seconds,
    )
    return HttpServer(app, bind, log_level=log_level)
