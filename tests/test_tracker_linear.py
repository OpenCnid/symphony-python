"""Conformance tests for the Linear adapter (SPEC 11, 17.3).

No network: every provider interaction runs through ``httpx.MockTransport``
against synthetic Linear GraphQL payloads. Tests that would need real
credentials are marked ``integration`` and live at the bottom.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from symphony.errors import (
    InvalidTrackerConfig,
    MissingTrackerSecret,
    TrackerPaginationError,
    TrackerRateLimited,
    TrackerRequestError,
    TrackerResponseError,
    TrackerStatusError,
)
from symphony.models import Issue
from symphony.trackers import linear
from symphony.trackers.base import ToolContext, adapter_kinds, build_adapter
from symphony.trackers.linear import LinearAdapter, map_priority, normalize_issue

API_KEY = "lin_api_TESTKEY_do_not_leak"

# --------------------------------------------------------------------------
# Synthetic payload builders
# --------------------------------------------------------------------------


def issue_node(**overrides: Any) -> dict[str, Any]:
    """A well-formed Linear ``Issue`` node with every selected field present."""
    node: dict[str, Any] = {
        "id": "8f1e-uuid-1",
        "identifier": "ENG-1",
        "number": 1,
        "title": "Ship the adapter",
        "description": "Body text",
        "priority": 2,
        "priorityLabel": "High",
        "url": "https://linear.app/acme/issue/ENG-1",
        "branchName": "eng-1-ship-the-adapter",
        "createdAt": "2026-07-01T10:00:00.000Z",
        "updatedAt": "2026-07-02T11:30:00.000Z",
        "archivedAt": None,
        "state": {"id": "st-todo", "name": "Todo", "type": "unstarted"},
        "assignee": {"id": "user-1"},
        "team": {"id": "team-1", "key": "ENG"},
        "project": {"id": "proj-1", "name": "Core"},
        "labels": {"nodes": [{"id": "l1", "name": "Backend"}]},
        "inverseRelations": {"nodes": []},
    }
    node.update(overrides)
    return node


def blocks_relation(state_type: str, *, identifier: str = "ENG-9") -> dict[str, Any]:
    return {
        "type": "blocks",
        "issue": {
            "id": f"uuid-{identifier}",
            "identifier": identifier,
            "state": {"name": state_type.title(), "type": state_type},
        },
    }


def issues_page(
    nodes: list[Any], *, has_next: bool = False, cursor: str | None = None
) -> dict[str, Any]:
    return {
        "data": {
            "issues": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }


class LinearStub:
    """Routes requests by GraphQL operation name and records the bodies sent."""

    def __init__(self, routes: dict[str, Any]) -> None:
        # value: a dict payload, an httpx.Response, or a list consumed in order.
        self.routes = routes
        self.calls: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        self.headers.append(request.headers)
        for name, value in self.routes.items():
            if name in body["query"]:
                if isinstance(value, list):
                    value = value.pop(0)
                if isinstance(value, httpx.Response):
                    return value
                if callable(value):
                    return value(request)
                return httpx.Response(200, json=value)
        raise AssertionError(f"unrouted operation: {body['query'][:120]!r}")

    def variables(self, index: int = 0) -> dict[str, Any]:
        return self.calls[index]["variables"]


def never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError("the adapter made a provider request when it must not have")


def make_adapter(handler: Any = never_called, **provider: Any) -> LinearAdapter:
    cfg: dict[str, Any] = {"api_key": API_KEY, "team_key": "ENG"}
    cfg.update(provider)
    return LinearAdapter(cfg, transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------
# Registration and configuration (SPEC 5.3.1, 6.3, 11.2)
# --------------------------------------------------------------------------


def test_adapter_is_registered_under_kind_linear() -> None:
    assert "linear" in adapter_kinds()
    built = build_adapter("linear", {"api_key": API_KEY, "team_key": "ENG"})
    assert isinstance(built, LinearAdapter)
    assert built.kind == "linear"


def test_documented_state_defaults_exist() -> None:
    # SPEC 5.3.1 allows omitting active/terminal states only when the profile
    # documents defaults.
    assert LinearAdapter.default_active_states == ("Todo", "In Progress")
    assert LinearAdapter.default_terminal_states == ("Done", "Canceled", "Duplicate")


def test_configured_states_override_defaults() -> None:
    adapter = LinearAdapter(
        {"api_key": API_KEY, "team_key": "ENG"},
        active_states=["In Progress"],
        terminal_states=["Shipped"],
    )
    assert adapter.active_states == ("In Progress",)
    assert adapter.terminal_states == ("Shipped",)


def test_missing_team_scope_is_invalid_config() -> None:
    with pytest.raises(InvalidTrackerConfig) as exc:
        LinearAdapter({"api_key": API_KEY})
    assert exc.value.category == "invalid_tracker_config"


def test_missing_credential_is_missing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(MissingTrackerSecret) as exc:
        LinearAdapter({"team_key": "ENG"})
    assert exc.value.category == "missing_tracker_secret"


def test_var_indirection_resolves_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_LINEAR_TOKEN", API_KEY)
    adapter = LinearAdapter({"api_key": "$MY_LINEAR_TOKEN", "team_key": "ENG"})
    assert "MY_LINEAR_TOKEN" in adapter.secret_environment_names()


def test_empty_var_is_treated_as_missing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # SPEC 5.3.1: a documented secret $VAR resolving to '' is *missing*.
    monkeypatch.setenv("MY_LINEAR_TOKEN", "   ")
    with pytest.raises(MissingTrackerSecret):
        LinearAdapter({"api_key": "$MY_LINEAR_TOKEN", "team_key": "ENG"})


def test_secret_environment_names_always_declares_the_default() -> None:
    adapter = make_adapter()
    assert adapter.secret_environment_names() == ["LINEAR_API_KEY"]


def test_invalid_auth_scheme_and_page_size_are_config_errors() -> None:
    with pytest.raises(InvalidTrackerConfig):
        LinearAdapter({"api_key": API_KEY, "team_key": "ENG", "auth_scheme": "hmac"})
    with pytest.raises(InvalidTrackerConfig):
        LinearAdapter({"api_key": API_KEY, "team_key": "ENG", "page_size": 0})
    with pytest.raises(InvalidTrackerConfig):
        LinearAdapter({"api_key": API_KEY, "team_key": "ENG", "page_size": 500})


def test_repr_does_not_leak_the_credential() -> None:
    adapter = make_adapter()
    assert API_KEY not in repr(adapter)
    assert "ENG" in repr(adapter)


async def test_auth_header_scheme_is_explicit() -> None:
    stub = LinearStub({"SymphonyLinearIssues": issues_page([])})
    raw = make_adapter(stub)
    await raw.fetch_issues_by_states(["Todo"])
    assert stub.headers[0]["authorization"] == API_KEY

    stub2 = LinearStub({"SymphonyLinearIssues": issues_page([])})
    bearer = make_adapter(stub2, auth_scheme="bearer")
    await bearer.fetch_issues_by_states(["Todo"])
    assert stub2.headers[0]["authorization"] == f"Bearer {API_KEY}"


# --------------------------------------------------------------------------
# Read kernel (SPEC 11.1, 17.3)
# --------------------------------------------------------------------------


async def test_empty_state_list_returns_empty_without_a_provider_call() -> None:
    adapter = make_adapter()
    assert await adapter.fetch_issues_by_states([]) == []


async def test_blank_state_names_return_empty_without_a_provider_call() -> None:
    adapter = make_adapter()
    assert await adapter.fetch_issues_by_states(["", "   "]) == []


async def test_empty_id_list_returns_empty_without_a_provider_call() -> None:
    adapter = make_adapter()
    assert await adapter.fetch_issues_by_ids([]) == []


async def test_state_selection_and_scope_are_applied_provider_side() -> None:
    stub = LinearStub({"SymphonyLinearIssues": issues_page([issue_node()])})
    adapter = make_adapter(stub, project_id="proj-1")
    await adapter.fetch_issues_by_states(["Todo", "todo", "In Progress"])

    filter_ = stub.variables()["filter"]
    assert filter_["team"] == {"key": {"eq": "ENG"}}
    assert filter_["project"] == {"id": {"eq": "proj-1"}}
    # Case-insensitive, de-duplicated, provider-side state matching.
    assert filter_["or"] == [
        {"state": {"name": {"eqIgnoreCase": "Todo"}}},
        {"state": {"name": {"eqIgnoreCase": "In Progress"}}},
    ]
    assert stub.variables()["first"] == linear.DEFAULT_PAGE_SIZE


async def test_pagination_preserves_order_across_pages() -> None:
    page1 = issues_page(
        [issue_node(id="u1", identifier="ENG-1"), issue_node(id="u2", identifier="ENG-2")],
        has_next=True,
        cursor="cursor-1",
    )
    page2 = issues_page([issue_node(id="u3", identifier="ENG-3")])
    stub = LinearStub({"SymphonyLinearIssues": [page1, page2]})
    adapter = make_adapter(stub)

    issues = await adapter.fetch_issues_by_states(["Todo"])

    assert [i.identifier for i in issues] == ["ENG-1", "ENG-2", "ENG-3"]
    assert stub.variables(0)["after"] is None
    assert stub.variables(1)["after"] == "cursor-1"


async def test_repeated_cursor_is_a_pagination_error() -> None:
    page = issues_page([issue_node()], has_next=True, cursor="loop")
    stub = LinearStub({"SymphonyLinearIssues": [page, dict(page)]})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerPaginationError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "tracker_pagination"


async def test_has_next_page_without_cursor_is_a_pagination_error() -> None:
    stub = LinearStub(
        {"SymphonyLinearIssues": issues_page([issue_node()], has_next=True, cursor=None)}
    )
    adapter = make_adapter(stub)
    with pytest.raises(TrackerPaginationError):
        await adapter.fetch_issues_by_states(["Todo"])


async def test_max_pages_cap_is_a_pagination_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cursor = body["variables"]["after"] or "0"
        return httpx.Response(
            200, json=issues_page([issue_node()], has_next=True, cursor=f"{cursor}x")
        )

    adapter = make_adapter(handler, max_pages=3)
    with pytest.raises(TrackerPaginationError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert "max_pages=3" in exc.value.message


async def test_fetch_by_ids_treats_input_as_a_set_and_omits_invisible_ids() -> None:
    stub = LinearStub(
        {"SymphonyLinearIssues": issues_page([issue_node(id="u1", identifier="ENG-1")])}
    )
    adapter = make_adapter(stub)

    issues = await adapter.fetch_issues_by_ids(["u1", "u1", "u2", "  "])

    assert stub.variables()["filter"]["id"] == {"in": ["u1", "u2"]}
    assert [i.id for i in issues] == ["u1"]


async def test_fetch_by_ids_returns_full_snapshots_not_just_state() -> None:
    stub = LinearStub({"SymphonyLinearIssues": issues_page([issue_node()])})
    adapter = make_adapter(stub)

    (issue,) = await adapter.fetch_issues_by_ids(["8f1e-uuid-1"])

    assert issue.title == "Ship the adapter"
    assert issue.labels == ("backend",)
    assert issue.branch_name == "eng-1-ship-the-adapter"
    assert issue.url == "https://linear.app/acme/issue/ENG-1"
    assert issue.assignee_id == "user-1"
    assert issue.created_at is not None and issue.updated_at is not None
    assert issue.native_ref is not None and issue.native_ref["project_id"] == "proj-1"


async def test_state_list_omits_and_logs_malformed_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[tuple[str, dict[str, Any]]] = []

    class Recorder:
        def warning(self, message: str, **fields: Any) -> None:
            warnings.append((message, fields))

    monkeypatch.setattr(linear, "_LOG", Recorder())

    good = issue_node(id="u1", identifier="ENG-1")
    missing_title = issue_node(id="u2", identifier="ENG-2", title="")
    missing_state = issue_node(id="u3", identifier="ENG-3", state={"id": "s", "type": "unstarted"})
    stub = LinearStub(
        {"SymphonyLinearIssues": issues_page([good, missing_title, missing_state, "not-an-object"])}
    )
    adapter = make_adapter(stub)

    issues = await adapter.fetch_issues_by_states(["Todo"])

    assert [i.identifier for i in issues] == ["ENG-1"]
    assert len(adapter.last_normalization_report.omitted) == 3
    assert warnings and warnings[0][1]["omitted"] == 3


async def test_id_refresh_fails_on_a_malformed_requested_record() -> None:
    # SPEC 11.1: omission is meaningful here, so a malformed record MUST fail.
    stub = LinearStub({"SymphonyLinearIssues": issues_page([issue_node(title="  ")])})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerResponseError) as exc:
        await adapter.fetch_issues_by_ids(["8f1e-uuid-1"])
    assert exc.value.category == "tracker_response"
    assert exc.value.details["field"] == "title"


async def test_missing_issues_connection_is_a_response_error() -> None:
    stub = LinearStub({"SymphonyLinearIssues": {"data": {}}})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerResponseError):
        await adapter.fetch_issues_by_states(["Todo"])


# --------------------------------------------------------------------------
# Normalization (SPEC 11.3, 17.3)
# --------------------------------------------------------------------------


def test_labels_are_trimmed_lowercased_and_deduplicated() -> None:
    node = issue_node(
        labels={
            "nodes": [
                {"name": "  Backend "},
                {"name": "BACKEND"},
                {"name": "   "},
                {"name": "Needs Review"},
                {"no_name": 1},
            ]
        }
    )
    assert normalize_issue(node).labels == ("backend", "needs review")


def test_unusable_optional_metadata_degrades_without_hiding_required_fields() -> None:
    node = issue_node(
        description="   ",
        priority="not-a-number",
        priorityLabel=None,
        branchName=None,
        url=12345,
        assignee=None,
        project=None,
        createdAt="never",
        updatedAt=None,
        labels=None,
        inverseRelations="broken",
    )
    issue = normalize_issue(node)

    assert issue.identifier == "ENG-1" and issue.state == "Todo"
    assert issue.description is None
    assert issue.priority is None
    assert issue.branch_name is None
    assert issue.url is None
    assert issue.assignee_id is None
    assert issue.created_at is None and issue.updated_at is None
    assert issue.labels == ()
    assert issue.blocked_by == ()


def test_provider_state_spelling_is_preserved() -> None:
    node = issue_node(state={"id": "s", "name": "In Progress", "type": "started"})
    issue = normalize_issue(node)
    assert issue.state == "In Progress"
    assert issue.normalized_state == "in progress"


def test_native_ref_preserves_distinct_provider_ids_and_no_secret() -> None:
    issue = normalize_issue(issue_node())
    assert issue.native_ref == {
        "issue_id": "8f1e-uuid-1",
        "number": 1,
        "team_id": "team-1",
        "team_key": "ENG",
        "state_id": "st-todo",
        "state_name": "Todo",
        "state_type": "unstarted",
        "priority_raw": 2,
        "priority_label": "High",
        "project_id": "proj-1",
        "project_name": "Core",
    }
    assert API_KEY not in json.dumps(issue.native_ref)


@pytest.mark.parametrize("field", ["id", "identifier", "title"])
def test_missing_required_string_field_is_malformed(field: str) -> None:
    with pytest.raises(TrackerResponseError):
        normalize_issue(issue_node(**{field: ""}))


def test_missing_state_name_is_malformed() -> None:
    with pytest.raises(TrackerResponseError):
        normalize_issue(issue_node(state={"id": "s", "type": "started"}))


# --------------------------------------------------------------------------
# Priority mapping (SPEC 8.2, 11.3) — the documented divergence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, None),  # Linear "No priority" -> the null bucket, not rank 0
        (1, 1),  # Urgent
        (2, 2),  # High
        (3, 3),  # Medium
        (4, 4),  # Low
        (None, None),
        ("3", 3),
        (5, None),  # outside Linear's closed scale
        (-1, None),
        (True, None),  # bool is not a priority
        ("", None),
    ],
)
def test_map_priority_table(raw: Any, expected: int | None) -> None:
    assert map_priority(raw) == expected


def test_no_priority_normalizes_to_null_but_raw_value_survives() -> None:
    issue = normalize_issue(issue_node(priority=0, priorityLabel="No priority"))
    assert issue.priority is None
    assert issue.native_ref is not None
    assert issue.native_ref["priority_raw"] == 0
    assert issue.native_ref["priority_label"] == "No priority"


def test_urgent_through_low_pass_through_unchanged() -> None:
    for raw in (1, 2, 3, 4):
        assert normalize_issue(issue_node(priority=raw)).priority == raw


# --------------------------------------------------------------------------
# dispatchable derivation (SPEC 11.3, 17.3)
# --------------------------------------------------------------------------


def test_plain_unstarted_issue_is_dispatchable() -> None:
    assert normalize_issue(issue_node()).dispatchable is True


def test_open_blocker_makes_the_issue_undispatchable() -> None:
    node = issue_node(inverseRelations={"nodes": [blocks_relation("started")]})
    issue = normalize_issue(node)
    assert issue.dispatchable is False
    assert [b.identifier for b in issue.blocked_by] == ["ENG-9"]


@pytest.mark.parametrize("state_type", ["completed", "canceled"])
def test_closed_blockers_do_not_block(state_type: str) -> None:
    node = issue_node(inverseRelations={"nodes": [blocks_relation(state_type)]})
    issue = normalize_issue(node)
    assert issue.dispatchable is True
    assert len(issue.blocked_by) == 1


def test_non_blocking_relation_types_are_ignored() -> None:
    node = issue_node(
        inverseRelations={
            "nodes": [{"type": "related", "issue": {"id": "x", "identifier": "ENG-8"}}]
        }
    )
    issue = normalize_issue(node)
    assert issue.blocked_by == ()
    assert issue.dispatchable is True


def test_blocker_rule_can_be_disabled() -> None:
    node = issue_node(inverseRelations={"nodes": [blocks_relation("started")]})
    assert normalize_issue(node, block_on_open_blockers=False).dispatchable is True


@pytest.mark.parametrize("state_type", ["canceled", "triage"])
def test_canceled_and_triage_state_types_are_undispatchable(state_type: str) -> None:
    node = issue_node(state={"id": "s", "name": "Triage", "type": state_type})
    assert normalize_issue(node).dispatchable is False


def test_completed_state_type_stays_dispatchable_by_default() -> None:
    # A completed-type state may be a configured active handoff state
    # (SPEC 11.5); the adapter does not silently veto explicit config.
    node = issue_node(state={"id": "s", "name": "Ready for Release", "type": "completed"})
    assert normalize_issue(node).dispatchable is True


def test_archived_issue_is_undispatchable() -> None:
    node = issue_node(archivedAt="2026-07-05T00:00:00.000Z")
    assert normalize_issue(node).dispatchable is False


def test_require_assignee_gate() -> None:
    node = issue_node(assignee=None)
    assert normalize_issue(node).dispatchable is True
    assert normalize_issue(node, require_assignee=True).dispatchable is False


async def test_undispatchable_issues_are_still_returned_by_state_polling() -> None:
    # SPEC 11.1: the scheduler owns the dispatchable filter, not the adapter.
    node = issue_node(inverseRelations={"nodes": [blocks_relation("started")]})
    stub = LinearStub({"SymphonyLinearIssues": issues_page([node])})
    adapter = make_adapter(stub)
    (issue,) = await adapter.fetch_issues_by_states(["Todo"])
    assert issue.dispatchable is False


# --------------------------------------------------------------------------
# Error mapping (SPEC 11.4, 17.3)
# --------------------------------------------------------------------------


async def test_transport_failure_maps_to_tracker_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = make_adapter(handler)
    with pytest.raises(TrackerRequestError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "tracker_request"
    assert exc.value.retryable is True


async def test_timeout_maps_to_tracker_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    adapter = make_adapter(handler)
    with pytest.raises(TrackerRequestError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert "timed out" in exc.value.message


async def test_http_429_maps_to_rate_limited_with_retry_after() -> None:
    stub = LinearStub(
        {"SymphonyLinearIssues": httpx.Response(429, json={}, headers={"Retry-After": "30"})}
    )
    adapter = make_adapter(stub)
    with pytest.raises(TrackerRateLimited) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "tracker_rate_limited"
    assert exc.value.retry_after_ms == 30_000


async def test_graphql_ratelimited_code_maps_to_rate_limited() -> None:
    payload = {"errors": [{"message": "slow down", "extensions": {"code": "RATELIMITED"}}]}
    stub = LinearStub({"SymphonyLinearIssues": httpx.Response(400, json=payload)})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerRateLimited):
        await adapter.fetch_issues_by_states(["Todo"])


async def test_http_401_maps_to_tracker_status() -> None:
    stub = LinearStub({"SymphonyLinearIssues": httpx.Response(401, json={})})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerStatusError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "tracker_status"
    assert exc.value.retryable is False


async def test_graphql_authentication_error_maps_to_tracker_status() -> None:
    payload = {
        "errors": [
            {"message": "Authentication required", "extensions": {"code": "AUTHENTICATION_ERROR"}}
        ]
    }
    stub = LinearStub({"SymphonyLinearIssues": httpx.Response(200, json=payload)})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerStatusError):
        await adapter.fetch_issues_by_states(["Todo"])


async def test_graphql_invalid_input_maps_to_tracker_response() -> None:
    payload = {"errors": [{"message": "bad filter", "extensions": {"code": "INVALID_INPUT"}}]}
    stub = LinearStub({"SymphonyLinearIssues": httpx.Response(200, json=payload)})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerResponseError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.details["graphql_code"] == "INVALID_INPUT"


async def test_graphql_internal_error_maps_to_tracker_request() -> None:
    payload = {"errors": [{"message": "boom", "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]}
    stub = LinearStub({"SymphonyLinearIssues": httpx.Response(500, json=payload)})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerRequestError):
        await adapter.fetch_issues_by_states(["Todo"])


async def test_non_json_body_maps_to_tracker_response() -> None:
    stub = LinearStub({"SymphonyLinearIssues": httpx.Response(200, text="<html>nope</html>")})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerResponseError):
        await adapter.fetch_issues_by_states(["Todo"])


async def test_missing_data_object_maps_to_tracker_response() -> None:
    stub = LinearStub({"SymphonyLinearIssues": httpx.Response(200, json={"nope": 1})})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerResponseError):
        await adapter.fetch_issues_by_states(["Todo"])


async def test_error_details_never_carry_the_credential() -> None:
    stub = LinearStub({"SymphonyLinearIssues": httpx.Response(403, json={})})
    adapter = make_adapter(stub)
    with pytest.raises(TrackerStatusError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert API_KEY not in json.dumps(exc.value.to_dict())


# --------------------------------------------------------------------------
# Provider-native agent tools (SPEC 10.5, 11.5)
# --------------------------------------------------------------------------


def context_issue(**overrides: Any) -> Issue:
    return normalize_issue(issue_node(**overrides))


def test_tool_specs_declare_names_and_mutation_capability() -> None:
    specs = {s.name: s for s in make_adapter().agent_tool_specs()}
    assert set(specs) == {
        "linear_set_issue_state",
        "linear_add_comment",
        "linear_attach_link",
        "linear_list_workflow_states",
    }
    assert specs["linear_set_issue_state"].mutates_tracker is True
    assert specs["linear_add_comment"].mutates_tracker is True
    assert specs["linear_attach_link"].mutates_tracker is True
    assert specs["linear_list_workflow_states"].mutates_tracker is False
    for spec in specs.values():
        assert spec.input_schema["type"] == "object"


async def test_unsupported_tool_name_returns_failure_instead_of_raising() -> None:
    adapter = make_adapter()
    result = await adapter.execute_agent_tool("linear_delete_everything", {}, ToolContext())
    assert result.ok is False
    assert "unsupported tool" in (result.error or "")
    assert "linear_add_comment" in result.content["supported"]


async def test_tool_without_issue_context_fails_cleanly() -> None:
    adapter = make_adapter()
    result = await adapter.execute_agent_tool(
        "linear_add_comment", {"body": "hi"}, ToolContext(issue=None)
    )
    assert result.ok is False
    assert "no issue in tool context" in (result.error or "")


async def test_set_issue_state_resolves_state_name_and_mutates() -> None:
    states = {
        "data": {
            "team": {
                "id": "team-1",
                "key": "ENG",
                "states": {
                    "nodes": [
                        {"id": "st-todo", "name": "Todo", "type": "unstarted"},
                        {"id": "st-review", "name": "Human Review", "type": "started"},
                    ]
                },
            }
        }
    }
    update = {
        "data": {
            "issueUpdate": {
                "success": True,
                "issue": {
                    "id": "8f1e-uuid-1",
                    "identifier": "ENG-1",
                    "updatedAt": "2026-07-03T00:00:00.000Z",
                    "state": {"id": "st-review", "name": "Human Review", "type": "started"},
                },
            }
        }
    }
    stub = LinearStub(
        {"SymphonyLinearTeamStates": states, "SymphonyLinearIssueUpdate": update}
    )
    adapter = make_adapter(stub)

    result = await adapter.execute_agent_tool(
        "linear_set_issue_state",
        {"state_name": "human review"},  # case-insensitive
        ToolContext(issue=context_issue()),
    )

    assert result.ok is True
    assert result.content["state"] == "Human Review"
    assert stub.variables(1) == {"id": "8f1e-uuid-1", "input": {"stateId": "st-review"}}
    # The team id came from native_ref, so no team lookup was needed.
    assert len(stub.calls) == 2
    assert API_KEY not in json.dumps(result.to_dict())


async def test_set_issue_state_unknown_state_lists_available_states() -> None:
    states = {
        "data": {
            "team": {
                "id": "team-1",
                "key": "ENG",
                "states": {"nodes": [{"id": "st-todo", "name": "Todo", "type": "unstarted"}]},
            }
        }
    }
    stub = LinearStub({"SymphonyLinearTeamStates": states})
    adapter = make_adapter(stub)

    result = await adapter.execute_agent_tool(
        "linear_set_issue_state", {"state_name": "Shipped"}, ToolContext(issue=context_issue())
    )

    assert result.ok is False
    assert result.content["available"] == ["Todo"]


async def test_set_issue_state_reports_provider_rejection() -> None:
    states = {
        "data": {
            "team": {
                "id": "team-1",
                "states": {"nodes": [{"id": "st-todo", "name": "Todo", "type": "unstarted"}]},
            }
        }
    }
    stub = LinearStub(
        {
            "SymphonyLinearTeamStates": states,
            "SymphonyLinearIssueUpdate": {"data": {"issueUpdate": {"success": False}}},
        }
    )
    adapter = make_adapter(stub)
    result = await adapter.execute_agent_tool(
        "linear_set_issue_state", {"state_name": "Todo"}, ToolContext(issue=context_issue())
    )
    assert result.ok is False
    assert "rejected the state transition" in (result.error or "")


async def test_add_comment_happy_path_and_blank_body() -> None:
    payload = {
        "data": {
            "commentCreate": {
                "success": True,
                "comment": {
                    "id": "c-1",
                    "url": "https://linear.app/acme/issue/ENG-1#comment-c-1",
                    "createdAt": "2026-07-03T00:00:00.000Z",
                },
            }
        }
    }
    stub = LinearStub({"SymphonyLinearCommentCreate": payload})
    adapter = make_adapter(stub)
    ctx = ToolContext(issue=context_issue())

    ok = await adapter.execute_agent_tool("linear_add_comment", {"body": "PR opened"}, ctx)
    assert ok.ok is True
    assert ok.content["comment_id"] == "c-1"
    assert stub.variables()["input"] == {"issueId": "8f1e-uuid-1", "body": "PR opened"}

    blank = await adapter.execute_agent_tool("linear_add_comment", {"body": "   "}, ctx)
    assert blank.ok is False


async def test_attach_link_happy_path_and_url_validation() -> None:
    payload = {
        "data": {
            "attachmentLinkURL": {
                "success": True,
                "attachment": {
                    "id": "a-1",
                    "url": "https://github.com/acme/repo/pull/7",
                    "title": "PR #7",
                },
            }
        }
    }
    stub = LinearStub({"SymphonyLinearAttachmentLinkURL": payload})
    adapter = make_adapter(stub)
    ctx = ToolContext(issue=context_issue())

    ok = await adapter.execute_agent_tool(
        "linear_attach_link",
        {"url": "https://github.com/acme/repo/pull/7", "title": "PR #7"},
        ctx,
    )
    assert ok.ok is True
    assert ok.content["attachment_id"] == "a-1"
    assert stub.variables()["issueId"] == "8f1e-uuid-1"

    bad = await adapter.execute_agent_tool("linear_attach_link", {"url": "not-a-url"}, ctx)
    assert bad.ok is False
    assert "absolute http(s) URL" in (bad.error or "")


async def test_tools_refuse_to_act_on_another_issue() -> None:
    # Authorization boundary (SPEC 10.5): the credential can reach the whole
    # workspace, so the adapter pins mutations to the dispatched issue.
    adapter = make_adapter()
    result = await adapter.execute_agent_tool(
        "linear_add_comment",
        {"body": "hello", "issue_id": "some-other-issue"},
        ToolContext(issue=context_issue()),
    )
    assert result.ok is False
    assert result.content["allowed"] == "ENG-1"


async def test_explicit_matching_issue_id_is_accepted() -> None:
    payload = {"data": {"commentCreate": {"success": True, "comment": {"id": "c-2"}}}}
    stub = LinearStub({"SymphonyLinearCommentCreate": payload})
    adapter = make_adapter(stub)
    result = await adapter.execute_agent_tool(
        "linear_add_comment",
        {"body": "hello", "issue_id": "8f1e-uuid-1"},
        ToolContext(issue=context_issue()),
    )
    assert result.ok is True


async def test_list_workflow_states_resolves_team_by_key_without_context() -> None:
    teams = {"data": {"teams": {"nodes": [{"id": "team-99", "key": "ENG", "name": "Eng"}]}}}
    states = {
        "data": {
            "team": {
                "id": "team-99",
                "states": {"nodes": [{"id": "s1", "name": "Todo", "type": "unstarted"}]},
            }
        }
    }
    stub = LinearStub({"SymphonyLinearTeamByKey": teams, "SymphonyLinearTeamStates": states})
    adapter = make_adapter(stub)

    result = await adapter.execute_agent_tool(
        "linear_list_workflow_states", {}, ToolContext(issue=None)
    )

    assert result.ok is True
    assert result.content["states"] == [{"id": "s1", "name": "Todo", "type": "unstarted"}]
    assert stub.variables(1) == {"teamId": "team-99"}


async def test_tool_provider_failure_becomes_a_structured_result() -> None:
    stub = LinearStub({"SymphonyLinearTeamStates": httpx.Response(500, json={})})
    adapter = make_adapter(stub)
    result = await adapter.execute_agent_tool(
        "linear_set_issue_state", {"state_name": "Todo"}, ToolContext(issue=context_issue())
    )
    assert result.ok is False
    assert result.content["category"] == "tracker_status"


async def test_non_object_arguments_fail_without_raising() -> None:
    adapter = make_adapter()
    result = await adapter.execute_agent_tool(
        "linear_add_comment", ["body"], ToolContext(issue=context_issue())
    )
    assert result.ok is False


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


async def test_aclose_is_idempotent() -> None:
    stub = LinearStub({"SymphonyLinearIssues": [issues_page([]), issues_page([])]})
    adapter = make_adapter(stub)
    await adapter.fetch_issues_by_states(["Todo"])
    await adapter.aclose()
    await adapter.aclose()
    # A closed adapter may still be reused; it builds a fresh client.
    assert await adapter.fetch_issues_by_states(["Todo"]) == []
    await adapter.aclose()


# --------------------------------------------------------------------------
# SPEC 17.8 Real Integration Profile — requires credentials and network.
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_real_linear_candidate_fetch() -> None:  # pragma: no cover - opt-in
    import os

    if not os.environ.get("LINEAR_API_KEY") or not os.environ.get("LINEAR_TEAM_KEY"):
        pytest.skip("LINEAR_API_KEY and LINEAR_TEAM_KEY required")
    adapter = LinearAdapter({"team_key": os.environ["LINEAR_TEAM_KEY"]})
    try:
        issues = await adapter.fetch_issues_by_states(list(LinearAdapter.default_active_states))
        assert all(isinstance(i, Issue) for i in issues)
    finally:
        await adapter.aclose()
