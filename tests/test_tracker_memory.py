"""Conformance tests for the in-process ``memory`` tracker adapter.

Covers the SPEC 17.3 test matrix against the SPEC 11.1 read kernel, the
SPEC 11.3 normalization rules, the SPEC 11.4 error categories, and the
SPEC 10.5 provider-native tool extension.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from symphony.trackers.base import ToolContext, adapter_kinds, build_adapter
from symphony.trackers.memory import PROVIDER_KEYS, MemoryTrackerAdapter, ProviderCall

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class RecordingLogger:
    """Duck-typed stand-in for the structured logger (SPEC 13.1)."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, Any]]] = []

    def bind(self, **_: Any) -> RecordingLogger:
        return self

    def debug(self, message: str, **fields: Any) -> None:
        self.entries.append(("debug", message, fields))

    def info(self, message: str, **fields: Any) -> None:
        self.entries.append(("info", message, fields))

    def warning(self, message: str, **fields: Any) -> None:
        self.entries.append(("warning", message, fields))

    def error(self, message: str, **fields: Any) -> None:
        self.entries.append(("error", message, fields))

    @property
    def warnings(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [e for e in self.entries if e[0] == "warning"]


def record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "T-1",
        "identifier": "ABC-1",
        "title": "Fix the login form",
        "state": "Todo",
    }
    base.update(overrides)
    return base


FIXED_NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def tracker() -> MemoryTrackerAdapter:
    return MemoryTrackerAdapter(
        {
            "seed": [
                record(id="T-1", identifier="ABC-1", title="One", state="Todo", priority=2),
                record(id="T-2", identifier="ABC-2", title="Two", state="In Progress"),
                record(id="T-3", identifier="ABC-3", title="Three", state="Done"),
            ]
        },
        clock=lambda: FIXED_NOW,
    )


# --------------------------------------------------------------------------
# Registration and construction (SPEC 11.2, 5.3.1, 6.3)
# --------------------------------------------------------------------------


def test_adapter_is_registered_under_kind_memory() -> None:
    assert "memory" in adapter_kinds()
    built = build_adapter("memory", {"seed": []})
    assert isinstance(built, MemoryTrackerAdapter)
    assert built.kind == "memory"


def test_adapter_documents_default_states() -> None:
    # SPEC 5.3.1: active/terminal states may be omitted only when the adapter
    # profile documents defaults.
    assert MemoryTrackerAdapter.default_active_states == ("Todo", "In Progress")
    assert MemoryTrackerAdapter.default_terminal_states == ("Done", "Canceled")
    assert MemoryTrackerAdapter().active_states == ("Todo", "In Progress")


def test_effective_states_come_from_construction_kwargs() -> None:
    adapter = MemoryTrackerAdapter({}, active_states=["Ready"], terminal_states=["Shipped"])
    assert adapter.active_states == ("Ready",)
    assert adapter.terminal_states == ("Shipped",)


def test_bare_construction_is_repl_friendly() -> None:
    adapter = MemoryTrackerAdapter()
    adapter.add(id="1", identifier="X-1", title="t", state="Todo")
    assert [r["identifier"] for r in adapter.records()] == ["X-1"]


@pytest.mark.parametrize(
    "provider",
    [
        {"seed": "not-a-list"},
        {"seed": ["not-a-mapping"]},
        {"page_size": -1},
        {"page_size": "2"},
        {"max_ids_per_request": -3},
        {"require_assignee": "yes"},
        {"scope": 7},
        {"endpoint": "https://example.invalid"},
    ],
)
def test_invalid_provider_config_raises_invalid_tracker_config(provider: dict) -> None:
    with pytest.raises(InvalidTrackerConfig) as exc:
        MemoryTrackerAdapter(provider)
    assert exc.value.category == "invalid_tracker_config"


def test_provider_must_be_a_mapping() -> None:
    with pytest.raises(InvalidTrackerConfig):
        MemoryTrackerAdapter(["seed"])  # type: ignore[arg-type]


def test_unknown_provider_key_names_the_supported_set() -> None:
    with pytest.raises(InvalidTrackerConfig) as exc:
        MemoryTrackerAdapter({"tocken": "oops"})
    assert exc.value.details["unknown"] == ["tocken"]
    assert set(exc.value.details["supported"]) == set(PROVIDER_KEYS)


def test_duplicate_dispatch_id_in_scope_is_a_config_error() -> None:
    with pytest.raises(InvalidTrackerConfig, match="duplicate dispatch id"):
        MemoryTrackerAdapter({"seed": [record(id="T-1"), record(id="T-1", identifier="ABC-2")]})


def test_duplicate_identifier_in_scope_is_a_config_error() -> None:
    with pytest.raises(InvalidTrackerConfig, match="duplicate identifier"):
        MemoryTrackerAdapter({"seed": [record(id="T-1"), record(id="T-2")]})


def test_same_identifier_in_a_different_scope_is_allowed() -> None:
    adapter = MemoryTrackerAdapter(
        {
            "scope": "team-a",
            "seed": [record(id="T-1", scope="team-a"), record(id="T-9", scope="team-b")],
        }
    )
    assert [r["id"] for r in adapter.records()] == ["T-1"]


def test_missing_secret_env_raises_missing_tracker_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYMPHONY_MEMORY_TOKEN", raising=False)
    with pytest.raises(MissingTrackerSecret) as exc:
        MemoryTrackerAdapter({"secret_env": "SYMPHONY_MEMORY_TOKEN"})
    assert exc.value.category == "missing_tracker_secret"

    # SPEC 5.3.1: a documented secret resolving to '' is *missing*, not present.
    monkeypatch.setenv("SYMPHONY_MEMORY_TOKEN", "   ")
    with pytest.raises(MissingTrackerSecret):
        MemoryTrackerAdapter({"secret_env": "SYMPHONY_MEMORY_TOKEN"})


def test_secret_env_is_declared_but_never_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYMPHONY_MEMORY_TOKEN", "super-secret-value")
    adapter = MemoryTrackerAdapter({"secret_env": "SYMPHONY_MEMORY_TOKEN"})
    assert adapter.secret_environment_names() == ["SYMPHONY_MEMORY_TOKEN"]
    # SPEC 15.3: the credential value never lands in adapter state or repr.
    assert "super-secret-value" not in repr(adapter)
    assert "super-secret-value" not in repr(vars(adapter))


def test_no_secret_env_declares_no_secret_names() -> None:
    assert MemoryTrackerAdapter().secret_environment_names() == []


# --------------------------------------------------------------------------
# SPEC 11.1 read kernel — state list
# --------------------------------------------------------------------------


async def test_state_fetch_applies_configured_active_states(
    tracker: MemoryTrackerAdapter,
) -> None:
    issues = await tracker.fetch_issues_by_states(["Todo", "In Progress"])
    assert [i.identifier for i in issues] == ["ABC-1", "ABC-2"]


async def test_state_comparison_is_case_insensitive_and_preserves_spelling(
    tracker: MemoryTrackerAdapter,
) -> None:
    issues = await tracker.fetch_issues_by_states(["  IN PROGRESS  "])
    assert [i.state for i in issues] == ["In Progress"]
    assert issues[0].normalized_state == "in progress"


async def test_empty_state_list_returns_empty_without_a_provider_call() -> None:
    adapter = MemoryTrackerAdapter({"seed": [record()]})
    adapter.fail_requests("should never fire")
    assert await adapter.fetch_issues_by_states([]) == []
    assert adapter.provider_calls == 0


async def test_empty_id_list_returns_empty_without_a_provider_call() -> None:
    adapter = MemoryTrackerAdapter({"seed": [record()]})
    adapter.fail_requests("should never fire")
    assert await adapter.fetch_issues_by_ids([]) == []
    assert adapter.provider_calls == 0


async def test_state_fetch_includes_non_dispatchable_issues() -> None:
    # SPEC 11.1: the scheduler owns the dispatchable filter, not the adapter.
    adapter = MemoryTrackerAdapter({"seed": [record(dispatchable=False)]})
    issues = await adapter.fetch_issues_by_states(["Todo"])
    assert [i.dispatchable for i in issues] == [False]


async def test_state_fetch_applies_provider_scope_selection() -> None:
    adapter = MemoryTrackerAdapter(
        {
            "scope": "team-a",
            "seed": [
                record(id="T-1", identifier="ABC-1", scope="team-a"),
                record(id="T-2", identifier="ABC-2", scope="team-b"),
                record(id="T-3", identifier="ABC-3", scope="team-a"),
            ],
        }
    )
    issues = await adapter.fetch_issues_by_states(["Todo"])
    assert [i.identifier for i in issues] == ["ABC-1", "ABC-3"]


async def test_pagination_preserves_order_across_pages() -> None:
    seed = [record(id=f"T-{n}", identifier=f"ABC-{n}", title=f"#{n}") for n in range(1, 6)]
    adapter = MemoryTrackerAdapter({"seed": seed, "page_size": 2})
    issues = await adapter.fetch_issues_by_states(["Todo"])
    assert [i.identifier for i in issues] == ["ABC-1", "ABC-2", "ABC-3", "ABC-4", "ABC-5"]
    assert adapter.provider_calls == 3
    assert [c.op for c in adapter.calls] == ["fetch_issues_by_states"] * 3


async def test_state_fetch_makes_one_provider_call_even_when_nothing_matches(
    tracker: MemoryTrackerAdapter,
) -> None:
    assert await tracker.fetch_issues_by_states(["Nonexistent"]) == []
    assert tracker.provider_calls == 1


# --------------------------------------------------------------------------
# SPEC 11.1 read kernel — ID refresh
# --------------------------------------------------------------------------


async def test_id_refresh_returns_full_normalized_snapshots() -> None:
    adapter = MemoryTrackerAdapter(
        {
            "seed": [
                record(
                    labels=["Backend"],
                    priority=1,
                    url="https://example.invalid/ABC-1",
                    branch_name="feat/abc-1",
                    assignee_id="u-7",
                    description="Long body",
                    created_at="2026-01-02T03:04:05Z",
                )
            ]
        }
    )
    adapter.update("T-1", state="In Progress", labels=["Backend", "urgent"])
    (issue,) = await adapter.fetch_issues_by_ids(["T-1"])
    # Not just the state string — labels/priority/url/branch all refresh too.
    assert issue.state == "In Progress"
    assert issue.labels == ("backend", "urgent")
    assert issue.priority == 1
    assert issue.url == "https://example.invalid/ABC-1"
    assert issue.branch_name == "feat/abc-1"
    assert issue.assignee_id == "u-7"
    assert issue.description == "Long body"
    assert issue.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


async def test_id_refresh_omits_ids_no_longer_visible(tracker: MemoryTrackerAdapter) -> None:
    issues = await tracker.fetch_issues_by_ids(["T-1", "T-404"])
    assert [i.id for i in issues] == ["T-1"]


async def test_id_refresh_omits_ids_outside_the_configured_scope() -> None:
    adapter = MemoryTrackerAdapter(
        {
            "scope": "team-a",
            "seed": [
                record(id="T-1", identifier="ABC-1", scope="team-a"),
                record(id="T-2", identifier="ABC-2", scope="team-b"),
            ],
        }
    )
    issues = await adapter.fetch_issues_by_ids(["T-1", "T-2"])
    assert [i.id for i in issues] == ["T-1"]


async def test_id_refresh_treats_input_as_a_set(tracker: MemoryTrackerAdapter) -> None:
    issues = await tracker.fetch_issues_by_ids(["T-1", "T-1", "T-2", "  ", "T-1"])
    assert [i.id for i in issues] == ["T-1", "T-2"]


async def test_id_refresh_honors_provider_request_limits() -> None:
    seed = [record(id=f"T-{n}", identifier=f"ABC-{n}") for n in range(1, 6)]
    adapter = MemoryTrackerAdapter({"seed": seed, "max_ids_per_request": 2})
    issues = await adapter.fetch_issues_by_ids([f"T-{n}" for n in range(1, 6)])
    assert len(issues) == 5
    assert adapter.provider_calls == 3


async def test_id_refresh_sees_records_in_any_state(tracker: MemoryTrackerAdapter) -> None:
    # Reconciliation must be able to observe a terminal state (SPEC 8.5).
    (issue,) = await tracker.fetch_issues_by_ids(["T-3"])
    assert issue.state == "Done"


# --------------------------------------------------------------------------
# SPEC 11.1 malformed-record asymmetry
# --------------------------------------------------------------------------


async def test_state_fetch_omits_and_logs_malformed_records() -> None:
    logger = RecordingLogger()
    adapter = MemoryTrackerAdapter(
        {
            "seed": [
                record(id="T-1", identifier="ABC-1", title="Good"),
                record(id="T-2", identifier="ABC-2", title="   "),
            ]
        },
        logger=logger,
    )
    issues = await adapter.fetch_issues_by_states(["Todo"])
    assert [i.id for i in issues] == ["T-1"]
    assert len(adapter.last_normalization_report.omitted) == 1
    assert "title" in adapter.last_normalization_report.omitted[0]
    assert len(logger.warnings) == 1
    _, message, fields = logger.warnings[0]
    assert "omitted" in message
    # SPEC 13.1 requires issue_id / issue_identifier context on issue logs.
    assert fields["issue_id"] == "T-2"
    assert fields["issue_identifier"] == "ABC-2"


@pytest.mark.parametrize("field_name", ["identifier", "title", "state"])
async def test_id_refresh_fails_on_a_malformed_requested_record(field_name: str) -> None:
    adapter = MemoryTrackerAdapter({"seed": [record(id="T-1"), record(id="T-2", identifier="X-2")]})
    adapter.corrupt("T-2", field_name, "")
    with pytest.raises(TrackerResponseError) as exc:
        await adapter.fetch_issues_by_ids(["T-2"])
    assert exc.value.category == "tracker_response"
    assert exc.value.details["field"] == field_name


async def test_a_record_without_any_dispatch_identity_is_simply_invisible() -> None:
    # It cannot be *requested*, so there is nothing to fail; the state-list read
    # still reports it as malformed.
    logger = RecordingLogger()
    adapter = MemoryTrackerAdapter(
        {"seed": [record(id="", identifier="ABC-1")]},
        logger=logger,
    )
    assert await adapter.fetch_issues_by_ids(["T-1"]) == []
    assert await adapter.fetch_issues_by_states(["Todo"]) == []
    assert "'id'" in logger.warnings[0][2]["reason"]


async def test_id_refresh_ignores_malformed_records_it_was_not_asked_for() -> None:
    adapter = MemoryTrackerAdapter({"seed": [record(id="T-1"), record(id="T-2", identifier="X-2")]})
    adapter.corrupt("T-2", "title", "")
    issues = await adapter.fetch_issues_by_ids(["T-1"])
    assert [i.id for i in issues] == ["T-1"]


async def test_state_fetch_reports_a_record_whose_state_is_unusable() -> None:
    logger = RecordingLogger()
    adapter = MemoryTrackerAdapter(
        {"seed": [record(id="T-1"), record(id="T-2", identifier="ABC-2", state="")]},
        logger=logger,
    )
    issues = await adapter.fetch_issues_by_states(["Todo"])
    assert [i.id for i in issues] == ["T-1"]
    assert len(logger.warnings) == 1


async def test_explicit_dispatchable_must_be_a_boolean() -> None:
    adapter = MemoryTrackerAdapter({"seed": [record(dispatchable="yes")]})
    with pytest.raises(TrackerResponseError) as exc:
        await adapter.fetch_issues_by_ids(["T-1"])
    assert exc.value.details["field"] == "dispatchable"


# --------------------------------------------------------------------------
# SPEC 11.3 normalization
# --------------------------------------------------------------------------


async def test_labels_are_lowercased_trimmed_deduped_and_blank_dropped() -> None:
    adapter = MemoryTrackerAdapter(
        {"seed": [record(labels=["  Bug ", "BUG", "", {"name": "UI"}, 42, None])]}
    )
    (issue,) = await adapter.fetch_issues_by_ids(["T-1"])
    assert issue.labels == ("bug", "ui")


async def test_unusable_optional_metadata_normalizes_without_hiding_required_fields() -> None:
    adapter = MemoryTrackerAdapter(
        {
            "seed": [
                record(
                    priority="not-a-number",
                    created_at="yesterday",
                    updated_at=None,
                    labels="backend",
                    blocked_by="ABC-9",
                    url=42,
                    branch_name="",
                    description=object(),
                    assignee_id=None,
                )
            ]
        }
    )
    (issue,) = await adapter.fetch_issues_by_ids(["T-1"])
    assert (issue.id, issue.identifier, issue.title, issue.state) == (
        "T-1",
        "ABC-1",
        "Fix the login form",
        "Todo",
    )
    assert issue.priority is None
    assert issue.created_at is None
    assert issue.updated_at is None
    assert issue.labels == ()
    assert issue.blocked_by == ()
    assert issue.url is None
    assert issue.branch_name is None
    assert issue.description is None
    assert issue.assignee_id is None


async def test_priority_coercion_accepts_numeric_strings_and_rejects_bools() -> None:
    adapter = MemoryTrackerAdapter(
        {
            "seed": [
                record(id="T-1", identifier="ABC-1", priority=" 3 "),
                record(id="T-2", identifier="ABC-2", priority=True),
            ]
        }
    )
    issues = await adapter.fetch_issues_by_ids(["T-1", "T-2"])
    assert [i.priority for i in issues] == [3, None]


async def test_native_ref_preserves_the_distinct_ticket_id() -> None:
    adapter = MemoryTrackerAdapter(
        {"seed": [record(id="TCK-9", item_id="PI-1", native_ref={"board": "roadmap"})]}
    )
    (issue,) = await adapter.fetch_issues_by_ids(["PI-1"])
    assert issue.id == "PI-1"
    assert issue.native_ref == {"board": "roadmap", "ticket_id": "TCK-9"}


async def test_native_ref_drops_secretish_and_non_json_safe_entries() -> None:
    adapter = MemoryTrackerAdapter(
        {
            "seed": [
                record(
                    native_ref={
                        "board": "roadmap",
                        "api_key": "sk-live-123",
                        "authorization": "Bearer x",
                        "handle": object(),
                        7: "non-string-key",
                    }
                )
            ]
        }
    )
    (issue,) = await adapter.fetch_issues_by_ids(["T-1"])
    assert issue.native_ref == {"board": "roadmap"}


async def test_native_ref_becomes_null_when_nothing_can_be_retained_safely() -> None:
    adapter = MemoryTrackerAdapter({"seed": [record(native_ref={"token": "abc"})]})
    (issue,) = await adapter.fetch_issues_by_ids(["T-1"])
    assert issue.native_ref is None


# --------------------------------------------------------------------------
# SPEC 11.3 dispatchable derivation
# --------------------------------------------------------------------------


async def test_unresolved_blocker_makes_the_issue_non_dispatchable() -> None:
    adapter = MemoryTrackerAdapter(
        {"seed": [record(blocked_by=[{"identifier": "ABC-9", "state": "In Progress"}])]}
    )
    (issue,) = await adapter.fetch_issues_by_ids(["T-1"])
    assert issue.dispatchable is False
    assert issue.blocked_by[0].identifier == "ABC-9"


async def test_blocker_in_a_terminal_state_leaves_the_issue_dispatchable() -> None:
    adapter = MemoryTrackerAdapter(
        {"seed": [record(blocked_by=[{"identifier": "ABC-9", "state": "done"}])]}
    )
    (issue,) = await adapter.fetch_issues_by_ids(["T-1"])
    assert issue.dispatchable is True


async def test_blocker_resolution_follows_the_configured_terminal_states() -> None:
    seed = [record(blocked_by=[{"identifier": "ABC-9", "state": "Shipped"}])]
    strict = MemoryTrackerAdapter({"seed": seed})
    custom = MemoryTrackerAdapter({"seed": seed}, terminal_states=["Shipped"])
    assert (await strict.fetch_issues_by_ids(["T-1"]))[0].dispatchable is False
    assert (await custom.fetch_issues_by_ids(["T-1"]))[0].dispatchable is True


async def test_archived_records_are_not_dispatchable() -> None:
    adapter = MemoryTrackerAdapter({"seed": [record(archived=True)]})
    (issue,) = await adapter.fetch_issues_by_ids(["T-1"])
    assert issue.dispatchable is False


async def test_require_assignee_routing_rule() -> None:
    adapter = MemoryTrackerAdapter(
        {
            "require_assignee": True,
            "seed": [
                record(id="T-1", identifier="ABC-1"),
                record(id="T-2", identifier="ABC-2", assignee_id="u-7"),
            ],
        }
    )
    issues = await adapter.fetch_issues_by_ids(["T-1", "T-2"])
    assert [i.dispatchable for i in issues] == [False, True]


async def test_explicit_dispatchable_overrides_the_derivation() -> None:
    adapter = MemoryTrackerAdapter({"seed": [record(archived=True, dispatchable=True)]})
    (issue,) = await adapter.fetch_issues_by_ids(["T-1"])
    assert issue.dispatchable is True


# --------------------------------------------------------------------------
# SPEC 11.4 error handling
# --------------------------------------------------------------------------


async def test_injected_transport_failure_maps_to_tracker_request(
    tracker: MemoryTrackerAdapter,
) -> None:
    tracker.fail_requests("connection reset by peer")
    with pytest.raises(TrackerRequestError) as exc:
        await tracker.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "tracker_request"
    assert exc.value.retryable is True
    assert "connection reset by peer" in exc.value.message


async def test_injected_non_success_response_maps_to_tracker_status(
    tracker: MemoryTrackerAdapter,
) -> None:
    tracker.fail_status(503, "upstream unavailable")
    with pytest.raises(TrackerStatusError) as exc:
        await tracker.fetch_issues_by_ids(["T-1"])
    assert exc.value.category == "tracker_status"
    assert exc.value.details["status"] == 503
    assert exc.value.retryable is True


async def test_injected_client_error_status_is_not_retryable(
    tracker: MemoryTrackerAdapter,
) -> None:
    tracker.fail_status(404)
    with pytest.raises(TrackerStatusError) as exc:
        await tracker.fetch_issues_by_states(["Todo"])
    assert exc.value.retryable is False


async def test_injected_rate_limit_maps_to_tracker_rate_limited(
    tracker: MemoryTrackerAdapter,
) -> None:
    tracker.rate_limit(retry_after_ms=1500)
    with pytest.raises(TrackerRateLimited) as exc:
        await tracker.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "tracker_rate_limited"
    assert exc.value.retry_after_ms == 1500
    assert exc.value.to_dict()["retry_after_ms"] == 1500


async def test_injected_pagination_failure_maps_to_tracker_pagination() -> None:
    seed = [record(id=f"T-{n}", identifier=f"ABC-{n}") for n in range(1, 6)]
    adapter = MemoryTrackerAdapter({"seed": seed, "page_size": 2})
    adapter.fail_pagination(after_pages=1)
    with pytest.raises(TrackerPaginationError) as exc:
        await adapter.fetch_issues_by_states(["Todo"])
    assert exc.value.category == "tracker_pagination"
    # One page was walked before the failure; the partial walk is not observable.
    assert adapter.provider_calls == 2


async def test_faults_can_be_scoped_to_a_number_of_calls(tracker: MemoryTrackerAdapter) -> None:
    tracker.fail_requests(times=1)
    with pytest.raises(TrackerRequestError):
        await tracker.fetch_issues_by_states(["Todo"])
    assert tracker.faults.armed is False
    assert len(await tracker.fetch_issues_by_states(["Todo"])) == 1


async def test_clear_faults_disarms_everything(tracker: MemoryTrackerAdapter) -> None:
    tracker.rate_limit()
    tracker.clear_faults()
    assert tracker.faults.armed is False
    assert len(await tracker.fetch_issues_by_states(["Todo"])) == 1


async def test_reads_after_aclose_fail_as_transport_errors(tracker: MemoryTrackerAdapter) -> None:
    await tracker.aclose()
    await tracker.aclose()  # idempotent
    assert tracker.closed is True
    with pytest.raises(TrackerRequestError):
        await tracker.fetch_issues_by_states(["Todo"])


def test_reset_calls_clears_recorded_provider_traffic(tracker: MemoryTrackerAdapter) -> None:
    tracker.calls.append(ProviderCall("probe"))
    assert tracker.provider_calls == 1
    tracker.reset_calls()
    assert tracker.provider_calls == 0


# --------------------------------------------------------------------------
# SPEC 10.5 / 11.5 provider-native agent tools
# --------------------------------------------------------------------------


def test_agent_tool_specs_declare_mutation_capability(tracker: MemoryTrackerAdapter) -> None:
    specs = {s.name: s for s in tracker.agent_tool_specs()}
    assert set(specs) == {"memory_get_issue", "memory_add_comment", "memory_set_state"}
    assert specs["memory_get_issue"].mutates_tracker is False
    assert specs["memory_add_comment"].mutates_tracker is True
    assert specs["memory_set_state"].mutates_tracker is True
    for spec in specs.values():
        assert spec.input_schema["type"] == "object"


async def test_unsupported_tool_name_returns_structured_failure(
    tracker: MemoryTrackerAdapter,
) -> None:
    result = await tracker.execute_agent_tool("memory_delete_everything", {}, ToolContext())
    assert result.ok is False
    assert "unsupported tool" in (result.error or "")
    assert "memory_get_issue" in result.content["supported"]


async def test_get_issue_defaults_to_the_context_issue(tracker: MemoryTrackerAdapter) -> None:
    (issue,) = await tracker.fetch_issues_by_ids(["T-1"])
    result = await tracker.execute_agent_tool("memory_get_issue", {}, ToolContext(issue=issue))
    assert result.ok is True
    assert result.content["issue"]["identifier"] == "ABC-1"
    assert result.content["comments"] == []


async def test_get_issue_without_context_or_argument_fails_structurally(
    tracker: MemoryTrackerAdapter,
) -> None:
    result = await tracker.execute_agent_tool("memory_get_issue", {}, ToolContext())
    assert result.ok is False
    assert "issue_id is required" in (result.error or "")


async def test_add_comment_mutates_and_is_visible_to_a_later_read(
    tracker: MemoryTrackerAdapter,
) -> None:
    result = await tracker.execute_agent_tool(
        "memory_add_comment", {"issue_id": "T-1", "body": "picked this up"}, ToolContext()
    )
    assert result.ok is True
    assert result.content["comment_index"] == 0
    assert result.content["created_at"] == FIXED_NOW.isoformat()
    read = await tracker.execute_agent_tool("memory_get_issue", {"issue_id": "T-1"}, ToolContext())
    assert read.content["comments"] == [
        {"body": "picked this up", "created_at": FIXED_NOW.isoformat()}
    ]


async def test_set_state_mutates_what_the_read_kernel_reports(
    tracker: MemoryTrackerAdapter,
) -> None:
    result = await tracker.execute_agent_tool(
        "memory_set_state", {"issue_id": "T-1", "state": "Human Review"}, ToolContext()
    )
    assert result.ok is True
    assert result.content == {
        "issue_id": "T-1",
        "previous_state": "Todo",
        "state": "Human Review",
    }
    remaining = await tracker.fetch_issues_by_states(["Todo"])
    assert [i.id for i in remaining] == []
    (refreshed,) = await tracker.fetch_issues_by_ids(["T-1"])
    assert refreshed.state == "Human Review"


async def test_tools_respect_the_configured_scope_as_an_authorization_boundary() -> None:
    seed = [
        record(id="T-1", identifier="ABC-1", scope="team-a"),
        record(id="T-2", identifier="ABC-2", scope="team-b"),
    ]
    adapter = MemoryTrackerAdapter({"scope": "team-a", "seed": seed})
    result = await adapter.execute_agent_tool(
        "memory_set_state", {"issue_id": "T-2", "state": "Done"}, ToolContext()
    )
    assert result.ok is False
    assert "not visible in the configured tracker scope" in (result.error or "")
    # The out-of-scope record was not mutated.
    neighbor = MemoryTrackerAdapter({"scope": "team-b", "seed": seed})
    assert [r["state"] for r in neighbor.records()] == ["Todo"]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("memory_add_comment", {"issue_id": "T-1", "body": "   "}),
        ("memory_add_comment", {"issue_id": "T-1"}),
        ("memory_set_state", {"issue_id": "T-1", "state": ""}),
        ("memory_set_state", {"issue_id": "T-1", "state": 42}),
    ],
)
async def test_invalid_tool_arguments_return_structured_failures(
    tracker: MemoryTrackerAdapter, name: str, arguments: dict
) -> None:
    result = await tracker.execute_agent_tool(name, arguments, ToolContext())
    assert result.ok is False
    assert result.error


async def test_non_object_tool_arguments_return_structured_failure(
    tracker: MemoryTrackerAdapter,
) -> None:
    result = await tracker.execute_agent_tool("memory_get_issue", ["T-1"], ToolContext())  # type: ignore[arg-type]
    assert result.ok is False
    assert "JSON object" in (result.error or "")


async def test_tool_calls_surface_injected_faults_without_raising(
    tracker: MemoryTrackerAdapter,
) -> None:
    tracker.rate_limit(retry_after_ms=250)
    result = await tracker.execute_agent_tool(
        "memory_get_issue", {"issue_id": "T-1"}, ToolContext()
    )
    assert result.ok is False
    assert result.content["category"] == "tracker_rate_limited"


async def test_get_issue_reports_a_malformed_record_as_a_tool_failure(
    tracker: MemoryTrackerAdapter,
) -> None:
    tracker.corrupt("T-1", "title", "")
    result = await tracker.execute_agent_tool(
        "memory_get_issue", {"issue_id": "T-1"}, ToolContext()
    )
    assert result.ok is False
    assert result.content["category"] == "tracker_response"


# --------------------------------------------------------------------------
# Record store helpers
# --------------------------------------------------------------------------


async def test_add_update_remove_change_what_the_read_kernel_sees(
    tracker: MemoryTrackerAdapter,
) -> None:
    tracker.add(id="T-4", identifier="ABC-4", title="Four", state="Todo")
    assert [i.id for i in await tracker.fetch_issues_by_states(["Todo"])] == ["T-1", "T-4"]

    tracker.update("T-4", state="Done")
    assert [i.id for i in await tracker.fetch_issues_by_states(["Todo"])] == ["T-1"]

    assert tracker.remove("T-4") is True
    assert tracker.remove("T-404") is False
    assert await tracker.fetch_issues_by_ids(["T-4"]) == []


def test_extend_preserves_order_and_rejects_duplicates(tracker: MemoryTrackerAdapter) -> None:
    tracker.extend([record(id="T-8", identifier="ABC-8"), record(id="T-9", identifier="ABC-9")])
    assert [r["id"] for r in tracker.records()][-2:] == ["T-8", "T-9"]
    with pytest.raises(InvalidTrackerConfig):
        tracker.add(id="T-9", identifier="ABC-99", title="dupe", state="Todo")


def test_update_and_corrupt_reject_unknown_ids(tracker: MemoryTrackerAdapter) -> None:
    with pytest.raises(KeyError):
        tracker.update("T-404", state="Done")
    with pytest.raises(KeyError):
        tracker.corrupt("T-404")


async def test_description_is_preserved_verbatim() -> None:
    # Description is prompt content (SPEC 12.1); trimming it would be lossy.
    adapter = MemoryTrackerAdapter({"seed": [record(description="  line one\n\n  line two  ")]})
    (issue,) = await adapter.fetch_issues_by_ids(["T-1"])
    assert issue.description == "  line one\n\n  line two  "


# --------------------------------------------------------------------------
# SPEC 11.2 — the adapter MUST publish a compact profile in documentation
# --------------------------------------------------------------------------


def test_adapter_publishes_the_required_profile_document() -> None:
    profile = Path(__file__).resolve().parents[1] / "docs" / "adapters" / "memory.md"
    assert profile.is_file(), "SPEC 11.2 requires a published adapter profile"
    text = profile.read_text(encoding="utf-8")
    for required in (
        "kind: memory",
        "native_ref",
        "dispatchable",
        "pagination",
        "memory_get_issue",
        "memory_add_comment",
        "memory_set_state",
    ):
        assert required in text, f"profile is missing {required!r}"
    for key in PROVIDER_KEYS:
        assert f"`{key}`" in text, f"profile does not document provider key {key!r}"
    for category in (
        "invalid_tracker_config",
        "missing_tracker_secret",
        "tracker_request",
        "tracker_status",
        "tracker_response",
        "tracker_pagination",
        "tracker_rate_limited",
    ):
        assert category in text, f"profile does not map error category {category!r}"
