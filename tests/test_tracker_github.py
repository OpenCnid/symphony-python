"""Conformance tests for the GitHub Projects v2 adapter (SPEC 11, 17.3).

Every test drives the real request path through ``httpx.MockTransport`` over
synthetic GraphQL payloads, so the default suite makes no network calls.
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
from symphony.trackers.base import ToolContext, build_adapter
from symphony.trackers.github import GITHUB_KIND, GitHubProjectsAdapter

TOKEN = "ghp_supersecrettokenvalue"
OWNER = "octo-org"
PROJECT = 7


# --------------------------------------------------------------------------
# Synthetic payload builders
# --------------------------------------------------------------------------


def issue_item(
    *,
    item_id: str = "PVTI_item1",
    number: int = 12,
    title: str = "Add retry backoff",
    status: str | None = "In Progress",
    state: str = "OPEN",
    archived: bool = False,
    labels: list[str] | None = None,
    assignees: list[dict[str, str]] | None = None,
    priority: str | None = "P2",
    blocked_by: list[dict[str, Any]] | None = None,
    repo: str = f"{OWNER}/hello-world",
    typename: str = "Issue",
    project_number: int = PROJECT,
    project_owner: str = OWNER,
) -> dict[str, Any]:
    field_nodes: list[dict[str, Any]] = []
    if status is not None:
        field_nodes.append(
            {
                "__typename": "ProjectV2ItemFieldSingleSelectValue",
                "name": status,
                "optionId": "opt_" + status.replace(" ", "_").lower(),
                "field": {"name": "Status"},
            }
        )
    if priority is not None:
        field_nodes.append(
            {
                "__typename": "ProjectV2ItemFieldSingleSelectValue",
                "name": priority,
                "optionId": "opt_priority",
                "field": {"name": "Priority"},
            }
        )
    content: dict[str, Any] = {
        "__typename": typename,
        "id": f"I_node{number}",
        "number": number,
        "title": title,
        "body": "Body text.",
        "url": f"https://github.com/{repo}/issues/{number}",
        "state": state,
        "stateReason": None,
        "createdAt": "2026-07-01T10:00:00Z",
        "updatedAt": "2026-07-02T11:30:00Z",
        "repository": {
            "id": "R_repo1",
            "name": repo.split("/")[-1],
            "nameWithOwner": repo,
            "owner": {"login": repo.split("/")[0]},
        },
        "assignees": {
            "nodes": assignees if assignees is not None else [{"id": "U_1", "login": "alice"}]
        },
        "labels": {
            "nodes": [{"name": name} for name in (labels or ["Backend", "backend", " Bug "])]
        },
    }
    if blocked_by is not None:
        content["blockedBy"] = {"nodes": blocked_by}
    return {
        "id": item_id,
        "isArchived": archived,
        "type": "ISSUE",
        "createdAt": "2026-06-30T09:00:00Z",
        "updatedAt": "2026-07-02T11:30:00Z",
        "project": {
            "id": "PVT_project1",
            "number": project_number,
            "title": "Delivery",
            "url": f"https://github.com/orgs/{project_owner}/projects/{project_number}",
            "owner": {"__typename": "Organization", "login": project_owner},
        },
        "fieldValues": {"nodes": field_nodes},
        "content": content,
    }


def draft_item(*, item_id: str = "PVTI_draft1", status: str = "Todo") -> dict[str, Any]:
    return {
        "id": item_id,
        "isArchived": False,
        "type": "DRAFT_ISSUE",
        "createdAt": "2026-06-30T09:00:00Z",
        "updatedAt": "2026-06-30T09:00:00Z",
        "project": {
            "id": "PVT_project1",
            "number": PROJECT,
            "title": "Delivery",
            "url": f"https://github.com/orgs/{OWNER}/projects/{PROJECT}",
            "owner": {"__typename": "Organization", "login": OWNER},
        },
        "fieldValues": {
            "nodes": [
                {
                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "name": status,
                    "optionId": "opt_todo",
                    "field": {"name": "Status"},
                }
            ]
        },
        "content": {
            "__typename": "DraftIssue",
            "id": "DI_1",
            "title": "Spike: evaluate queue",
            "body": None,
            "createdAt": "2026-06-30T09:00:00Z",
            "updatedAt": "2026-06-30T09:00:00Z",
        },
    }


def list_page(items: list[dict[str, Any]], *, cursor: str | None = None) -> dict[str, Any]:
    return {
        "data": {
            "organization": {
                "projectV2": {
                    "id": "PVT_project1",
                    "number": PROJECT,
                    "title": "Delivery",
                    "url": f"https://github.com/orgs/{OWNER}/projects/{PROJECT}",
                    "items": {
                        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                        "nodes": items,
                    },
                }
            }
        }
    }


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class Recorder:
    """Captures every outbound request and replays canned responses."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.responses = responses or []
        self._index = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if self._index < len(self.responses):
            response = self.responses[self._index]
            self._index += 1
            return response
        return httpx.Response(200, json=list_page([]))

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content.decode()) for r in self.requests]


def make_adapter(
    recorder: Recorder | None = None,
    *,
    provider: dict[str, Any] | None = None,
    clock: float = 1_800_000_000.0,
) -> GitHubProjectsAdapter:
    base = {"owner": OWNER, "project_number": PROJECT}
    base.update(provider or {})
    recorder = recorder or Recorder()
    return GitHubProjectsAdapter(
        base,
        transport=httpx.MockTransport(recorder.handler),
        clock=lambda: clock,
    )


@pytest.fixture(autouse=True)
def _token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)


# --------------------------------------------------------------------------
# Registration and configuration (SPEC 5.3.1, 6.3, 11.2)
# --------------------------------------------------------------------------


def test_adapter_is_registered_under_kind_github() -> None:
    adapter = build_adapter(GITHUB_KIND, {"owner": OWNER, "project_number": PROJECT})
    assert isinstance(adapter, GitHubProjectsAdapter)
    assert adapter.kind == "github"


def test_documents_default_active_and_terminal_states() -> None:
    assert GitHubProjectsAdapter.default_active_states == ("Todo", "In Progress")
    assert GitHubProjectsAdapter.default_terminal_states == ("Done",)


def test_missing_owner_is_invalid_tracker_config() -> None:
    with pytest.raises(InvalidTrackerConfig) as exc:
        GitHubProjectsAdapter({"project_number": PROJECT})
    assert exc.value.category == "invalid_tracker_config"
    assert exc.value.details["key"] == "owner"


def test_missing_project_number_is_invalid_tracker_config() -> None:
    with pytest.raises(InvalidTrackerConfig) as exc:
        GitHubProjectsAdapter({"owner": OWNER})
    assert exc.value.details["key"] == "project_number"


def test_unknown_owner_type_is_rejected() -> None:
    with pytest.raises(InvalidTrackerConfig) as exc:
        GitHubProjectsAdapter({"owner": OWNER, "project_number": 1, "owner_type": "team"})
    assert exc.value.details["key"] == "owner_type"


def test_page_size_out_of_range_is_rejected() -> None:
    with pytest.raises(InvalidTrackerConfig) as exc:
        GitHubProjectsAdapter({"owner": OWNER, "project_number": 1, "page_size": 500})
    assert exc.value.details["key"] == "page_size"


def test_non_http_endpoint_is_rejected() -> None:
    with pytest.raises(InvalidTrackerConfig) as exc:
        GitHubProjectsAdapter({"owner": OWNER, "project_number": 1, "endpoint": "ftp://x/y"})
    assert exc.value.details["key"] == "endpoint"


async def test_user_owned_project_queries_the_user_graphql_root() -> None:
    recorder = Recorder([httpx.Response(200, json={"data": {"user": None}})])
    adapter = GitHubProjectsAdapter(
        {"owner": "octocat", "project_number": 3, "owner_type": "user"},
        transport=httpx.MockTransport(recorder.handler),
    )
    with pytest.raises(InvalidTrackerConfig):
        await adapter.fetch_issues_by_states(["Todo"])
    assert "user(login: $owner)" in recorder.bodies[0]["query"]
    assert "organization(login:" not in recorder.bodies[0]["query"]


# --------------------------------------------------------------------------
# Secrets (SPEC 15.3, 5.3.1)
# --------------------------------------------------------------------------


def test_token_resolves_from_default_environment_variable() -> None:
    adapter = make_adapter()
    assert adapter.secret_environment_names() == ["GITHUB_TOKEN"]


def test_token_env_override_is_declared_as_the_secret_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYMPHONY_GH_PAT", TOKEN)
    adapter = GitHubProjectsAdapter(
        {"owner": OWNER, "project_number": PROJECT, "token_env": "SYMPHONY_GH_PAT"}
    )
    assert adapter.secret_environment_names() == ["SYMPHONY_GH_PAT"]


def test_dollar_var_indirection_declares_the_referenced_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOARD_TOKEN", TOKEN)
    adapter = GitHubProjectsAdapter(
        {"owner": OWNER, "project_number": PROJECT, "token": "${BOARD_TOKEN}"}
    )
    assert adapter.secret_environment_names() == ["BOARD_TOKEN"]


def test_literal_token_declares_no_environment_name() -> None:
    adapter = GitHubProjectsAdapter({"owner": OWNER, "project_number": PROJECT, "token": TOKEN})
    assert adapter.secret_environment_names() == []


def test_empty_dollar_var_is_a_missing_secret_and_never_echoes_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOARD_TOKEN", "   ")
    with pytest.raises(MissingTrackerSecret) as exc:
        GitHubProjectsAdapter({"owner": OWNER, "project_number": PROJECT, "token": "$BOARD_TOKEN"})
    assert exc.value.category == "missing_tracker_secret"
    assert exc.value.details["env"] == "BOARD_TOKEN"
    assert "   " not in exc.value.message


def test_unset_token_env_is_a_missing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(MissingTrackerSecret) as exc:
        GitHubProjectsAdapter({"owner": OWNER, "project_number": PROJECT})
    assert "GITHUB_TOKEN" in exc.value.message


async def test_token_never_appears_in_native_ref_or_errors() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item()]))])
    adapter = make_adapter(recorder)
    issues = await adapter.fetch_issues_by_states(["In Progress"])
    serialized = json.dumps(issues[0].native_ref)
    assert TOKEN not in serialized
    assert recorder.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    await adapter.aclose()


# --------------------------------------------------------------------------
# Read kernel — empty inputs (SPEC 11.1, 17.3)
# --------------------------------------------------------------------------


async def test_empty_state_list_returns_empty_without_a_provider_request() -> None:
    recorder = Recorder()
    adapter = make_adapter(recorder)
    assert await adapter.fetch_issues_by_states([]) == []
    assert recorder.requests == []


async def test_blank_only_state_list_returns_empty_without_a_provider_request() -> None:
    recorder = Recorder()
    adapter = make_adapter(recorder)
    assert await adapter.fetch_issues_by_states(["  ", ""]) == []
    assert recorder.requests == []


async def test_empty_id_list_returns_empty_without_a_provider_request() -> None:
    recorder = Recorder()
    adapter = make_adapter(recorder)
    assert await adapter.fetch_issues_by_ids([]) == []
    assert recorder.requests == []


# --------------------------------------------------------------------------
# Identity mapping (SPEC 11.2, 11.3, 17.3)
# --------------------------------------------------------------------------


async def test_project_item_id_is_the_dispatch_identity() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item()]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.id == "PVTI_item1"
    assert issue.id != issue.native_ref["issue_node_id"]


async def test_native_ref_preserves_the_distinct_underlying_ids() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item()]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    ref = issue.native_ref
    assert ref["issue_node_id"] == "I_node12"
    assert ref["issue_number"] == 12
    assert ref["repository"] == f"{OWNER}/hello-world"
    assert ref["project_item_id"] == "PVTI_item1"
    assert ref["project_id"] == "PVT_project1"
    assert json.loads(json.dumps(ref)) == ref


async def test_identifier_is_repo_qualified_and_unique_in_scope() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item()]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.identifier == f"{OWNER}/hello-world#12"


async def test_draft_identifier_falls_back_to_the_item_id() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([draft_item()]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["Todo"])
    assert issue.identifier == "draft:PVTI_draft1"


# --------------------------------------------------------------------------
# Normalization (SPEC 11.3, 17.3)
# --------------------------------------------------------------------------


async def test_state_comes_from_the_status_field_and_keeps_provider_spelling() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item(status="In Progress")]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["in progress"])
    assert issue.state == "In Progress"
    assert issue.normalized_state == "in progress"


async def test_state_filter_excludes_items_in_other_board_states() -> None:
    page = list_page([issue_item(status="Todo"), issue_item(item_id="PVTI_2", number=13)])
    recorder = Recorder([httpx.Response(200, json=page)])
    adapter = make_adapter(recorder)
    issues = await adapter.fetch_issues_by_states(["Todo"])
    assert [i.id for i in issues] == ["PVTI_item1"]


async def test_closed_issue_in_an_active_board_state_is_still_returned() -> None:
    """Board state, not issue open/closed, decides membership in a state read."""
    page = list_page([issue_item(state="CLOSED", status="In Progress")])
    recorder = Recorder([httpx.Response(200, json=page)])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.state == "In Progress"
    assert issue.dispatchable is False


async def test_labels_are_lowercased_trimmed_and_deduplicated() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item()]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.labels == ("backend", "bug")


@pytest.mark.parametrize(
    ("option", "expected"),
    [("P1", 1), ("p3", 3), ("Urgent", 1), ("Low", 4), ("2", 2), ("Someday", None)],
)
async def test_priority_options_map_to_integers_or_null(option: str, expected: int | None) -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item(priority=option)]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.priority == expected


async def test_missing_priority_field_normalizes_to_null() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item(priority=None)]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.priority is None


async def test_timestamps_parse_and_unusable_values_normalize_to_null() -> None:
    good = issue_item()
    bad = issue_item(item_id="PVTI_2", number=13)
    bad["content"]["updatedAt"] = "not-a-date"
    bad["updatedAt"] = "also-not-a-date"
    recorder = Recorder([httpx.Response(200, json=list_page([good, bad]))])
    adapter = make_adapter(recorder)
    first, second = await adapter.fetch_issues_by_states(["In Progress"])
    assert first.updated_at is not None and first.updated_at.year == 2026
    assert second.updated_at is None
    assert second.title == "Add retry backoff"  # required fields survive the fallback


async def test_branch_name_is_null_unless_a_branch_field_is_configured() -> None:
    item = issue_item()
    item["fieldValues"]["nodes"].append(
        {
            "__typename": "ProjectV2ItemFieldTextValue",
            "text": "feat/retry-backoff",
            "field": {"name": "Branch"},
        }
    )
    recorder = Recorder([httpx.Response(200, json=list_page([item]))])
    default = make_adapter(recorder)
    (issue,) = await default.fetch_issues_by_states(["In Progress"])
    assert issue.branch_name is None

    recorder2 = Recorder([httpx.Response(200, json=list_page([item]))])
    configured = make_adapter(recorder2, provider={"branch_field": "Branch"})
    (issue2,) = await configured.fetch_issues_by_states(["In Progress"])
    assert issue2.branch_name == "feat/retry-backoff"


# --------------------------------------------------------------------------
# dispatchable derivation (SPEC 11.3, 17.3)
# --------------------------------------------------------------------------


async def test_open_assigned_issue_on_the_board_is_dispatchable() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item()]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.dispatchable is True
    assert issue.native_ref["not_dispatchable_reasons"] == []


async def test_draft_item_is_not_dispatchable() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([draft_item()]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["Todo"])
    assert issue.dispatchable is False
    assert "draft_item" in issue.native_ref["not_dispatchable_reasons"]


async def test_pull_request_item_is_not_dispatchable() -> None:
    item = issue_item(typename="PullRequest")
    item["type"] = "PULL_REQUEST"
    recorder = Recorder([httpx.Response(200, json=list_page([item]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.dispatchable is False
    assert "pull_request_item" in issue.native_ref["not_dispatchable_reasons"]


async def test_archived_board_item_is_not_dispatchable() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item(archived=True)]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.dispatchable is False
    assert "archived_on_board" in issue.native_ref["not_dispatchable_reasons"]


async def test_closed_issue_is_not_dispatchable() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item(state="CLOSED")]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert "issue_not_open" in issue.native_ref["not_dispatchable_reasons"]


async def test_require_assignee_makes_unassigned_items_undispatchable() -> None:
    payload = list_page([issue_item(assignees=[])])
    recorder = Recorder([httpx.Response(200, json=payload)])
    adapter = make_adapter(recorder, provider={"require_assignee": True})
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.dispatchable is False
    assert "unassigned" in issue.native_ref["not_dispatchable_reasons"]

    recorder2 = Recorder([httpx.Response(200, json=payload)])
    lenient = make_adapter(recorder2)
    (issue2,) = await lenient.fetch_issues_by_states(["In Progress"])
    assert issue2.dispatchable is True


async def test_assignee_allow_list_gates_dispatch_case_insensitively() -> None:
    recorder = Recorder(
        [
            httpx.Response(
                200, json=list_page([issue_item(assignees=[{"id": "U_9", "login": "Bob"}])])
            )
        ]
    )
    adapter = make_adapter(recorder, provider={"assignee_logins": ["bob"]})
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.dispatchable is True

    recorder2 = Recorder([httpx.Response(200, json=list_page([issue_item()]))])
    strict = make_adapter(recorder2, provider={"assignee_logins": ["symphony-bot"]})
    (issue2,) = await strict.fetch_issues_by_states(["In Progress"])
    assert issue2.dispatchable is False
    assert "assignee_not_allowed" in issue2.native_ref["not_dispatchable_reasons"]


async def test_open_issue_dependency_blocks_dispatch_and_populates_blocked_by() -> None:
    blockers = [
        {
            "id": "I_blocker",
            "number": 4,
            "state": "OPEN",
            "repository": {"nameWithOwner": f"{OWNER}/hello-world"},
        }
    ]
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item(blocked_by=blockers)]))])
    adapter = make_adapter(recorder, provider={"issue_dependencies": True})
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.dispatchable is False
    assert "blocked_by_open_dependency" in issue.native_ref["not_dispatchable_reasons"]
    assert issue.blocked_by[0].identifier == f"{OWNER}/hello-world#4"
    assert issue.blocked_by[0].state == "OPEN"


async def test_closed_issue_dependency_does_not_block_dispatch() -> None:
    blockers = [
        {
            "id": "I_blocker",
            "number": 4,
            "state": "CLOSED",
            "repository": {"nameWithOwner": f"{OWNER}/hello-world"},
        }
    ]
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item(blocked_by=blockers)]))])
    adapter = make_adapter(recorder, provider={"issue_dependencies": True})
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.dispatchable is True
    assert len(issue.blocked_by) == 1


async def test_issue_dependencies_are_not_requested_by_default() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item()]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    assert issue.blocked_by == ()
    assert "blockedBy" not in recorder.bodies[0]["query"]


# --------------------------------------------------------------------------
# Pagination (SPEC 11.1, 11.4, 17.3)
# --------------------------------------------------------------------------


async def test_pagination_preserves_order_across_pages() -> None:
    pages = [
        httpx.Response(
            200,
            json=list_page(
                [issue_item(item_id="PVTI_a", number=1), issue_item(item_id="PVTI_b", number=2)],
                cursor="cur1",
            ),
        ),
        httpx.Response(
            200, json=list_page([issue_item(item_id="PVTI_c", number=3)], cursor="cur2")
        ),
        httpx.Response(200, json=list_page([issue_item(item_id="PVTI_d", number=4)])),
    ]
    recorder = Recorder(pages)
    adapter = make_adapter(recorder)
    issues = await adapter.fetch_issues_by_states(["In Progress"])
    assert [i.id for i in issues] == ["PVTI_a", "PVTI_b", "PVTI_c", "PVTI_d"]
    assert [b["variables"]["after"] for b in recorder.bodies] == [None, "cur1", "cur2"]


async def test_repeated_cursor_raises_pagination_error_instead_of_looping() -> None:
    recorder = Recorder(
        [
            httpx.Response(200, json=list_page([issue_item(item_id="PVTI_a")], cursor="same")),
            httpx.Response(200, json=list_page([issue_item(item_id="PVTI_b")], cursor="same")),
        ]
    )
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerPaginationError) as exc:
        await adapter.fetch_issues_by_states(["In Progress"])
    assert exc.value.category == "tracker_pagination"
    assert exc.value.details["reason"] == "cursor_loop"


async def test_has_next_page_without_cursor_raises_pagination_error() -> None:
    payload = list_page([issue_item()], cursor="x")
    payload["data"]["organization"]["projectV2"]["items"]["pageInfo"]["endCursor"] = None
    recorder = Recorder([httpx.Response(200, json=payload)])
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerPaginationError) as exc:
        await adapter.fetch_issues_by_states(["In Progress"])
    assert exc.value.details["reason"] == "missing_cursor"


async def test_exceeding_max_pages_fails_rather_than_truncating_the_board() -> None:
    responses = [
        httpx.Response(200, json=list_page([issue_item(item_id=f"PVTI_{n}")], cursor=f"c{n}"))
        for n in range(5)
    ]
    recorder = Recorder(responses)
    adapter = make_adapter(recorder, provider={"max_pages": 2})
    with pytest.raises(TrackerPaginationError) as exc:
        await adapter.fetch_issues_by_states(["In Progress"])
    assert exc.value.details["reason"] == "page_limit_exceeded"
    assert len(recorder.requests) == 2


async def test_item_repeated_across_pages_is_kept_once_in_first_seen_order() -> None:
    recorder = Recorder(
        [
            httpx.Response(
                200,
                json=list_page(
                    [
                        issue_item(item_id="PVTI_a", number=1),
                        issue_item(item_id="PVTI_b", number=2),
                    ],
                    cursor="cur1",
                ),
            ),
            httpx.Response(
                200,
                json=list_page(
                    [issue_item(item_id="PVTI_b", number=2), issue_item(item_id="PVTI_c", number=3)]
                ),
            ),
        ]
    )
    adapter = make_adapter(recorder)
    issues = await adapter.fetch_issues_by_states(["In Progress"])
    assert [i.id for i in issues] == ["PVTI_a", "PVTI_b", "PVTI_c"]


async def test_page_size_is_sent_to_the_provider() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([]))])
    adapter = make_adapter(recorder, provider={"page_size": 25})
    await adapter.fetch_issues_by_states(["Todo"])
    assert recorder.bodies[0]["variables"] == {
        "owner": OWNER,
        "number": PROJECT,
        "first": 25,
        "after": None,
    }


# --------------------------------------------------------------------------
# Malformed records (SPEC 11.1, 17.3)
# --------------------------------------------------------------------------


async def test_state_list_omits_malformed_records_and_keeps_valid_ones(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = issue_item(item_id="PVTI_broken", number=99, status=None)
    recorder = Recorder([httpx.Response(200, json=list_page([broken, issue_item()]))])
    adapter = make_adapter(recorder)
    with caplog.at_level("WARNING", logger="symphony.trackers.github"):
        issues = await adapter.fetch_issues_by_states(["In Progress"])
    assert [i.id for i in issues] == ["PVTI_item1"]
    assert "tracker_record_omitted" in caplog.text
    assert "PVTI_broken" in caplog.text


async def test_state_list_omits_a_record_missing_its_title() -> None:
    broken = issue_item(item_id="PVTI_broken")
    broken["content"]["title"] = "   "
    recorder = Recorder([httpx.Response(200, json=list_page([broken, issue_item()]))])
    adapter = make_adapter(recorder)
    issues = await adapter.fetch_issues_by_states(["In Progress"])
    assert [i.id for i in issues] == ["PVTI_item1"]


async def test_id_refresh_fails_on_a_malformed_requested_record() -> None:
    broken = issue_item(item_id="PVTI_broken", status=None)
    recorder = Recorder([httpx.Response(200, json={"data": {"nodes": [broken]}})])
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerResponseError) as exc:
        await adapter.fetch_issues_by_ids(["PVTI_broken"])
    assert exc.value.category == "tracker_response"
    assert exc.value.details["field"] == "state"


# --------------------------------------------------------------------------
# fetch_issues_by_ids (SPEC 11.1, 17.3)
# --------------------------------------------------------------------------


async def test_refresh_returns_full_normalized_snapshots() -> None:
    recorder = Recorder([httpx.Response(200, json={"data": {"nodes": [issue_item()]}})])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_ids(["PVTI_item1"])
    assert issue.title == "Add retry backoff"
    assert issue.labels == ("backend", "bug")
    assert issue.state == "In Progress"
    assert issue.dispatchable is True
    assert issue.native_ref["issue_number"] == 12


async def test_refresh_omits_ids_that_no_longer_resolve() -> None:
    payload = {"data": {"nodes": [None, issue_item()]}}
    recorder = Recorder([httpx.Response(200, json=payload)])
    adapter = make_adapter(recorder)
    issues = await adapter.fetch_issues_by_ids(["PVTI_gone", "PVTI_item1"])
    assert [i.id for i in issues] == ["PVTI_item1"]


async def test_refresh_omits_items_belonging_to_another_project() -> None:
    foreign = issue_item(item_id="PVTI_foreign", project_number=99)
    other_owner = issue_item(item_id="PVTI_other", project_owner="someone-else")
    payload = {"data": {"nodes": [foreign, other_owner, issue_item()]}}
    recorder = Recorder([httpx.Response(200, json=payload)])
    adapter = make_adapter(recorder)
    issues = await adapter.fetch_issues_by_ids(["PVTI_foreign", "PVTI_other", "PVTI_item1"])
    assert [i.id for i in issues] == ["PVTI_item1"]


async def test_refresh_ignores_nodes_that_are_not_project_items() -> None:
    payload = {"data": {"nodes": [{"__typename": "Repository", "id": "R_1"}]}}
    recorder = Recorder([httpx.Response(200, json=payload)])
    adapter = make_adapter(recorder)
    assert await adapter.fetch_issues_by_ids(["R_1"]) == []


async def test_refresh_deduplicates_input_ids() -> None:
    recorder = Recorder([httpx.Response(200, json={"data": {"nodes": [issue_item()]}})])
    adapter = make_adapter(recorder)
    await adapter.fetch_issues_by_ids(["PVTI_item1", "PVTI_item1", " PVTI_item1 "])
    assert recorder.bodies[0]["variables"]["ids"] == ["PVTI_item1"]


async def test_refresh_batches_more_than_one_hundred_ids() -> None:
    recorder = Recorder(
        [
            httpx.Response(200, json={"data": {"nodes": []}}),
            httpx.Response(200, json={"data": {"nodes": []}}),
        ]
    )
    adapter = make_adapter(recorder)
    await adapter.fetch_issues_by_ids([f"PVTI_{n}" for n in range(150)])
    assert [len(b["variables"]["ids"]) for b in recorder.bodies] == [100, 50]


# --------------------------------------------------------------------------
# Error mapping (SPEC 11.4, 17.3)
# --------------------------------------------------------------------------


async def test_transport_failure_maps_to_tracker_request() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = GitHubProjectsAdapter(
        {"owner": OWNER, "project_number": PROJECT}, transport=httpx.MockTransport(boom)
    )
    with pytest.raises(TrackerRequestError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "tracker_request"
    assert exc.value.retryable is True


async def test_timeout_maps_to_tracker_request() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = GitHubProjectsAdapter(
        {"owner": OWNER, "project_number": PROJECT}, transport=httpx.MockTransport(slow)
    )
    with pytest.raises(TrackerRequestError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.details["reason"] == "timeout"


async def test_unauthorized_maps_to_tracker_status() -> None:
    recorder = Recorder([httpx.Response(401, json={"message": "Bad credentials"})])
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerStatusError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "tracker_status"
    assert exc.value.details["status"] == 401
    assert exc.value.retryable is False


async def test_server_error_maps_to_retryable_tracker_status() -> None:
    recorder = Recorder([httpx.Response(502, text="bad gateway")])
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerStatusError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.retryable is True


async def test_plain_forbidden_without_rate_limit_signals_is_a_status_error() -> None:
    recorder = Recorder([httpx.Response(403, json={"message": "Resource not accessible"})])
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerStatusError):
        await adapter.fetch_issues_by_states(["Todo"])


async def test_primary_rate_limit_uses_the_reset_header() -> None:
    now = 1_800_000_000.0
    recorder = Recorder(
        [
            httpx.Response(
                403,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(now) + 90),
                },
                json={"message": "API rate limit exceeded"},
            )
        ]
    )
    adapter = make_adapter(recorder, clock=now)
    with pytest.raises(TrackerRateLimited) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "tracker_rate_limited"
    assert exc.value.details["limit"] == "primary"
    assert exc.value.retry_after_ms == 90_000


async def test_secondary_rate_limit_uses_the_retry_after_header() -> None:
    recorder = Recorder(
        [
            httpx.Response(
                403,
                headers={"retry-after": "45", "x-ratelimit-remaining": "4991"},
                json={"message": "You have exceeded a secondary rate limit"},
            )
        ]
    )
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerRateLimited) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.details["limit"] == "secondary"
    assert exc.value.retry_after_ms == 45_000


async def test_secondary_rate_limit_detected_from_the_body_when_no_header_is_sent() -> None:
    recorder = Recorder(
        [
            httpx.Response(
                429,
                headers={"x-ratelimit-remaining": "4000"},
                json={"message": "You have exceeded a secondary rate limit."},
            )
        ]
    )
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerRateLimited) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.details["limit"] == "secondary"
    assert exc.value.retry_after_ms == 60_000


async def test_graphql_rate_limited_error_on_a_200_maps_to_rate_limited() -> None:
    body = {
        "data": None,
        "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
    }
    recorder = Recorder([httpx.Response(200, json=body)])
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerRateLimited) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.details["limit"] == "primary"


async def test_graphql_not_found_maps_to_invalid_tracker_config() -> None:
    body = {
        "data": None,
        "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a node"}],
    }
    recorder = Recorder([httpx.Response(200, json=body)])
    adapter = make_adapter(recorder)
    with pytest.raises(InvalidTrackerConfig) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "invalid_tracker_config"


async def test_graphql_forbidden_maps_to_tracker_status() -> None:
    body = {"data": None, "errors": [{"type": "FORBIDDEN", "message": "no access"}]}
    recorder = Recorder([httpx.Response(200, json=body)])
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerStatusError):
        await adapter.fetch_issues_by_states(["Todo"])


async def test_other_graphql_errors_map_to_tracker_response() -> None:
    body = {"data": None, "errors": [{"message": "Field 'nope' doesn't exist"}]}
    recorder = Recorder([httpx.Response(200, json=body)])
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerResponseError):
        await adapter.fetch_issues_by_states(["Todo"])


async def test_non_json_body_maps_to_tracker_response() -> None:
    recorder = Recorder([httpx.Response(200, text="<html>gateway</html>")])
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerResponseError):
        await adapter.fetch_issues_by_states(["Todo"])


async def test_missing_project_in_payload_maps_to_invalid_tracker_config() -> None:
    recorder = Recorder([httpx.Response(200, json={"data": {"organization": None}})])
    adapter = make_adapter(recorder)
    with pytest.raises(InvalidTrackerConfig) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.details["key"] == "owner"


async def test_missing_items_connection_maps_to_tracker_response() -> None:
    payload = {"data": {"organization": {"projectV2": {"id": "PVT_1", "number": PROJECT}}}}
    recorder = Recorder([httpx.Response(200, json=payload)])
    adapter = make_adapter(recorder)
    with pytest.raises(TrackerResponseError):
        await adapter.fetch_issues_by_states(["Todo"])


# --------------------------------------------------------------------------
# Provider-native agent tools (SPEC 10.5, 11.5)
# --------------------------------------------------------------------------


def test_agent_tool_specs_declare_names_and_mutation_capability() -> None:
    adapter = make_adapter()
    specs = {spec.name: spec for spec in adapter.agent_tool_specs()}
    assert set(specs) == {"github_set_project_status", "github_add_issue_comment"}
    assert all(spec.mutates_tracker for spec in specs.values())
    assert specs["github_set_project_status"].input_schema["required"] == ["status"]


async def test_unsupported_tool_name_returns_a_structured_failure() -> None:
    adapter = make_adapter()
    result = await adapter.execute_agent_tool("github_delete_repo", {}, ToolContext())
    assert result.ok is False
    assert "unsupported tool" in (result.error or "")


async def test_set_project_status_resolves_the_option_and_mutates() -> None:
    field_payload = {
        "data": {
            "organization": {
                "projectV2": {
                    "id": "PVT_project1",
                    "field": {
                        "__typename": "ProjectV2SingleSelectField",
                        "id": "PVTSSF_status",
                        "name": "Status",
                        "options": [
                            {"id": "opt_todo", "name": "Todo"},
                            {"id": "opt_review", "name": "Human Review"},
                        ],
                    },
                }
            }
        }
    }
    recorder = Recorder(
        [
            httpx.Response(200, json=list_page([issue_item()])),
            httpx.Response(200, json=field_payload),
            httpx.Response(
                200,
                json={"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}},
            ),
        ]
    )
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    result = await adapter.execute_agent_tool(
        "github_set_project_status", {"status": "human review"}, ToolContext(issue=issue)
    )
    assert result.ok is True
    assert result.content["status"] == "Human Review"
    variables = recorder.bodies[-1]["variables"]
    assert variables["item"] == "PVTI_item1"
    assert variables["option"] == "opt_review"
    assert variables["project"] == "PVT_project1"


async def test_set_project_status_rejects_an_unknown_option() -> None:
    field_payload = {
        "data": {
            "organization": {
                "projectV2": {
                    "id": "PVT_project1",
                    "field": {
                        "id": "PVTSSF_status",
                        "name": "Status",
                        "options": [{"id": "opt_todo", "name": "Todo"}],
                    },
                }
            }
        }
    }
    recorder = Recorder(
        [
            httpx.Response(200, json=list_page([issue_item()])),
            httpx.Response(200, json=field_payload),
        ]
    )
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    result = await adapter.execute_agent_tool(
        "github_set_project_status", {"status": "Shipped"}, ToolContext(issue=issue)
    )
    assert result.ok is False
    assert result.content["available"] == ["Todo"]


async def test_tools_fail_structurally_without_issue_context() -> None:
    adapter = make_adapter()
    result = await adapter.execute_agent_tool(
        "github_set_project_status", {"status": "Todo"}, ToolContext()
    )
    assert result.ok is False


async def test_tool_errors_are_returned_not_raised() -> None:
    recorder = Recorder(
        [
            httpx.Response(200, json=list_page([issue_item()])),
            httpx.Response(500, text="boom"),
        ]
    )
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    result = await adapter.execute_agent_tool(
        "github_set_project_status", {"status": "Todo"}, ToolContext(issue=issue)
    )
    assert result.ok is False
    assert result.content["category"] == "tracker_status"


async def test_add_issue_comment_targets_the_underlying_issue_node() -> None:
    recorder = Recorder(
        [
            httpx.Response(200, json=list_page([issue_item()])),
            httpx.Response(
                200,
                json={
                    "data": {
                        "addComment": {
                            "commentEdge": {"node": {"id": "IC_1", "url": "https://github.com/c/1"}}
                        }
                    }
                },
            ),
        ]
    )
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    result = await adapter.execute_agent_tool(
        "github_add_issue_comment", {"body": "done"}, ToolContext(issue=issue)
    )
    assert result.ok is True
    assert recorder.bodies[-1]["variables"]["subject"] == "I_node12"


async def test_add_issue_comment_rejects_draft_items() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([draft_item()]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["Todo"])
    result = await adapter.execute_agent_tool(
        "github_add_issue_comment", {"body": "hi"}, ToolContext(issue=issue)
    )
    assert result.ok is False
    assert result.content["content_type"] == "DraftIssue"


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


async def test_aclose_is_idempotent() -> None:
    adapter = make_adapter()
    await adapter.aclose()
    await adapter.aclose()


async def test_issue_round_trips_through_the_template_context() -> None:
    recorder = Recorder([httpx.Response(200, json=list_page([issue_item()]))])
    adapter = make_adapter(recorder)
    (issue,) = await adapter.fetch_issues_by_states(["In Progress"])
    context = issue.to_template_context()
    assert json.loads(json.dumps(context))["native_ref"]["repository"] == f"{OWNER}/hello-world"
    assert TOKEN not in json.dumps(context)


@pytest.mark.integration
async def test_live_project_read() -> None:  # pragma: no cover - requires credentials
    """SPEC 17.8 Real Integration Profile. Requires GITHUB_TOKEN and a real board."""
    import os

    owner = os.environ.get("SYMPHONY_GH_OWNER")
    number = os.environ.get("SYMPHONY_GH_PROJECT")
    if not owner or not number:
        pytest.skip("SYMPHONY_GH_OWNER / SYMPHONY_GH_PROJECT not configured")
    adapter = GitHubProjectsAdapter({"owner": owner, "project_number": int(number)})
    try:
        issues = await adapter.fetch_issues_by_states(list(adapter.default_active_states))
        assert all(issue.id and issue.identifier for issue in issues)
    finally:
        await adapter.aclose()
