"""JSON REST API for the OPTIONAL HTTP server extension — SPEC 13.7.2.

This module is deliberately transport-free. Every endpoint is a plain function
returning an :class:`ApiResponse` (status + JSON-safe payload + headers), so the
route table in :mod:`symphony.http.server` is a thin adapter and the behaviour
can be exercised — and driven from an RLM's REPL — without an ASGI stack.

Framing constraint (SPEC 13.7): the API is an observability/control surface and
MUST NOT become REQUIRED for orchestrator correctness. Concretely, every read
path here treats the snapshot provider as untrusted: a provider that raises is
reported as ``snapshot_unavailable``/``snapshot_timeout`` (SPEC 13.3 error
modes) rather than propagating and taking the service down (SPEC 14.2).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "API_PREFIX",
    "ROUTE_STATE",
    "ROUTE_REFRESH",
    "ApiResponse",
    "ApiTarget",
    "JsonApi",
    "RefreshCoordinator",
    "RefreshResult",
    "SnapshotSource",
    "api_target",
    "error_payload",
    "error_response",
    "normalize_issue_payload",
    "normalize_state_payload",
    "rfc3339",
    "snapshot_source",
]

# SPEC 13.7.2 — the API lives under this prefix. The two literal routes are
# matched before anything is treated as an issue identifier, so a ticket keyed
# "state" or "refresh" cannot shadow them and they cannot be shadowed by it.
API_PREFIX = "/api/v1"
ROUTE_STATE = f"{API_PREFIX}/state"
ROUTE_REFRESH = f"{API_PREFIX}/refresh"

_READ_METHODS = ("GET", "HEAD")
_WRITE_METHODS = ("POST",)

# SPEC 13.7.2: "Queues an immediate tracker poll + reconciliation cycle".
DEFAULT_REFRESH_OPERATIONS = ("poll", "reconcile")

_ERROR_DETAIL_LIMIT = 200

SnapshotFn = Callable[[], Mapping[str, Any]]
IssueDetailFn = Callable[[str], Mapping[str, Any] | None]
Clock = Callable[[], datetime]
RefreshHook = Callable[[], Any]


def rfc3339(moment: datetime) -> str:
    """Render a timestamp the way the SPEC 13.7.2 examples do (``...Z``).

    Naive datetimes are read as UTC; aware ones are converted. Sub-second
    precision is dropped so the wire format matches the spec samples exactly.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    text = moment.astimezone(UTC).replace(microsecond=0).isoformat()
    return text.replace("+00:00", "Z")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _detail(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    if len(text) > _ERROR_DETAIL_LIMIT:
        return text[: _ERROR_DETAIL_LIMIT - 1] + "…"
    return text


# --------------------------------------------------------------------------
# Responses and the error envelope (SPEC 13.7.2 "API design notes")
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """One JSON reply: HTTP status, payload, and extra response headers."""

    status: int
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def error_payload(code: str, message: str) -> dict[str, Any]:
    """SPEC 13.7.2: errors use the ``{"error":{"code","message"}}`` envelope."""
    return {"error": {"code": code, "message": message}}


def error_response(code: str, message: str, *, status: int, **headers: str) -> ApiResponse:
    return ApiResponse(status=status, payload=error_payload(code, message), headers=dict(headers))


def _snapshot_failure(exc: BaseException) -> ApiResponse:
    """Map a misbehaving snapshot provider onto SPEC 13.3's error modes."""
    if isinstance(exc, TimeoutError):
        return error_response(
            "snapshot_timeout",
            f"runtime snapshot timed out ({_detail(exc)})",
            status=504,
        )
    return error_response(
        "snapshot_unavailable",
        f"runtime snapshot unavailable ({_detail(exc)})",
        status=503,
    )


# --------------------------------------------------------------------------
# Where runtime state comes from
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotSource:
    """The two reads this surface needs, injected rather than imported.

    Production wiring is :func:`snapshot_source`, which binds the SPEC 13.3
    builders in ``symphony.observability.snapshot`` to a live
    :class:`~symphony.models.OrchestratorState`. Tests supply their own pair.
    """

    snapshot: SnapshotFn
    issue_detail: IssueDetailFn


def snapshot_source(state: Any) -> SnapshotSource:
    """Bind ``symphony.observability.snapshot`` to a live orchestrator state.

    The import is deferred to call time on purpose: the HTTP extension must be
    importable (and unit-testable) whether or not the observability module is
    present, and a dashboard dependency must never be able to break service
    startup (SPEC 13.7, 14.2).
    """
    from symphony.observability.snapshot import build_issue_detail, build_snapshot

    return SnapshotSource(
        snapshot=lambda: build_snapshot(state),
        issue_detail=lambda identifier: build_issue_detail(state, identifier),
    )


# --------------------------------------------------------------------------
# POST /api/v1/refresh plumbing
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Outcome of a refresh request (SPEC 13.7.2 ``202`` body)."""

    queued: bool = True
    coalesced: bool = False
    operations: tuple[str, ...] = DEFAULT_REFRESH_OPERATIONS

    def to_payload(self, requested_at: str) -> dict[str, Any]:
        return {
            "queued": self.queued,
            "coalesced": self.coalesced,
            "requested_at": requested_at,
            "operations": list(self.operations),
        }


class RefreshCoordinator:
    """Best-effort poll+reconcile trigger with exact coalescing (SPEC 13.7.2).

    The spec permits implementations to coalesce repeated requests. This does so
    against a real handoff rather than a time window: :meth:`request` raises a
    pending flag, and repeated requests made before the orchestrator calls
    :meth:`consume` collapse into that one pending refresh and are reported
    ``coalesced: true``.

    The orchestrator side is two calls — :meth:`wait` to be woken, or
    :meth:`consume` to poll the flag at the top of a tick. Neither is REQUIRED:
    an orchestrator that ignores this object entirely still runs correctly, the
    refresh trigger simply becomes a no-op.

    Single-event-loop object; :meth:`request` and :meth:`wait` must run on the
    same loop (the orchestrator has exactly one).
    """

    __slots__ = ("_coalesced", "_event", "_pending", "_requests")

    def __init__(self) -> None:
        self._pending = False
        self._requests = 0
        self._coalesced = 0
        # Created lazily: constructing the coordinator must not require a
        # running loop, so an orchestrator can build one during setup.
        self._event: Any = None

    def _ensure_event(self) -> Any:
        if self._event is None:
            import asyncio

            self._event = asyncio.Event()
        return self._event

    @property
    def pending(self) -> bool:
        """True when a requested refresh has not yet been consumed."""
        return self._pending

    @property
    def request_count(self) -> int:
        return self._requests

    @property
    def coalesced_count(self) -> int:
        return self._coalesced

    def request(self) -> RefreshResult:
        """Queue a refresh; coalesce onto an already-pending one."""
        already = self._pending
        self._pending = True
        self._requests += 1
        if already:
            self._coalesced += 1
        self._ensure_event().set()
        return RefreshResult(queued=True, coalesced=already)

    def consume(self) -> bool:
        """Orchestrator side: take the pending refresh, if any."""
        was_pending = self._pending
        self._pending = False
        if self._event is not None:
            self._event.clear()
        return was_pending

    async def wait(self) -> None:
        """Orchestrator side: block until a refresh is pending."""
        event = self._ensure_event()
        while not self._pending:
            event.clear()
            await event.wait()


# --------------------------------------------------------------------------
# Payload normalization
# --------------------------------------------------------------------------


def normalize_state_payload(raw: Mapping[str, Any], *, generated_at: str) -> dict[str, Any]:
    """Guarantee the SPEC 13.7.2 ``/state`` keys without discarding extras.

    The snapshot builder is a sibling module and MAY add fields (the spec
    explicitly allows it); those pass through untouched. Only the baseline keys
    are backfilled, so the documented shape holds even if the provider is
    partial.
    """
    out: dict[str, Any] = dict(raw)

    running = list(out.get("running") or [])
    retrying = list(out.get("retrying") or [])
    out["running"] = running
    out["retrying"] = retrying

    counts: dict[str, Any] = dict(out.get("counts") or {})
    counts.setdefault("running", len(running))
    counts.setdefault("retrying", len(retrying))
    out["counts"] = counts

    out.setdefault("generated_at", generated_at)
    out.setdefault(
        "codex_totals",
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "seconds_running": 0.0},
    )
    out.setdefault("rate_limits", None)
    return out


def normalize_issue_payload(raw: Mapping[str, Any], *, identifier: str) -> dict[str, Any]:
    """Guarantee the SPEC 13.7.2 ``/<issue_identifier>`` baseline keys."""
    out: dict[str, Any] = dict(raw)
    out.setdefault("issue_identifier", identifier)
    out.setdefault("issue_id", None)
    out.setdefault("status", "unknown")
    out.setdefault("running", None)
    out.setdefault("retry", None)
    out.setdefault("recent_events", [])
    out.setdefault("last_error", None)
    return out


# --------------------------------------------------------------------------
# Route resolution (SPEC 13.7.2)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApiTarget:
    """A resolved ``/api/v1`` route."""

    name: str
    allowed_methods: tuple[str, ...]
    identifier: str | None = None


def api_target(path: str) -> ApiTarget | None:
    """Classify an already-percent-decoded request path (SPEC 13.7.2).

    ``None`` means "no such API route" (a 404), which is distinct from "route
    exists, wrong verb" (a 405) — the caller needs both.

    The literals are tested first so ``/api/v1/refresh`` can never be read as an
    issue identifier. Everything else after the prefix is the identifier
    *verbatim*: the ASGI server hands us the decoded path, so ``MT%2F649``
    arrives as ``MT/649`` and stays one identifier rather than becoming a nested
    route. Human ticket keys with spaces, ``#`` or ``/`` therefore round-trip.
    """
    if path == ROUTE_STATE:
        return ApiTarget("state", _READ_METHODS)
    if path == ROUTE_REFRESH:
        return ApiTarget("refresh", _WRITE_METHODS)
    if not path.startswith(API_PREFIX + "/"):
        return None
    identifier = path[len(API_PREFIX) + 1 :]
    if not identifier:
        return None
    return ApiTarget("issue", _READ_METHODS, identifier=identifier)


# --------------------------------------------------------------------------
# The endpoints
# --------------------------------------------------------------------------


class JsonApi:
    """SPEC 13.7.2 endpoints as transport-free handlers.

    Construct with a :class:`SnapshotSource`; optionally pass a
    :class:`RefreshCoordinator` and/or an ``on_refresh`` hook to make
    ``POST /api/v1/refresh`` do something. With neither wired, refresh reports
    ``503 refresh_unavailable`` rather than claiming a queue that does not exist.
    """

    __slots__ = ("_clock", "_hook", "_refresh", "_source")

    def __init__(
        self,
        source: SnapshotSource,
        *,
        refresh: RefreshCoordinator | None = None,
        on_refresh: RefreshHook | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._source = source
        self._refresh = refresh
        self._hook = on_refresh
        self._clock: Clock = clock or _utc_now

    @property
    def source(self) -> SnapshotSource:
        return self._source

    @property
    def coordinator(self) -> RefreshCoordinator | None:
        return self._refresh

    def now(self) -> str:
        return rfc3339(self._clock())

    # -- GET /api/v1/state -------------------------------------------------

    def state(self) -> ApiResponse:
        """SPEC 13.7.2: summary view of current runtime state."""
        raw: Any
        try:
            # Typed as a mapping, checked as one anyway: the provider is a
            # sibling module and this surface must not trust its annotations.
            raw = self._source.snapshot()
        except Exception as exc:  # SPEC 14.2: never let a read take the host down
            return _snapshot_failure(exc)
        if not isinstance(raw, Mapping):
            return error_response(
                "snapshot_unavailable",
                f"snapshot provider returned {type(raw).__name__}, expected a mapping",
                status=503,
            )
        return ApiResponse(200, normalize_state_payload(raw, generated_at=self.now()))

    # -- GET /api/v1/<issue_identifier> ------------------------------------

    def issue(self, identifier: str) -> ApiResponse:
        """SPEC 13.7.2: per-issue runtime/debug details, or ``404``."""
        raw: Any
        try:
            raw = self._source.issue_detail(identifier)
        except Exception as exc:  # SPEC 14.2
            return _snapshot_failure(exc)
        if raw is None:
            return error_response(
                "issue_not_found",
                f"issue {identifier!r} is not present in current in-memory state",
                status=404,
            )
        if not isinstance(raw, Mapping):
            return error_response(
                "snapshot_unavailable",
                f"issue detail provider returned {type(raw).__name__}, expected a mapping",
                status=503,
            )
        return ApiResponse(200, normalize_issue_payload(raw, identifier=identifier))

    # -- POST /api/v1/refresh ----------------------------------------------

    async def refresh(self) -> ApiResponse:
        """SPEC 13.7.2: queue a poll + reconcile cycle; ``202 Accepted``."""
        if self._refresh is None and self._hook is None:
            return error_response(
                "refresh_unavailable",
                "no refresh trigger is wired to this HTTP surface",
                status=503,
            )

        requested_at = self.now()
        result = self._refresh.request() if self._refresh is not None else RefreshResult()

        if self._hook is not None:
            try:
                returned = self._hook()
                if inspect.isawaitable(returned):
                    returned = await returned
            except Exception as exc:  # SPEC 14.2
                return error_response(
                    "refresh_unavailable",
                    f"refresh trigger failed ({_detail(exc)})",
                    status=503,
                )
            if isinstance(returned, RefreshResult):
                result = returned

        return ApiResponse(202, result.to_payload(requested_at))

    # -- error routes ------------------------------------------------------

    def method_not_allowed(self, method: str, allowed: tuple[str, ...]) -> ApiResponse:
        """SPEC 13.7.2: unsupported methods on defined routes return ``405``."""
        return error_response(
            "method_not_allowed",
            f"{method} is not supported here; allowed: {', '.join(allowed)}",
            status=405,
            Allow=", ".join(allowed),
        )

    def not_found(self, path: str) -> ApiResponse:
        return error_response("not_found", f"no route for {path!r}", status=404)

    # -- dispatch ----------------------------------------------------------

    async def handle(self, method: str, path: str) -> ApiResponse:
        """Resolve ``method``/``path`` under ``/api/v1`` and run the endpoint."""
        target = api_target(path)
        if target is None:
            return self.not_found(path)
        if method not in target.allowed_methods:
            return self.method_not_allowed(method, target.allowed_methods)
        if target.name == "state":
            return self.state()
        if target.name == "refresh":
            return await self.refresh()
        assert target.identifier is not None
        return self.issue(target.identifier)
