"""Tests for :mod:`symphony.orchestrator.scheduling` — SPEC 8.2, 8.3, 17.4.

The config surface is faked here on purpose. ``symphony.workflow.config`` is
owned by another module and is being written concurrently; this suite asserts
scheduling behavior against the CONTRACTS-documented config *interface*, so a
failure here always means the scheduler is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from symphony.models import Issue, OrchestratorState, RunningEntry, normalize_state
from symphony.orchestrator.scheduling import (
    available_slots,
    dispatch_sort_key,
    has_state_slot,
    issue_routable,
    issue_well_formed,
    should_dispatch,
    sort_for_dispatch,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fakes and builders
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeConfig:
    """Stand-in for ``symphony.workflow.config.ServiceConfig`` (CONTRACTS 3).

    Only the surface the scheduler reads is modeled, and it is modeled to the
    contract's documented semantics: state comparison is case-insensitive
    (SPEC 5.3.1) and ``slot_limit_for_state`` falls back to the global limit
    when a state has no override (SPEC 8.3).
    """

    required_labels: tuple[str, ...] = ()
    active_states: tuple[str, ...] = ("todo", "in progress")
    terminal_states: tuple[str, ...] = ("done", "canceled")
    max_concurrent_agents: int = 10
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)

    def is_active(self, state: str) -> bool:
        return normalize_state(state) in {normalize_state(s) for s in self.active_states}

    def is_terminal(self, state: str) -> bool:
        return normalize_state(state) in {normalize_state(s) for s in self.terminal_states}

    def slot_limit_for_state(self, state: str) -> int:
        return self.max_concurrent_agents_by_state.get(
            normalize_state(state), self.max_concurrent_agents
        )


def make_issue(
    identifier: str = "ABC-1",
    *,
    issue_id: str | None = None,
    state: str = "Todo",
    dispatchable: bool = True,
    priority: int | None = None,
    labels: tuple[str, ...] = (),
    created_at: datetime | None = None,
    title: str = "Do the thing",
) -> Issue:
    return Issue(
        id=issue_id if issue_id is not None else f"id-{identifier}",
        identifier=identifier,
        title=title,
        state=state,
        dispatchable=dispatchable,
        priority=priority,
        labels=labels,
        created_at=created_at,
    )


def unchecked_issue(**overrides: object) -> Issue:
    """Build an ``Issue`` bypassing ``__post_init__`` validation.

    SPEC 8.2 states field presence as a dispatch precondition, so the scheduler
    must reject a malformed record even though the normal construction path
    (SPEC 11.3) cannot produce one.
    """
    issue = Issue.__new__(Issue)
    values: dict[str, object] = {
        "id": "id-ABC-1",
        "identifier": "ABC-1",
        "title": "Do the thing",
        "state": "Todo",
        "dispatchable": True,
        "native_ref": None,
        "description": None,
        "priority": None,
        "branch_name": None,
        "url": None,
        "assignee_id": None,
        "labels": (),
        "blocked_by": (),
        "created_at": None,
        "updated_at": None,
    }
    values.update(overrides)
    for name, value in values.items():
        object.__setattr__(issue, name, value)
    return issue


def make_state(
    *running: Issue,
    claimed: tuple[str, ...] = (),
    state_max_concurrent: int = 10,
) -> OrchestratorState:
    """Build an ``OrchestratorState`` with the given issues in ``running``.

    ``claimed`` is populated independently of ``running`` so the two SPEC 8.2
    bullets can be exercised in isolation.
    """
    st = OrchestratorState(max_concurrent_agents=state_max_concurrent)
    for issue in running:
        st.running[issue.id] = RunningEntry(
            issue=issue, identifier=issue.identifier, started_at=NOW
        )
    st.claimed = set(claimed)
    return st


def day(n: int) -> datetime:
    return datetime(2026, 1, n, tzinfo=UTC)


# --------------------------------------------------------------------------
# SPEC 8.2 tier 1 — priority bucketing
# --------------------------------------------------------------------------


def test_priority_1_to_4_bucket_precedes_every_other_priority_including_zero():
    """SPEC 8.2 tier 1 / 17.4 'dispatch sort order is priority then oldest creation'.

    The mixed list is the discriminating case: 0 and 5 and null must all land
    together *after* the 1..4 bucket, ordered among themselves by created_at.
    A key that simply sorts priority ascending puts 0 first; a key that only
    special-cases null puts 0 first and 5 before null. Both are wrong.
    """
    issues = [
        make_issue("A", priority=5, created_at=day(1)),
        make_issue("B", priority=2, created_at=day(6)),
        make_issue("C", priority=0, created_at=day(3)),
        make_issue("D", priority=None, created_at=day(2)),
        make_issue("E", priority=1, created_at=day(7)),
        make_issue("F", priority=4, created_at=day(4)),
    ]

    assert [i.identifier for i in sort_for_dispatch(issues)] == ["E", "B", "F", "A", "D", "C"]


@pytest.mark.parametrize("priority", [0, 5, -3, 99, None])
def test_out_of_bucket_priorities_rank_identically_at_tier_one(priority):
    """SPEC 8.2: every non-1..4 value ranks *with* null, not merely after 4."""
    null_key = dispatch_sort_key(make_issue("X", priority=None))
    other_key = dispatch_sort_key(make_issue("X", priority=priority))

    assert other_key[:2] == null_key[:2]


@pytest.mark.parametrize("priority", [1, 2, 3, 4])
def test_in_bucket_priorities_keep_their_value_at_tier_one(priority):
    assert dispatch_sort_key(make_issue("X", priority=priority))[:2] == (0, priority)


def test_boundary_priorities_five_and_zero_sort_after_priority_four():
    issues = [
        make_issue("zero", priority=0, created_at=day(1)),
        make_issue("five", priority=5, created_at=day(1)),
        make_issue("four", priority=4, created_at=day(9)),
    ]

    ordered = [i.identifier for i in sort_for_dispatch(issues)]

    assert ordered[0] == "four"
    assert set(ordered[1:]) == {"zero", "five"}


def test_out_of_bucket_priorities_order_by_created_at_not_by_priority():
    """Priority 5 created before priority 0 must still come first."""
    issues = [
        make_issue("newer-p0", priority=0, created_at=day(5)),
        make_issue("older-p5", priority=5, created_at=day(1)),
    ]

    assert [i.identifier for i in sort_for_dispatch(issues)] == ["older-p5", "newer-p0"]


def test_bool_priority_does_not_masquerade_as_bucket_priority_one():
    """``True`` is an ``int`` subclass; SPEC 4.1.1 priority is integer or null."""
    key = dispatch_sort_key(unchecked_issue(identifier="X", priority=True))

    assert key[:2] == dispatch_sort_key(make_issue("X", priority=None))[:2]


# --------------------------------------------------------------------------
# SPEC 8.2 tier 2 and 3 — created_at, identifier
# --------------------------------------------------------------------------


def test_created_at_oldest_first_within_the_same_priority():
    issues = [
        make_issue("late", priority=2, created_at=day(9)),
        make_issue("early", priority=2, created_at=day(2)),
        make_issue("middle", priority=2, created_at=day(5)),
    ]

    assert [i.identifier for i in sort_for_dispatch(issues)] == ["early", "middle", "late"]


def test_null_created_at_sorts_last_within_its_priority_tier():
    issues = [
        make_issue("no-date", priority=1, created_at=None),
        make_issue("dated", priority=1, created_at=day(9)),
    ]

    assert [i.identifier for i in sort_for_dispatch(issues)] == ["dated", "no-date"]


def test_null_created_at_does_not_promote_an_issue_past_a_higher_priority_bucket():
    issues = [
        make_issue("null-date-null-prio", priority=None, created_at=None),
        make_issue("dated-p3", priority=3, created_at=day(9)),
    ]

    assert [i.identifier for i in sort_for_dispatch(issues)] == [
        "dated-p3",
        "null-date-null-prio",
    ]


def test_identifier_breaks_ties_lexicographically():
    """The tie-breaker is ``identifier``, not the opaque dispatch ``id`` (SPEC 4.2).

    The ``id`` values are deliberately ordered opposite to the identifiers so a
    key that reached for ``issue.id`` produces the reverse result.
    """
    issues = [
        make_issue("ABC-9", issue_id="z-1", priority=1, created_at=day(3)),
        make_issue("ABC-10", issue_id="z-2", priority=1, created_at=day(3)),
        make_issue("AAA-1", issue_id="z-3", priority=1, created_at=day(3)),
    ]

    assert [i.identifier for i in sort_for_dispatch(issues)] == ["AAA-1", "ABC-10", "ABC-9"]


def test_identifier_tiebreak_also_applies_when_both_created_at_are_null():
    issues = [
        make_issue("ZZ-1", priority=None, created_at=None),
        make_issue("AA-1", priority=None, created_at=None),
    ]

    assert [i.identifier for i in sort_for_dispatch(issues)] == ["AA-1", "ZZ-1"]


def test_sort_is_stable_for_fully_equal_keys():
    """SPEC 8.2 'stable intent': equal keys keep input order."""
    first = make_issue("DUP-1", issue_id="a", priority=2, created_at=day(4))
    second = make_issue("DUP-1", issue_id="b", priority=2, created_at=day(4))

    assert [i.id for i in sort_for_dispatch([first, second])] == ["a", "b"]
    assert [i.id for i in sort_for_dispatch([second, first])] == ["b", "a"]


def test_mixed_naive_and_aware_created_at_sorts_without_raising():
    """Naive instants are read as UTC rather than blowing up the sort."""
    naive = unchecked_issue(
        identifier="naive", id="id-naive", priority=1, created_at=datetime(2026, 1, 1, 0, 0)
    )
    aware = make_issue("aware", priority=1, created_at=day(2))

    assert [i.identifier for i in sort_for_dispatch([aware, naive])] == ["naive", "aware"]


def test_naive_created_at_is_read_as_utc_not_as_local_time():
    """Pins the convention rather than inheriting the host timezone."""
    naive = unchecked_issue(identifier="naive", created_at=datetime(2026, 1, 1, 12, 0))
    aware = make_issue("aware", created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    assert dispatch_sort_key(naive)[3] == dispatch_sort_key(aware)[3]


def test_sub_second_created_at_differences_are_preserved():
    early = make_issue("early", priority=1, created_at=NOW)
    late = make_issue("late", priority=1, created_at=NOW + timedelta(microseconds=1))

    assert [i.identifier for i in sort_for_dispatch([late, early])] == ["early", "late"]


def test_sort_returns_a_new_list_and_does_not_mutate_the_input():
    issues = [make_issue("B", priority=2), make_issue("A", priority=1)]
    original = list(issues)

    result = sort_for_dispatch(issues)

    assert issues == original
    assert result is not issues


def test_sort_accepts_any_iterable_and_handles_empty_input():
    assert sort_for_dispatch([]) == []
    assert [i.identifier for i in sort_for_dispatch(iter([make_issue("A")]))] == ["A"]


# --------------------------------------------------------------------------
# SPEC 8.2 — issue_routable
# --------------------------------------------------------------------------


def test_issue_routable_requires_dispatchable_true():
    """SPEC 17.4: ``dispatchable=false`` issues are not eligible."""
    cfg = FakeConfig()

    assert issue_routable(make_issue(dispatchable=True), cfg) is True
    assert issue_routable(make_issue(dispatchable=False), cfg) is False


def test_issue_routable_requires_every_configured_label():
    cfg = FakeConfig(required_labels=("agent", "ready"))

    assert issue_routable(make_issue(labels=("agent", "ready", "extra")), cfg) is True
    assert issue_routable(make_issue(labels=("agent",)), cfg) is False
    assert issue_routable(make_issue(labels=()), cfg) is False


def test_issue_routable_label_match_ignores_case_and_surrounding_whitespace():
    """SPEC 5.3.1 / 17.4: matching applies after adapter normalization."""
    cfg = FakeConfig(required_labels=("  Agent  ", "READY"))

    assert issue_routable(make_issue(labels=("agent", "ready")), cfg) is True


def test_issue_routable_blank_configured_label_matches_no_issue():
    """SPEC 5.3.1: a blank configured label matches no issue."""
    cfg = FakeConfig(required_labels=("agent", "   "))

    assert issue_routable(make_issue(labels=("agent",)), cfg) is False


def test_issue_routable_with_no_required_labels_accepts_an_unlabeled_issue():
    assert issue_routable(make_issue(labels=()), FakeConfig(required_labels=())) is True


def test_issue_routable_ignores_state_claims_and_concurrency():
    """SPEC 8.2: routability is dispatchable + labels *only*.

    Reconciliation (8.5) and the retry refresh (8.4) call this on issues that
    are already running, already claimed, and holding the last slot. If it
    silently checked state or claims those callers would tear down live work.
    """
    cfg = FakeConfig(required_labels=("agent",), max_concurrent_agents=0)
    issue = make_issue(state="Done", labels=("agent",))

    assert cfg.is_terminal(issue.state) is True
    assert cfg.is_active(issue.state) is False
    assert issue_routable(issue, cfg) is True

    issue_in_unknown_state = make_issue(state="Some Unmapped State", labels=("agent",))

    assert cfg.is_active(issue_in_unknown_state.state) is False
    assert issue_routable(issue_in_unknown_state, cfg) is True


# --------------------------------------------------------------------------
# SPEC 8.2 — issue_well_formed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["id", "identifier", "title", "state"])
def test_issue_well_formed_rejects_blank_required_fields(missing):
    assert issue_well_formed(unchecked_issue(**{missing: "   "})) is False
    assert issue_well_formed(unchecked_issue(**{missing: None})) is False


def test_issue_well_formed_accepts_a_normally_constructed_issue():
    assert issue_well_formed(make_issue()) is True


# --------------------------------------------------------------------------
# SPEC 8.2 — should_dispatch
# --------------------------------------------------------------------------


def test_should_dispatch_accepts_an_eligible_issue():
    cfg = FakeConfig(required_labels=("agent",))
    issue = make_issue(state="Todo", labels=("agent",))

    assert should_dispatch(issue, make_state(), cfg) is True


def test_should_dispatch_rejects_a_malformed_issue():
    assert should_dispatch(unchecked_issue(title=""), make_state(), FakeConfig()) is False


def test_should_dispatch_rejects_a_state_outside_active_states():
    cfg = FakeConfig(active_states=("todo",), terminal_states=("done",))

    assert should_dispatch(make_issue(state="Backlog"), make_state(), cfg) is False


def test_should_dispatch_accepts_active_state_case_insensitively():
    cfg = FakeConfig(active_states=("Todo",))

    assert should_dispatch(make_issue(state="  todo  "), make_state(), cfg) is True


def test_should_dispatch_rejects_a_terminal_state_even_when_also_listed_active():
    """SPEC 8.2 requires both: in active_states *and* not in terminal_states."""
    cfg = FakeConfig(active_states=("todo", "done"), terminal_states=("done",))

    assert should_dispatch(make_issue(state="Done"), make_state(), cfg) is False


def test_should_dispatch_rejects_dispatchable_false():
    assert should_dispatch(make_issue(dispatchable=False), make_state(), FakeConfig()) is False


def test_should_dispatch_rejects_a_missing_required_label():
    cfg = FakeConfig(required_labels=("agent",))

    assert should_dispatch(make_issue(labels=("other",)), make_state(), cfg) is False


def test_should_dispatch_rejects_an_issue_already_running():
    issue = make_issue("ABC-1")

    assert should_dispatch(issue, make_state(issue), FakeConfig()) is False


def test_should_dispatch_rejects_an_issue_already_claimed():
    issue = make_issue("ABC-1")

    assert should_dispatch(issue, make_state(claimed=(issue.id,)), FakeConfig()) is False


def test_should_dispatch_rejects_when_global_slots_are_exhausted():
    cfg = FakeConfig(max_concurrent_agents=1)
    running = make_issue("OTHER-1", state="In Progress")
    candidate = make_issue("ABC-1", state="Todo")

    assert should_dispatch(candidate, make_state(running), cfg) is False


def test_should_dispatch_rejects_when_per_state_slots_are_exhausted():
    cfg = FakeConfig(max_concurrent_agents=10, max_concurrent_agents_by_state={"todo": 1})
    running = make_issue("OTHER-1", state="Todo")
    candidate = make_issue("ABC-1", state="Todo")
    state = make_state(running)

    assert available_slots(state, cfg) > 0
    assert should_dispatch(candidate, state, cfg) is False


def test_should_dispatch_allows_a_different_state_when_one_state_is_saturated():
    cfg = FakeConfig(
        max_concurrent_agents=10,
        max_concurrent_agents_by_state={"todo": 1, "in progress": 5},
    )
    state = make_state(make_issue("OTHER-1", state="Todo"))

    assert should_dispatch(make_issue("ABC-1", state="In Progress"), state, cfg) is True
    assert should_dispatch(make_issue("ABC-2", state="Todo"), state, cfg) is False


# --------------------------------------------------------------------------
# SPEC 8.3 — available_slots
# --------------------------------------------------------------------------


def test_available_slots_is_limit_minus_running_count():
    cfg = FakeConfig(max_concurrent_agents=3)

    assert available_slots(make_state(), cfg) == 3
    assert available_slots(make_state(make_issue("A")), cfg) == 2
    assert available_slots(make_state(make_issue("A"), make_issue("B")), cfg) == 1


def test_available_slots_floors_at_zero_when_over_subscribed():
    cfg = FakeConfig(max_concurrent_agents=1)
    state = make_state(make_issue("A"), make_issue("B"), make_issue("C"))

    assert available_slots(state, cfg) == 0


def test_available_slots_reads_the_limit_from_config_not_from_runtime_state():
    """SPEC 6.2: a re-applied config change affects the next dispatch decision."""
    cfg = FakeConfig(max_concurrent_agents=5)
    state = make_state(state_max_concurrent=1)

    assert available_slots(state, cfg) == 5


def test_available_slots_counts_running_regardless_of_state():
    cfg = FakeConfig(max_concurrent_agents=4)
    state = make_state(
        make_issue("A", state="Todo"),
        make_issue("B", state="In Progress"),
    )

    assert available_slots(state, cfg) == 2


# --------------------------------------------------------------------------
# SPEC 8.3 — has_state_slot
# --------------------------------------------------------------------------


def test_has_state_slot_falls_back_to_the_global_limit_without_an_override():
    cfg = FakeConfig(max_concurrent_agents=2, max_concurrent_agents_by_state={})
    candidate = make_issue("ABC-1", state="Todo")

    assert has_state_slot(candidate, make_state(make_issue("A", state="Todo")), cfg) is True

    saturated = make_state(make_issue("A", state="Todo"), make_issue("B", state="Todo"))

    assert has_state_slot(candidate, saturated, cfg) is False


def test_has_state_slot_uses_the_per_state_override_when_present():
    cfg = FakeConfig(max_concurrent_agents=10, max_concurrent_agents_by_state={"todo": 2})
    candidate = make_issue("ABC-1", state="Todo")
    state = make_state(make_issue("A", state="Todo"), make_issue("B", state="Todo"))

    assert state.running_count() < cfg.max_concurrent_agents
    assert has_state_slot(candidate, state, cfg) is False


def test_has_state_slot_counts_only_issues_currently_tracked_in_that_state():
    """SPEC 8.3: the count is over the running map's *current* tracked states."""
    cfg = FakeConfig(max_concurrent_agents=10, max_concurrent_agents_by_state={"todo": 1})
    state = make_state(
        make_issue("A", state="In Progress"),
        make_issue("B", state="In Progress"),
        make_issue("C", state="In Review"),
    )

    assert has_state_slot(make_issue("ABC-1", state="Todo"), state, cfg) is True


def test_has_state_slot_normalizes_state_keys_on_both_sides():
    """SPEC 5.3.1 / 4.2: state keys and tracked states compare trimmed+lowercased."""
    cfg = FakeConfig(max_concurrent_agents=10, max_concurrent_agents_by_state={"in progress": 1})
    state = make_state(make_issue("A", state="  IN PROGRESS  "))

    assert has_state_slot(make_issue("ABC-1", state="In Progress"), state, cfg) is False


def test_has_state_slot_is_true_when_nothing_is_running():
    cfg = FakeConfig(max_concurrent_agents=1, max_concurrent_agents_by_state={"todo": 1})

    assert has_state_slot(make_issue("ABC-1", state="Todo"), make_state(), cfg) is True


def test_has_state_slot_ignores_claims_and_counts_only_running():
    cfg = FakeConfig(max_concurrent_agents=10, max_concurrent_agents_by_state={"todo": 1})
    state = make_state(claimed=("id-A", "id-B", "id-C"))

    assert has_state_slot(make_issue("ABC-1", state="Todo"), state, cfg) is True
