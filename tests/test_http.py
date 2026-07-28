"""Conformance tests for the OPTIONAL HTTP server extension — SPEC 13.7.

Extension Conformance profile (SPEC 17, 18.2): "HTTP server extension honors CLI
``--port`` over ``server.port``, uses a safe default bind host, and exposes the
baseline endpoints/error semantics in Section 13.7 if shipped."

The snapshot provider is always a fake. ``symphony.observability.snapshot`` is
owned by another module and is deliberately not imported here — the one test
that covers the CONTRACTS.md integration point stubs it into ``sys.modules`` to
prove the binding is late.
"""

from __future__ import annotations

import signal
import socket
import sys
import types
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from symphony.errors import ConfigValidationError
from symphony.http.api import (
    ApiResponse,
    JsonApi,
    RefreshCoordinator,
    RefreshResult,
    SnapshotSource,
    api_target,
    rfc3339,
    snapshot_source,
)
from symphony.http.dashboard import format_duration, render_dashboard, render_error_page
from symphony.http.server import (
    DEFAULT_BIND_HOST,
    BindTarget,
    HttpServer,
    build_http_server,
    create_app,
    extension_enabled,
    open_listener,
    resolve_bind,
    resolve_port,
)

# --------------------------------------------------------------------------
# Fixtures: a fake snapshot provider shaped exactly like SPEC 13.7.2
# --------------------------------------------------------------------------

SPEC_STATE: dict[str, Any] = {
    "generated_at": "2026-02-24T20:15:30Z",
    "counts": {"running": 2, "retrying": 1},
    "running": [
        {
            "issue_id": "abc123",
            "issue_identifier": "MT-649",
            "issue_url": "https://tracker.example/issues/MT-649",
            "state": "In Progress",
            "session_id": "thread-1-turn-1",
            "turn_count": 7,
            "last_event": "turn_completed",
            "last_message": "",
            "started_at": "2026-02-24T20:10:12Z",
            "last_event_at": "2026-02-24T20:14:59Z",
            "tokens": {"input_tokens": 1200, "output_tokens": 800, "total_tokens": 2000},
        },
        {
            "issue_id": "ghi789",
            "issue_identifier": "MT-651",
            "issue_url": None,
            "state": "In Progress",
            "session_id": "thread-2-turn-3",
            "turn_count": 2,
            "last_event": "notification",
            "last_message": "Working on tests",
            "started_at": "2026-02-24T20:12:00Z",
            "last_event_at": "2026-02-24T20:15:01Z",
            "tokens": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
    ],
    "retrying": [
        {
            "issue_id": "def456",
            "issue_identifier": "MT-650",
            "issue_url": "https://tracker.example/issues/MT-650",
            "attempt": 3,
            "due_at": "2026-02-24T20:16:00Z",
            "error": "no available orchestrator slots",
        }
    ],
    "codex_totals": {
        "input_tokens": 5000,
        "output_tokens": 2400,
        "total_tokens": 7400,
        "seconds_running": 1834.2,
    },
    "rate_limits": None,
}

SPEC_ISSUE: dict[str, Any] = {
    "issue_identifier": "MT-649",
    "issue_id": "abc123",
    "status": "running",
    "workspace": {"path": "/tmp/symphony_workspaces/MT-649"},
    "attempts": {"restart_count": 1, "current_retry_attempt": 2},
    "running": {
        "session_id": "thread-1-turn-1",
        "turn_count": 7,
        "state": "In Progress",
        "started_at": "2026-02-24T20:10:12Z",
        "last_event": "notification",
        "last_message": "Working on tests",
        "last_event_at": "2026-02-24T20:14:59Z",
        "tokens": {"input_tokens": 1200, "output_tokens": 800, "total_tokens": 2000},
    },
    "retry": None,
    "logs": {
        "codex_session_logs": [
            {"label": "latest", "path": "/var/log/symphony/codex/MT-649/latest.log", "url": None}
        ]
    },
    "recent_events": [
        {"at": "2026-02-24T20:14:59Z", "event": "notification", "message": "Working on tests"}
    ],
    "last_error": None,
    "tracked": {},
}


class FakeProvider:
    """Stand-in for ``symphony.observability.snapshot`` (SPEC 13.3)."""

    def __init__(
        self,
        state: Any = None,
        details: dict[str, Any] | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self.state = SPEC_STATE if state is None else state
        self.details = {"MT-649": SPEC_ISSUE} if details is None else details
        self.raises = raises
        self.snapshot_calls = 0
        self.detail_calls: list[str] = []

    def snapshot(self) -> Any:
        self.snapshot_calls += 1
        if self.raises is not None:
            raise self.raises
        return self.state

    def issue_detail(self, identifier: str) -> Any:
        self.detail_calls.append(identifier)
        if self.raises is not None:
            raise self.raises
        return self.details.get(identifier)

    def as_source(self) -> SnapshotSource:
        return SnapshotSource(snapshot=self.snapshot, issue_detail=self.issue_detail)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


def make_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://symphony.test"
    )


FIXED_NOW = datetime(2026, 2, 24, 20, 15, 30, 123456, tzinfo=UTC)


def fixed_clock() -> datetime:
    return FIXED_NOW


# ==========================================================================
# Bind policy — SPEC 13.7 "Extension config" / "Enablement", SPEC 18.2
# ==========================================================================


def test_extension_is_disabled_when_no_port_is_configured(provider: FakeProvider) -> None:
    assert resolve_port() is None
    assert resolve_bind() is None
    assert extension_enabled() is False
    assert build_http_server(provider.as_source()) is None


def test_cli_port_overrides_config_port() -> None:
    """SPEC 13.7: 'CLI --port overrides server.port when both are present.'"""
    assert resolve_port(cli_port=8123, config_port=9000) == 8123
    assert resolve_bind(8123, 9000) == BindTarget(DEFAULT_BIND_HOST, 8123)


def test_config_port_used_when_no_cli_port() -> None:
    assert resolve_port(config_port=9000) == 9000
    assert resolve_bind(None, 9000).port == 9000


def test_cli_port_zero_still_overrides_a_positive_config_port() -> None:
    """Port 0 is a value, not an absence.

    Truthiness-based precedence would silently pick 9000 here and bind a real
    port when the operator explicitly asked for an ephemeral one.
    """
    assert resolve_port(cli_port=0, config_port=9000) == 0
    assert resolve_bind(0, 9000).is_ephemeral is True


def test_config_port_zero_enables_the_extension() -> None:
    """SPEC 13.7: '0 requests an ephemeral port for local development and tests.'"""
    assert extension_enabled(config_port=0) is True
    bind = resolve_bind(config_port=0)
    assert bind == BindTarget(DEFAULT_BIND_HOST, 0)
    assert bind.is_ephemeral is True


def test_default_bind_host_is_loopback() -> None:
    """SPEC 13.7: 'SHOULD bind loopback by default ... unless explicitly configured'."""
    bind = resolve_bind(config_port=9000)
    assert bind.host == "127.0.0.1"
    assert bind.is_loopback is True
    assert bind.url() == "http://127.0.0.1:9000"


def test_explicit_host_overrides_the_loopback_default() -> None:
    bind = resolve_bind(config_port=9000, host="0.0.0.0")
    assert bind.host == "0.0.0.0"
    assert bind.is_loopback is False


def test_blank_host_falls_back_to_loopback() -> None:
    assert resolve_bind(config_port=9000, host="   ").host == DEFAULT_BIND_HOST


@pytest.mark.parametrize("bad", [-1, 65536, 70000])
def test_out_of_range_port_is_a_config_validation_error(bad: int) -> None:
    with pytest.raises(ConfigValidationError):
        resolve_port(config_port=bad)


@pytest.mark.parametrize("bad", ["9000", 90.5, True])
def test_non_integer_port_is_a_config_validation_error(bad: Any) -> None:
    with pytest.raises(ConfigValidationError):
        resolve_port(cli_port=bad)


def test_open_listener_resolves_an_ephemeral_port() -> None:
    """Port 0 must produce a real, knowable port before serving starts."""
    sock = open_listener(BindTarget(DEFAULT_BIND_HOST, 0))
    try:
        host, port = sock.getsockname()[:2]
        assert host == DEFAULT_BIND_HOST
        assert port != 0
    finally:
        sock.close()


# ==========================================================================
# Route resolution — SPEC 13.7.2
# ==========================================================================


def test_api_target_resolves_the_three_routes() -> None:
    assert api_target("/api/v1/state").name == "state"
    assert api_target("/api/v1/state").allowed_methods == ("GET", "HEAD")
    assert api_target("/api/v1/refresh").name == "refresh"
    assert api_target("/api/v1/refresh").allowed_methods == ("POST",)
    issue = api_target("/api/v1/MT-649")
    assert (issue.name, issue.identifier) == ("issue", "MT-649")


@pytest.mark.parametrize(
    "path", ["/", "/api", "/api/v1", "/api/v1/", "/api/v2/state", "/api/v1x/y"]
)
def test_api_target_rejects_non_api_paths(path: str) -> None:
    assert api_target(path) is None


def test_refresh_literal_is_not_shadowed_by_the_identifier_route() -> None:
    """A wildcard that swallowed '/api/v1/refresh' would break the POST route."""
    assert api_target("/api/v1/refresh").identifier is None


# ==========================================================================
# GET /api/v1/state — SPEC 13.7.2
# ==========================================================================


async def test_state_returns_the_spec_shape(provider: FakeProvider) -> None:
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.get("/api/v1/state")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) >= {
        "generated_at",
        "counts",
        "running",
        "retrying",
        "codex_totals",
        "rate_limits",
    }
    assert body["counts"] == {"running": 2, "retrying": 1}
    assert body["running"][0] == SPEC_STATE["running"][0]
    assert body["retrying"][0] == SPEC_STATE["retrying"][0]
    assert body["codex_totals"]["seconds_running"] == 1834.2
    assert body["rate_limits"] is None


async def test_state_passes_through_provider_added_fields() -> None:
    """SPEC 13.7.2: 'Implementations MAY add fields.'"""
    provider = FakeProvider({**SPEC_STATE, "host": "worker-3", "counts": {"running": 2}})
    async with make_client(create_app(provider.as_source())) as client:
        body = (await client.get("/api/v1/state")).json()

    assert body["host"] == "worker-3"
    # A provider-supplied count wins; the missing one is backfilled.
    assert body["counts"]["running"] == 2
    assert body["counts"]["retrying"] == 1


async def test_state_backfills_the_baseline_keys_from_a_partial_snapshot() -> None:
    """The wire contract must hold even if the snapshot builder is partial."""
    provider = FakeProvider({})
    app = create_app(provider.as_source(), clock=fixed_clock)
    async with make_client(app) as client:
        body = (await client.get("/api/v1/state")).json()

    assert body["counts"] == {"running": 0, "retrying": 0}
    assert body["running"] == []
    assert body["retrying"] == []
    assert body["rate_limits"] is None
    assert body["codex_totals"]["total_tokens"] == 0
    assert body["generated_at"] == "2026-02-24T20:15:30Z"


@pytest.mark.parametrize("path", ["/api/v1/state", "/api/v1/MT-649", "/"])
async def test_head_keeps_the_status_and_length_but_drops_the_body(
    path: str, provider: FakeProvider
) -> None:
    """RFC 7231: HEAD advertises the Content-Length GET would have sent."""
    async with make_client(create_app(provider.as_source())) as client:
        head = await client.head(path)
        get = await client.get(path)

    assert head.status_code == get.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == get.headers["content-length"]
    assert int(head.headers["content-length"]) > 0


# ==========================================================================
# GET /api/v1/<issue_identifier> — SPEC 13.7.2
# ==========================================================================


async def test_issue_detail_returns_the_spec_shape(provider: FakeProvider) -> None:
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.get("/api/v1/MT-649")

    assert response.status_code == 200
    body = response.json()
    assert body["issue_identifier"] == "MT-649"
    assert body["issue_id"] == "abc123"
    assert body["status"] == "running"
    assert body["workspace"]["path"] == "/tmp/symphony_workspaces/MT-649"
    assert body["attempts"] == {"restart_count": 1, "current_retry_attempt": 2}
    assert body["running"]["turn_count"] == 7
    assert body["retry"] is None
    assert body["logs"]["codex_session_logs"][0]["label"] == "latest"
    assert body["recent_events"][0]["event"] == "notification"
    assert body["last_error"] is None
    assert body["tracked"] == {}


async def test_unknown_issue_returns_404_with_the_error_envelope(provider: FakeProvider) -> None:
    """SPEC 13.7.2: unknown identifier -> 404 {"error":{"code":"issue_not_found",...}}."""
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.get("/api/v1/MT-9999")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "issue_not_found"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


@pytest.mark.parametrize(
    ("encoded", "identifier"),
    [
        ("MT-649", "MT-649"),
        ("team%2FMT-649", "team/MT-649"),
        ("MT%20649", "MT 649"),
        ("%23123", "#123"),
        ("proj%3Ffoo", "proj?foo"),
    ],
)
async def test_percent_encoded_identifiers_round_trip(encoded: str, identifier: str) -> None:
    """Human ticket keys carry characters that need URL handling."""
    provider = FakeProvider(details={identifier: {"issue_identifier": identifier}})
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.get(f"/api/v1/{encoded}")

    assert provider.detail_calls == [identifier]
    assert response.status_code == 200
    assert response.json()["issue_identifier"] == identifier


# ==========================================================================
# POST /api/v1/refresh — SPEC 13.7.2
# ==========================================================================


async def test_refresh_returns_202_with_the_spec_fields(provider: FakeProvider) -> None:
    app = create_app(provider.as_source(), clock=fixed_clock)
    async with make_client(app) as client:
        response = await client.post("/api/v1/refresh")

    assert response.status_code == 202
    assert response.json() == {
        "queued": True,
        "coalesced": False,
        "requested_at": "2026-02-24T20:15:30Z",
        "operations": ["poll", "reconcile"],
    }


async def test_refresh_accepts_an_empty_json_body(provider: FakeProvider) -> None:
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.post("/api/v1/refresh", json={})

    assert response.status_code == 202


async def test_repeated_refresh_requests_coalesce_until_consumed(provider: FakeProvider) -> None:
    """SPEC 13.7.2: 'implementations MAY coalesce repeated requests.'"""
    coordinator = RefreshCoordinator()
    app = create_app(provider.as_source(), refresh=coordinator)

    async with make_client(app) as client:
        first = (await client.post("/api/v1/refresh")).json()
        second = (await client.post("/api/v1/refresh")).json()
        assert first["coalesced"] is False
        assert second["coalesced"] is True
        assert coordinator.pending is True

        # The orchestrator takes the pending refresh; the next request is fresh.
        assert coordinator.consume() is True
        assert coordinator.pending is False
        third = (await client.post("/api/v1/refresh")).json()

    assert third["coalesced"] is False
    assert coordinator.request_count == 3
    assert coordinator.coalesced_count == 1


async def test_refresh_coordinator_wakes_a_waiting_orchestrator() -> None:
    coordinator = RefreshCoordinator()
    coordinator.request()
    await coordinator.wait()  # already pending: returns without blocking
    assert coordinator.consume() is True


async def test_refresh_invokes_a_sync_hook(provider: FakeProvider) -> None:
    calls: list[int] = []
    app = create_app(provider.as_source(), on_refresh=lambda: calls.append(1))
    async with make_client(app) as client:
        response = await client.post("/api/v1/refresh")

    assert response.status_code == 202
    assert calls == [1]


async def test_refresh_invokes_and_awaits_an_async_hook(provider: FakeProvider) -> None:
    calls: list[int] = []

    async def hook() -> RefreshResult:
        calls.append(1)
        return RefreshResult(queued=True, coalesced=False, operations=("poll",))

    app = create_app(provider.as_source(), on_refresh=hook)
    async with make_client(app) as client:
        body = (await client.post("/api/v1/refresh")).json()

    assert calls == [1]
    assert body["operations"] == ["poll"]


async def test_refresh_hook_failure_becomes_503_and_does_not_propagate(
    provider: FakeProvider,
) -> None:
    """SPEC 14.2: a control-surface failure must not take the orchestrator down."""

    def hook() -> None:
        raise RuntimeError("poll loop is wedged")

    app = create_app(provider.as_source(), on_refresh=hook)
    async with make_client(app) as client:
        response = await client.post("/api/v1/refresh")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "refresh_unavailable"


async def test_refresh_without_any_trigger_reports_unavailable(provider: FakeProvider) -> None:
    """Reporting queued=true with nothing wired would be a lie."""
    api = JsonApi(provider.as_source())
    reply = await api.refresh()

    assert isinstance(reply, ApiResponse)
    assert reply.status == 503
    assert reply.payload["error"]["code"] == "refresh_unavailable"


# ==========================================================================
# Method and path errors — SPEC 13.7.2 "API design notes"
# ==========================================================================


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_unsupported_method_on_state_returns_405(method: str, provider: FakeProvider) -> None:
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.request(method, "/api/v1/state")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert response.json()["error"]["code"] == "method_not_allowed"


async def test_get_on_refresh_returns_405_not_404(provider: FakeProvider) -> None:
    """The identifier route must not swallow the literal refresh route.

    A catch-all matched before the literal would answer 404 issue_not_found here
    and hide the fact that the route exists.
    """
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.get("/api/v1/refresh")

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


async def test_unsupported_method_on_an_issue_route_returns_405(provider: FakeProvider) -> None:
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.delete("/api/v1/MT-649")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"


async def test_unsupported_method_on_the_dashboard_returns_405(provider: FakeProvider) -> None:
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.post("/")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"


@pytest.mark.parametrize("path", ["/nope", "/api/v1/", "/api/v2/state", "/favicon.ico"])
async def test_unknown_paths_return_404_with_the_error_envelope(
    path: str, provider: FakeProvider
) -> None:
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.get(path)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ==========================================================================
# Provider faults must never reach the orchestrator — SPEC 13.3, 14.2
# ==========================================================================


async def test_snapshot_failure_becomes_503_snapshot_unavailable() -> None:
    """SPEC 13.3 RECOMMENDED snapshot error mode: 'unavailable'."""
    provider = FakeProvider(raises=RuntimeError("state lock poisoned"))
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.get("/api/v1/state")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "snapshot_unavailable"


async def test_snapshot_timeout_becomes_504_snapshot_timeout() -> None:
    """SPEC 13.3 RECOMMENDED snapshot error mode: 'timeout'."""
    provider = FakeProvider(raises=TimeoutError("snapshot took too long"))
    async with make_client(create_app(provider.as_source())) as client:
        state = await client.get("/api/v1/state")
        issue = await client.get("/api/v1/MT-649")

    assert state.status_code == 504
    assert state.json()["error"]["code"] == "snapshot_timeout"
    assert issue.status_code == 504


async def test_non_mapping_snapshot_is_reported_not_serialized() -> None:
    provider = FakeProvider(["not", "a", "mapping"])
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.get("/api/v1/state")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "snapshot_unavailable"


async def test_dashboard_render_failure_serves_a_page_instead_of_crashing() -> None:
    """SPEC 14.1 class 5 / 14.2: 'Dashboard/log failures: do not crash the orchestrator.'"""
    provider = FakeProvider(raises=RuntimeError("snapshot exploded"))
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.get("/")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert "dashboard unavailable" in response.text
    assert "snapshot exploded" in response.text


async def test_unserializable_snapshot_value_becomes_a_500_envelope(
    provider: FakeProvider,
) -> None:
    """A provider value json cannot encode must not escape as an ASGI exception."""
    source = SnapshotSource(
        snapshot=provider.snapshot,
        issue_detail=lambda identifier: {"issue_identifier": identifier, "bad": {object()}},
    )
    async with make_client(create_app(source)) as client:
        response = await client.get("/api/v1/MT-649")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


# ==========================================================================
# Dashboard — SPEC 13.7.1
# ==========================================================================


async def test_dashboard_is_served_at_root(provider: FakeProvider) -> None:
    async with make_client(create_app(provider.as_source())) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text.startswith("<!doctype html>")


def test_dashboard_fetches_no_external_assets() -> None:
    """Air-gapped hosts: the document must be entirely self-contained."""
    html = render_dashboard(SPEC_STATE)
    for forbidden in ("<script", " src=", "@import", "<link", "url(http", "//cdn", "<iframe"):
        assert forbidden not in html, forbidden
    # The only absolute URLs are the operator-navigable tracker links on rows
    # (SPEC 13.3: rows SHOULD carry the tracker-provided issue URL). Those are
    # anchors the operator clicks, not assets the document loads.
    assert html.count("https://") == 2
    assert 'href="https://tracker.example/issues/MT-649"' in html
    assert 'href="https://tracker.example/issues/MT-650"' in html


def test_dashboard_depicts_the_runtime_state() -> None:
    """SPEC 13.7.1: active sessions, retry delays, tokens, runtime, events, health."""
    html = render_dashboard(SPEC_STATE)
    assert "MT-649" in html  # active session
    assert "thread-1-turn-1" in html
    assert "MT-650" in html  # retry queue row
    assert "no available orchestrator slots" in html  # retry error
    assert "2026-02-24T20:16:00Z" in html  # retry due time
    assert "7,400" in html  # total tokens
    assert "30m 34s" in html  # seconds_running humanized
    assert "turn_completed" in html  # recent event
    assert "healthy" in html  # health indicator
    assert "none reported" in html  # rate limits


def test_dashboard_flags_session_errors_in_the_health_indicator() -> None:
    state = {**SPEC_STATE, "running": [{**SPEC_STATE["running"][0], "last_error": "turn_failed"}]}
    html = render_dashboard(state)
    assert "1 session error(s)" in html
    assert 'class="pill err"' in html


def test_dashboard_escapes_snapshot_values() -> None:
    """Snapshot text is provider data; it must never become markup."""
    state = {
        "generated_at": "<b>now</b>",
        "running": [
            {
                "issue_identifier": "<script>alert(1)</script>",
                "issue_url": "javascript:alert(1)",
                "last_message": '"><img onerror=x>',
            }
        ],
        "retrying": [],
    }
    html = render_dashboard(state)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "javascript:alert(1)" not in html  # non-http scheme is not linked
    assert "<img onerror" not in html


def test_dashboard_renders_an_empty_and_a_malformed_snapshot() -> None:
    for snapshot in ({}, {"running": "nonsense", "retrying": None, "codex_totals": 7}):
        html = render_dashboard(snapshot)
        assert html.startswith("<!doctype html>")
        assert "no active sessions" in html
        assert "no queued retries" in html


def test_dashboard_meta_refresh_is_configurable() -> None:
    assert '<meta http-equiv="refresh" content="5">' in render_dashboard(SPEC_STATE)
    assert '<meta http-equiv="refresh" content="30">' in render_dashboard(
        SPEC_STATE, refresh_seconds=30
    )
    assert "http-equiv" not in render_dashboard(SPEC_STATE, refresh_seconds=None)


def test_render_error_page_is_self_contained_and_names_the_failure() -> None:
    html = render_error_page("RuntimeError: kaboom")
    assert html.startswith("<!doctype html>")
    assert "RuntimeError: kaboom" in html
    for forbidden in ("<script", " src=", "@import", "<link"):
        assert forbidden not in html


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0.0s"),
        (9.25, "9.2s"),
        (61, "1m 01s"),
        (1834.2, "30m 34s"),
        (3661, "1h 01m 01s"),
        (None, "—"),
        ("nope", "—"),
    ],
)
def test_format_duration(seconds: Any, expected: str) -> None:
    assert format_duration(seconds) == expected


# ==========================================================================
# Timestamps
# ==========================================================================


def test_rfc3339_matches_the_spec_sample_format() -> None:
    assert rfc3339(FIXED_NOW) == "2026-02-24T20:15:30Z"
    assert rfc3339(datetime(2026, 2, 24, 20, 15, 30)) == "2026-02-24T20:15:30Z"


# ==========================================================================
# CONTRACTS.md integration point (late-bound, sibling-owned)
# ==========================================================================


def test_snapshot_source_binds_the_observability_builders_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTRACTS.md: build_snapshot(state) / build_issue_detail(state, identifier).

    The import happens inside snapshot_source, not at module scope, so the HTTP
    extension stays importable while the sibling module is still being written.
    """
    module = types.ModuleType("symphony.observability.snapshot")
    seen: list[Any] = []
    module.build_snapshot = lambda state: seen.append(("snapshot", state)) or {"ok": True}
    module.build_issue_detail = lambda state, identifier: (
        seen.append(("detail", state, identifier)) or {"issue_identifier": identifier}
    )
    monkeypatch.setitem(sys.modules, "symphony.observability.snapshot", module)

    sentinel = object()
    source = snapshot_source(sentinel)

    assert source.snapshot() == {"ok": True}
    assert source.issue_detail("MT-1") == {"issue_identifier": "MT-1"}
    assert seen == [("snapshot", sentinel), ("detail", sentinel, "MT-1")]


def test_server_module_does_not_import_the_snapshot_sibling_at_module_scope() -> None:
    import symphony.http.server as server_module

    assert not hasattr(server_module, "build_snapshot")
    assert not hasattr(server_module, "build_issue_detail")


# ==========================================================================
# Lifecycle — real listener, ephemeral port
# ==========================================================================


async def test_server_binds_an_ephemeral_loopback_port_and_serves(
    provider: FakeProvider,
) -> None:
    server = build_http_server(provider.as_source(), config_port=0)
    assert server is not None
    assert server.requested.is_ephemeral is True

    await server.start()
    try:
        assert server.running is True
        assert server.bound.host == DEFAULT_BIND_HOST
        assert server.port != 0
        async with httpx.AsyncClient(base_url=server.base_url, timeout=10.0) as client:
            state = await client.get("/api/v1/state")
            dashboard = await client.get("/")
            refresh = await client.post("/api/v1/refresh")
            missing = await client.get("/api/v1/MT-0")
    finally:
        await server.stop()

    assert state.status_code == 200
    assert state.json()["counts"]["running"] == 2
    assert dashboard.status_code == 200
    assert "MT-649" in dashboard.text
    assert refresh.status_code == 202
    assert missing.status_code == 404


async def test_stop_releases_the_listener_and_is_idempotent(provider: FakeProvider) -> None:
    server = build_http_server(provider.as_source(), cli_port=0)
    await server.start()
    port = server.port
    await server.stop()
    await server.stop()  # idempotent

    assert server.running is False
    with socket.socket() as probe:
        probe.settimeout(2.0)
        assert probe.connect_ex((DEFAULT_BIND_HOST, port)) != 0


async def test_http_server_works_as_an_async_context_manager(provider: FakeProvider) -> None:
    """The explicit construction path, for callers that build the app themselves."""
    server = HttpServer(create_app(provider.as_source()), BindTarget(DEFAULT_BIND_HOST, 0))
    async with server as running:
        assert running is server
        assert running.port != 0
        async with httpx.AsyncClient(base_url=running.base_url, timeout=10.0) as client:
            response = await client.get("/api/v1/state")
    assert response.status_code == 200
    assert server.running is False


async def test_starting_twice_is_rejected(provider: FakeProvider) -> None:
    server = build_http_server(provider.as_source(), cli_port=0)
    await server.start()
    try:
        with pytest.raises(RuntimeError):
            await server.start()
    finally:
        await server.stop()


async def test_port_is_unavailable_before_start(provider: FakeProvider) -> None:
    server = build_http_server(provider.as_source(), cli_port=0)
    assert server.bound is None
    with pytest.raises(RuntimeError):
        _ = server.port
    with pytest.raises(RuntimeError):
        _ = server.base_url


async def test_server_does_not_hijack_host_signal_handlers(provider: FakeProvider) -> None:
    """SPEC 17.7: the host process owns shutdown, not an OPTIONAL extension.

    uvicorn installs SIGINT/SIGTERM handlers inside serve() by default, which
    would silently take over the orchestrator's shutdown path.
    """
    before = signal.getsignal(signal.SIGINT)
    server = build_http_server(provider.as_source(), cli_port=0)
    await server.start()
    try:
        assert signal.getsignal(signal.SIGINT) is before
    finally:
        await server.stop()
    assert signal.getsignal(signal.SIGINT) is before
