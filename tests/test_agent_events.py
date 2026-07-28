"""Tests for SPEC 10.4 events, SPEC 13.5 token accounting, SPEC 10.5 approvals.

No sibling module is imported: everything here exercises ``symphony.agent.events``
and ``symphony.agent.approvals`` against the already-written domain model, with
hand-built payloads standing in for the app-server transport.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from symphony.agent.approvals import (
    APPROVAL_POLICIES,
    DEFAULT_APPROVAL_POLICY,
    DENY_ALL,
    TRUSTED_AUTO_APPROVE,
    ApprovalDecision,
    ApprovalKind,
    ApprovalPolicy,
    StaticApprovalPolicy,
    classify_approval,
    decide_approval,
    decide_user_input,
    get_approval_policy,
    policy_by_name,
    set_approval_policy,
    unsupported_tool_result,
    user_input_failure,
)
from symphony.agent.events import (
    AGENT_EVENT_NAMES,
    EVENT_NOTIFICATION,
    EVENT_TURN_COMPLETED,
    AgentEvent,
    apply_event_tokens,
    apply_token_totals,
    extract_rate_limits,
    extract_token_totals,
    read_token_counts,
    select_absolute_usage,
    token_delta,
)
from symphony.errors import TurnInputRequired
from symphony.models import CodexTotals, LiveSession

AT = datetime(2026, 2, 24, 20, 15, 30, tzinfo=UTC)


def event(name: str = EVENT_NOTIFICATION, **kwargs: object) -> AgentEvent:
    return AgentEvent(event=name, timestamp=AT, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# SPEC 10.4 — AgentEvent
# --------------------------------------------------------------------------


def test_agent_event_names_are_the_spec_10_4_strings_verbatim() -> None:
    assert AGENT_EVENT_NAMES == (
        "session_started",
        "startup_failed",
        "turn_completed",
        "turn_failed",
        "turn_cancelled",
        "turn_ended_with_error",
        "turn_input_required",
        "approval_auto_approved",
        "unsupported_tool_call",
        "notification",
        "other_message",
        "malformed",
    )


def test_agent_event_defaults_and_immutability() -> None:
    evt = AgentEvent(event=EVENT_TURN_COMPLETED, timestamp=AT)

    assert evt.codex_app_server_pid is None
    assert evt.usage is None
    assert evt.payload == {}
    assert evt.is_known is True

    with pytest.raises(FrozenInstanceError):
        evt.event = "mutated"  # type: ignore[misc]


def test_agent_event_accepts_unlisted_names_because_spec_10_4_is_open() -> None:
    evt = event("thread/item/started")
    assert evt.is_known is False
    assert evt.event == "thread/item/started"


def test_agent_event_to_dict_is_json_safe() -> None:
    evt = AgentEvent(
        event=EVENT_TURN_COMPLETED,
        timestamp=AT,
        codex_app_server_pid="4242",
        usage={"input_tokens": 5},
        payload={"nested": {"when": AT, "items": (1, 2)}},
    )

    rendered = evt.to_dict()

    assert rendered["event"] == "turn_completed"
    assert rendered["timestamp"] == "2026-02-24T20:15:30+00:00"
    assert rendered["codex_app_server_pid"] == "4242"
    assert rendered["payload"] == {"nested": {"when": "2026-02-24T20:15:30+00:00", "items": [1, 2]}}


# --------------------------------------------------------------------------
# SPEC 13.5 — accepted absolute payload shapes
# --------------------------------------------------------------------------


def test_thread_token_usage_updated_notification_is_absolute() -> None:
    payload = {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thread-1",
            "usage": {"input_tokens": 1200, "output_tokens": 800, "total_tokens": 2000},
        },
    }

    assert extract_token_totals(payload) == (1200, 800, 2000)


def test_total_token_usage_inside_token_count_wrapper_is_absolute() -> None:
    payload = {
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": 900,
                "cached_input_tokens": 640,
                "output_tokens": 300,
                "reasoning_output_tokens": 128,
                "total_tokens": 1200,
            },
            "last_token_usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        },
    }

    # Cached/reasoning counters are subsets of input/output and must not inflate.
    assert extract_token_totals(payload) == (900, 300, 1200)


def test_camel_case_token_usage_event_name_still_selects() -> None:
    payload = {
        "type": "tokenUsage/updated",
        "usage": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10},
    }

    assert extract_token_totals(payload) == (7, 3, 10)


# --------------------------------------------------------------------------
# SPEC 13.5 — ignored payload shapes (the trap)
# --------------------------------------------------------------------------


def test_last_token_usage_alone_is_never_a_total() -> None:
    payload = {
        "type": "token_count",
        "info": {"last_token_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
    }

    assert select_absolute_usage(payload) is None
    assert extract_token_totals(payload) is None


def test_absolute_looking_object_nested_inside_a_delta_is_not_harvested() -> None:
    payload = {
        "type": "token_count",
        "last_token_usage": {
            "total_token_usage": {"input_tokens": 99, "output_tokens": 99, "total_tokens": 198}
        },
    }

    assert extract_token_totals(payload) is None


def test_generic_usage_map_on_an_ordinary_event_is_not_cumulative() -> None:
    payload = {
        "type": "notification",
        "usage": {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
    }

    assert extract_token_totals(payload) is None


def test_bare_counts_without_an_absolute_wrapper_are_ignored() -> None:
    assert extract_token_totals({"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}) is None


def test_non_mapping_and_empty_payloads_return_none() -> None:
    assert extract_token_totals({}) is None
    assert extract_token_totals(None) is None  # type: ignore[arg-type]
    assert extract_token_totals("thread/tokenUsage/updated") is None  # type: ignore[arg-type]
    assert select_absolute_usage([1, 2, 3]) is None  # type: ignore[arg-type]


def test_selected_payload_without_recognizable_counts_returns_none() -> None:
    payload = {"total_token_usage": {"model_context_window": 272000}}

    assert select_absolute_usage(payload) == {"model_context_window": 272000}
    assert extract_token_totals(payload) is None


# --------------------------------------------------------------------------
# SPEC 13.5 — lenient field reading inside the selected payload
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}, (3, 4, 7)),
        ({"inputTokens": 3, "outputTokens": 4, "totalTokens": 7}, (3, 4, 7)),
        ({"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}, (3, 4, 7)),
        ({"input": 3, "output": 4, "total": 7}, (3, 4, 7)),
        ({"tokens_in": 3, "tokens_out": 4}, (3, 4, 7)),
        ({"input_tokens": "3", "output_tokens": "4", "total_tokens": "7"}, (3, 4, 7)),
        ({"input_tokens": 3.0, "output_tokens": 4.0}, (3, 4, 7)),
    ],
)
def test_read_token_counts_is_lenient_about_spelling(
    usage: dict[str, object], expected: tuple[int, int, int]
) -> None:
    assert read_token_counts(usage) == expected


def test_missing_total_is_derived_from_input_plus_output() -> None:
    assert read_token_counts({"input_tokens": 10, "output_tokens": 5}) == (10, 5, 15)


def test_only_total_present_leaves_input_and_output_at_zero() -> None:
    assert read_token_counts({"total_tokens": 42}) == (0, 0, 42)


def test_unusable_count_values_are_skipped_not_guessed() -> None:
    assert read_token_counts({}) is None
    assert read_token_counts({"model": "gpt-5"}) is None
    # Booleans are not counts; negatives and non-finite values are malformed.
    assert read_token_counts({"input_tokens": True, "output_tokens": 4}) == (0, 4, 4)
    assert read_token_counts({"input_tokens": -5, "output_tokens": 4}) == (0, 4, 4)
    assert read_token_counts({"input_tokens": float("inf"), "output_tokens": 4}) == (0, 4, 4)
    assert read_token_counts({"input_tokens": "abc", "output_tokens": 4}) == (0, 4, 4)


# --------------------------------------------------------------------------
# SPEC 13.5 — rate-limit tracking
# --------------------------------------------------------------------------


def test_extract_rate_limits_finds_nested_payload() -> None:
    payload = {
        "type": "token_count",
        "rate_limits": {
            "primary": {"used_percent": 12.5, "window_minutes": 300},
            "secondary": {"used_percent": 3.0},
        },
    }

    assert extract_rate_limits(payload) == {
        "primary": {"used_percent": 12.5, "window_minutes": 300},
        "secondary": {"used_percent": 3.0},
    }


def test_extract_rate_limits_accepts_camel_case_and_envelopes() -> None:
    payload = {"params": {"rateLimits": {"primary": {"used_percent": 90}}}}

    assert extract_rate_limits(payload) == {"primary": {"used_percent": 90}}


def test_extract_rate_limits_returns_a_detached_copy() -> None:
    source = {"rate_limits": {"primary": {"used_percent": 10}}}

    snapshot = extract_rate_limits(source)
    source["rate_limits"]["primary"]["used_percent"] = 99  # type: ignore[index]

    assert snapshot == {"primary": {"used_percent": 10}}


def test_extract_rate_limits_returns_none_when_absent_or_contentless() -> None:
    assert extract_rate_limits({"type": "token_count"}) is None
    assert extract_rate_limits({"rate_limits": {}}) is None
    assert extract_rate_limits({"rate_limits": "unknown"}) is None
    assert extract_rate_limits(None) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# SPEC 13.5 — delta arithmetic against last reported totals
# --------------------------------------------------------------------------


def usage_event(input_tokens: int, output_tokens: int, total_tokens: int) -> AgentEvent:
    """An absolute ``thread/tokenUsage/updated`` update."""
    return event(
        payload={
            "method": "thread/tokenUsage/updated",
            "params": {
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                }
            },
        }
    )


def test_monotonic_absolute_totals_are_not_double_counted() -> None:
    session = LiveSession()
    aggregate = CodexTotals()

    for absolute in ((100, 50, 150), (200, 100, 300), (260, 140, 400)):
        apply_event_tokens(usage_event(*absolute), session, aggregate)

    # Aggregate equals the last absolute, not the sum of the three reports.
    assert (aggregate.input_tokens, aggregate.output_tokens, aggregate.total_tokens) == (
        260,
        140,
        400,
    )
    assert session.codex_total_tokens == 400
    assert session.last_reported_total_tokens == 400


def test_repeated_identical_totals_credit_nothing() -> None:
    session = LiveSession()
    aggregate = CodexTotals()

    apply_event_tokens(usage_event(100, 50, 150), session, aggregate)
    delta = apply_event_tokens(usage_event(100, 50, 150), session, aggregate)

    assert delta == (0, 0, 0)
    assert aggregate.total_tokens == 150


def test_decreasing_total_never_drives_the_aggregate_backwards() -> None:
    session = LiveSession()
    aggregate = CodexTotals()

    apply_event_tokens(usage_event(200, 100, 300), session, aggregate)
    delta = apply_event_tokens(usage_event(80, 40, 120), session, aggregate)

    assert delta == (0, 0, 0)
    assert aggregate.total_tokens == 300
    assert session.codex_total_tokens == 300
    # The lower absolute becomes the new baseline...
    assert session.last_reported_total_tokens == 120

    # ...so growth after the reset is credited from that baseline, not swallowed
    # until the old high-water mark is passed again.
    delta = apply_event_tokens(usage_event(100, 50, 150), session, aggregate)
    assert delta == (20, 10, 30)
    assert aggregate.total_tokens == 330


def test_token_delta_is_pure() -> None:
    session = LiveSession(
        last_reported_input_tokens=10,
        last_reported_output_tokens=10,
        last_reported_total_tokens=20,
    )

    assert token_delta(session, (30, 5, 35)) == (20, 0, 15)
    assert session.codex_total_tokens == 0
    assert session.last_reported_total_tokens == 20


def test_components_are_credited_independently() -> None:
    session = LiveSession()
    aggregate = CodexTotals()

    apply_token_totals(session, (100, 100, 200), aggregate)
    delta = apply_token_totals(session, (60, 180, 240), aggregate)

    assert delta == (0, 80, 40)
    assert (aggregate.input_tokens, aggregate.output_tokens, aggregate.total_tokens) == (
        100,
        180,
        240,
    )


def test_aggregate_is_optional() -> None:
    session = LiveSession()

    assert apply_token_totals(session, (5, 5, 10)) == (5, 5, 10)
    assert session.codex_total_tokens == 10


def test_concurrent_sessions_accumulate_into_one_aggregate() -> None:
    first, second = LiveSession(), LiveSession()
    aggregate = CodexTotals()

    apply_event_tokens(usage_event(100, 100, 200), first, aggregate)
    apply_event_tokens(usage_event(30, 20, 50), second, aggregate)
    apply_event_tokens(usage_event(150, 150, 300), first, aggregate)

    assert aggregate.total_tokens == 350
    assert first.codex_total_tokens == 300
    assert second.codex_total_tokens == 50


def test_event_without_absolute_totals_leaves_every_counter_untouched() -> None:
    session = LiveSession()
    aggregate = CodexTotals()

    # A generic usage map on an ordinary event is exactly the shape SPEC 13.5
    # forbids treating as cumulative.
    noise = event(
        EVENT_NOTIFICATION,
        usage={"input_tokens": 999, "output_tokens": 999, "total_tokens": 1998},
        payload={"last_token_usage": {"input_tokens": 7, "output_tokens": 7, "total_tokens": 14}},
    )

    assert apply_event_tokens(noise, session, aggregate) is None
    assert session.codex_total_tokens == 0
    assert session.last_reported_total_tokens == 0
    assert aggregate.total_tokens == 0


def test_usage_field_contributes_only_when_it_carries_an_absolute_wrapper() -> None:
    session = LiveSession()
    aggregate = CodexTotals()
    evt = event(
        EVENT_TURN_COMPLETED,
        usage={"total_token_usage": {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}},
    )

    assert evt.token_totals() == (40, 10, 50)
    assert apply_event_tokens(evt, session, aggregate) == (40, 10, 50)
    assert aggregate.total_tokens == 50


def test_event_rate_limits_accessor_matches_extractor() -> None:
    evt = event(payload={"rate_limits": {"primary": {"used_percent": 5}}})

    assert evt.rate_limits() == {"primary": {"used_percent": 5}}
    assert event().rate_limits() is None


# --------------------------------------------------------------------------
# SPEC 10.5 — approval policy
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_active_policy():
    previous = get_approval_policy()
    yield
    set_approval_policy(previous)


def test_documented_posture_is_the_default() -> None:
    assert DEFAULT_APPROVAL_POLICY is TRUSTED_AUTO_APPROVE
    assert get_approval_policy() is TRUSTED_AUTO_APPROVE
    assert TRUSTED_AUTO_APPROVE.name == "trusted-auto-approve"


def test_documented_posture_auto_approves_commands_and_file_changes_for_the_session() -> None:
    assert decide_approval("execCommandApproval") is ApprovalDecision.APPROVE_FOR_SESSION
    assert decide_approval("applyPatchApproval") is ApprovalDecision.APPROVE_FOR_SESSION
    assert decide_approval("execCommandApproval").remembers_for_session is True


def test_documented_posture_fails_the_run_on_user_input() -> None:
    assert decide_user_input() is ApprovalDecision.FAIL_RUN
    assert decide_approval("turn/inputRequired") is ApprovalDecision.FAIL_RUN
    assert ApprovalDecision.FAIL_RUN.ends_run is True
    assert ApprovalDecision.FAIL_RUN.is_approval is False


def test_unclassifiable_requests_are_denied_not_approved() -> None:
    assert classify_approval({"method": "some/unknownThing"}) is ApprovalKind.UNKNOWN
    assert decide_approval({"method": "some/unknownThing"}) is ApprovalDecision.DENY
    assert decide_approval(None) is ApprovalDecision.DENY


@pytest.mark.parametrize("policy", [TRUSTED_AUTO_APPROVE, DENY_ALL])
def test_no_shipped_policy_can_stall_a_run(policy: ApprovalPolicy) -> None:
    """SPEC 10.5: an approval request must never leave a run waiting forever."""
    for kind in ApprovalKind:
        decision = policy.decide(kind)
        assert isinstance(decision, ApprovalDecision)
        assert decision.is_approval or decision is ApprovalDecision.DENY or decision.ends_run


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        ("execCommandApproval", ApprovalKind.COMMAND_EXECUTION),
        ("exec_command_approval", ApprovalKind.COMMAND_EXECUTION),
        ("applyPatchApproval", ApprovalKind.FILE_CHANGE),
        ("item/fileChange/approvalRequested", ApprovalKind.FILE_CHANGE),
        ("thread/userInput/requested", ApprovalKind.USER_INPUT),
        ("turn/inputRequired", ApprovalKind.USER_INPUT),
        ({"method": "execCommandApproval"}, ApprovalKind.COMMAND_EXECUTION),
        ({"params": {"type": "applyPatchApproval"}}, ApprovalKind.FILE_CHANGE),
        (
            {"method": "unknown", "params": {"command": ["rm", "-rf"]}},
            ApprovalKind.COMMAND_EXECUTION,
        ),
        ({"method": "unknown", "params": {"changes": {"a.py": "..."}}}, ApprovalKind.FILE_CHANGE),
        ({}, ApprovalKind.UNKNOWN),
        (None, ApprovalKind.UNKNOWN),
        (42, ApprovalKind.UNKNOWN),
    ],
)
def test_classify_approval(request_: object, expected: ApprovalKind) -> None:
    assert classify_approval(request_) is expected  # type: ignore[arg-type]


def test_policy_is_swappable_without_touching_call_sites() -> None:
    set_approval_policy(DENY_ALL)

    assert get_approval_policy() is DENY_ALL
    assert decide_approval("execCommandApproval") is ApprovalDecision.DENY
    assert decide_approval("applyPatchApproval") is ApprovalDecision.DENY
    # Still non-stalling under the stricter posture.
    assert decide_user_input() is ApprovalDecision.FAIL_RUN


def test_set_approval_policy_returns_the_previous_posture() -> None:
    previous = set_approval_policy(DENY_ALL)

    assert previous is TRUSTED_AUTO_APPROVE


def test_set_approval_policy_rejects_objects_that_are_not_policies() -> None:
    with pytest.raises(TypeError):
        set_approval_policy(object())  # type: ignore[arg-type]


def test_explicit_policy_argument_overrides_the_active_one() -> None:
    set_approval_policy(DENY_ALL)

    assert decide_approval("execCommandApproval", policy=TRUSTED_AUTO_APPROVE) is (
        ApprovalDecision.APPROVE_FOR_SESSION
    )
    assert decide_user_input(policy=TRUSTED_AUTO_APPROVE) is ApprovalDecision.FAIL_RUN


def test_custom_policy_object_is_honored() -> None:
    review_everything = StaticApprovalPolicy(
        name="review-everything",
        decisions={ApprovalKind.COMMAND_EXECUTION: ApprovalDecision.APPROVE},
        default=ApprovalDecision.FAIL_RUN,
    )
    set_approval_policy(review_everything)

    assert decide_approval("execCommandApproval") is ApprovalDecision.APPROVE
    assert decide_approval("execCommandApproval").remembers_for_session is False
    assert decide_approval("applyPatchApproval") is ApprovalDecision.FAIL_RUN


def test_shipped_policy_tables_cannot_be_mutated_at_runtime() -> None:
    with pytest.raises(TypeError):
        TRUSTED_AUTO_APPROVE.decisions[ApprovalKind.USER_INPUT] = ApprovalDecision.APPROVE  # type: ignore[index]


def test_policy_by_name_resolves_shipped_postures() -> None:
    assert policy_by_name("deny-all") is DENY_ALL
    assert policy_by_name(" Trusted-Auto-Approve ") is TRUSTED_AUTO_APPROVE
    assert set(APPROVAL_POLICIES) == {"trusted-auto-approve", "deny-all"}

    with pytest.raises(ValueError, match="unknown approval policy"):
        policy_by_name("yolo")


def test_user_input_failure_builds_the_spec_10_6_error_without_free_text() -> None:
    error = user_input_failure(
        {"method": "thread/userInput/requested", "params": {"question": "what is the password?"}}
    )

    assert isinstance(error, TurnInputRequired)
    assert error.category == "turn_input_required"
    assert error.details["method"] == "thread/userInput/requested"
    assert error.details["policy"] == "trusted-auto-approve"
    assert "password" not in repr(error.to_dict())


def test_unsupported_tool_call_returns_a_failure_result_and_does_not_raise() -> None:
    result = unsupported_tool_result("delete_everything", supported=("linear_comment",))

    assert result.ok is False
    assert result.error == "unsupported tool: delete_everything"
    assert result.content == {
        "tool_name": "delete_everything",
        "supported_tools": ["linear_comment"],
    }
