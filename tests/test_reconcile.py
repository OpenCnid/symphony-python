"""Conformance tests for SPEC 8.5, 8.6, and the 16.3 reference algorithm.

Every collaborator is a local fake. This module deliberately does not import
``symphony.orchestrator.core``, ``symphony.orchestrator.scheduling``, or
``symphony.workflow.config`` at runtime: reconciliation is specified in terms of
what it *decides*, and the tests assert those decisions directly.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from symphony.errors import TrackerRequestError
from symphony.models import (
    Issue,
    LiveSession,
    OrchestratorState,
    RetryEntry,
    RunningEntry,
    normalize_state,
)
from symphony.orchestrator import reconcile as rc

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class FakeCodexConfig:
    stall_timeout_ms: int = 600_000


@dataclass
class FakeConfig:
    """The ``ServiceConfig`` surface reconciliation actually reads (SPEC 5.3)."""

    active_states: tuple[str, ...] = ("in progress", "in review")
    terminal_states: tuple[str, ...] = ("done", "canceled")
    required_labels: tuple[str, ...] = ("agent",)
    codex: FakeCodexConfig = field(default_factory=FakeCodexConfig)

    def is_active(self, state: str) -> bool:
        return normalize_state(state) in {normalize_state(s) for s in self.active_states}

    def is_terminal(self, state: str) -> bool:
        return normalize_state(state) in {normalize_state(s) for s in self.terminal_states}


def fake_routable(issue: Issue, cfg: FakeConfig) -> bool:
    """Stand-in for ``scheduling.issue_routable`` (SPEC 8.2)."""
    return issue.dispatchable and issue.has_labels(cfg.required_labels)


@dataclass(frozen=True)
class TerminateCall:
    issue_id: str
    cleanup_workspace: bool
    reason: str


@dataclass(frozen=True)
class RetryCall:
    issue_id: str
    attempt: int
    identifier: str | None
    error: str


class FakeHost:
    """Stands in for the ``core`` operations reconciliation delegates to.

    ``terminate_running_issue`` is async and ``schedule_retry`` is sync on
    purpose: the host contract tolerates either, and both paths get exercised.
    """

    def __init__(self, *, terminate_returns_state: bool = True) -> None:
        self.terminated: list[TerminateCall] = []
        self.retries: list[RetryCall] = []
        self._terminate_returns_state = terminate_returns_state

    async def terminate_running_issue(
        self,
        state: OrchestratorState,
        issue_id: str,
        *,
        cleanup_workspace: bool,
        reason: str,
    ) -> OrchestratorState | None:
        self.terminated.append(TerminateCall(issue_id, cleanup_workspace, reason))
        state.running.pop(issue_id, None)
        state.claimed.discard(issue_id)
        return state if self._terminate_returns_state else None

    def schedule_retry(
        self,
        state: OrchestratorState,
        issue_id: str,
        *,
        attempt: int,
        identifier: str | None,
        error: str,
    ) -> OrchestratorState:
        self.retries.append(RetryCall(issue_id, attempt, identifier, error))
        state.claimed.add(issue_id)
        state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id, identifier=identifier, attempt=attempt, due_at_ms=0.0, error=error
        )
        return state

    @property
    def terminated_ids(self) -> list[str]:
        return [c.issue_id for c in self.terminated]

    def call_for(self, issue_id: str) -> TerminateCall:
        matches = [c for c in self.terminated if c.issue_id == issue_id]
        assert len(matches) == 1, f"expected exactly one terminate call for {issue_id}: {matches}"
        return matches[0]


class FakeTracker:
    """Scripted SPEC 11.1 read kernel that records every call."""

    def __init__(
        self,
        *,
        by_ids: list[Issue] | None = None,
        by_states: list[Issue] | None = None,
        ids_error: Exception | None = None,
        states_error: Exception | None = None,
    ) -> None:
        self._by_ids = by_ids or []
        self._by_states = by_states or []
        self._ids_error = ids_error
        self._states_error = states_error
        self.id_calls: list[list[str]] = []
        self.state_calls: list[list[str]] = []

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        self.id_calls.append(list(issue_ids))
        if self._ids_error is not None:
            raise self._ids_error
        return list(self._by_ids)

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        self.state_calls.append(list(state_names))
        if self._states_error is not None:
            raise self._states_error
        return list(self._by_states)


class FakeWorkspaces:
    """Stands in for ``WorkspaceManager.cleanup`` (SPEC 9)."""

    def __init__(self, *, missing: tuple[str, ...] = (), failing: tuple[str, ...] = ()) -> None:
        self.calls: list[str] = []
        self._missing = set(missing)
        self._failing = set(failing)

    async def cleanup(self, identifier: str) -> bool:
        self.calls.append(identifier)
        if identifier in self._failing:
            raise OSError(f"permission denied: {identifier}")
        return identifier not in self._missing


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str, msg: str, **fields: Any) -> None:
        self.records.append((level, msg, fields))

    def debug(self, msg: str, **fields: Any) -> None:
        self._record("debug", msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._record("info", msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._record("warning", msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._record("error", msg, **fields)

    def levels_for(self, level: str) -> list[str]:
        return [msg for lvl, msg, _ in self.records if lvl == level]


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def make_issue(
    issue_id: str = "id-1",
    *,
    identifier: str = "ENG-1",
    state: str = "In Progress",
    dispatchable: bool = True,
    labels: tuple[str, ...] = ("agent",),
    title: str = "Ship reconciliation",
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=title,
        state=state,
        dispatchable=dispatchable,
        labels=labels,
    )


def make_entry(
    issue: Issue,
    *,
    started_at: datetime | None = None,
    last_event_at: datetime | None = None,
    retry_attempt: int | None = None,
    worker_handle: Any = None,
) -> RunningEntry:
    session = LiveSession(last_codex_timestamp=last_event_at)
    if last_event_at is not None:
        session.last_codex_event = "codex/event/agent_message"
    return RunningEntry(
        issue=issue,
        identifier=issue.identifier,
        started_at=started_at if started_at is not None else NOW,
        session=session,
        retry_attempt=retry_attempt,
        worker_handle=worker_handle,
        workspace_path=f"/ws/{issue.identifier}",
    )


def make_state(*entries: RunningEntry) -> OrchestratorState:
    state = OrchestratorState()
    for entry in entries:
        state.running[entry.issue.id] = entry
        state.claimed.add(entry.issue.id)
    return state


def deps_for(host: FakeHost, *, now: datetime = NOW, logger: Any = None) -> rc.ReconcileDeps:
    return rc.ReconcileDeps(
        terminate_running_issue=host.terminate_running_issue,
        schedule_retry=host.schedule_retry,
        now=lambda: now,
        routable=fake_routable,
        logger=logger if logger is not None else RecordingLogger(),
    )


# ==========================================================================
# SPEC 8.5 Part A — stall detection
# ==========================================================================


def test_elapsed_measures_from_started_at_when_no_event_seen() -> None:
    entry = make_entry(make_issue(), started_at=NOW - timedelta(seconds=90))
    assert rc.elapsed_ms_for_entry(entry, NOW) == pytest.approx(90_000.0)


def test_elapsed_measures_from_last_event_when_an_event_was_seen() -> None:
    entry = make_entry(
        make_issue(),
        started_at=NOW - timedelta(hours=3),
        last_event_at=NOW - timedelta(seconds=5),
    )
    assert rc.elapsed_ms_for_entry(entry, NOW) == pytest.approx(5_000.0)


def test_naive_started_at_is_treated_as_utc_instead_of_raising() -> None:
    entry = make_entry(make_issue(), started_at=(NOW - timedelta(seconds=30)).replace(tzinfo=None))
    assert rc.elapsed_ms_for_entry(entry, NOW) == pytest.approx(30_000.0)


async def test_run_below_stall_timeout_is_left_alone() -> None:
    cfg = FakeConfig(codex=FakeCodexConfig(stall_timeout_ms=60_000))
    state = make_state(make_entry(make_issue(), started_at=NOW - timedelta(seconds=59)))
    host = FakeHost()

    state = await rc.reconcile_stalled_runs(state, cfg=cfg, deps=deps_for(host))

    assert host.terminated == []
    assert host.retries == []
    assert "id-1" in state.running


async def test_elapsed_exactly_equal_to_timeout_is_not_stalled() -> None:
    """SPEC 8.5: the trigger is ``elapsed_ms > stall_timeout_ms``, strictly."""
    cfg = FakeConfig(codex=FakeCodexConfig(stall_timeout_ms=60_000))
    state = make_state(make_entry(make_issue(), started_at=NOW - timedelta(milliseconds=60_000)))
    host = FakeHost()

    await rc.reconcile_stalled_runs(state, cfg=cfg, deps=deps_for(host))

    assert host.terminated == []


async def test_stalled_run_is_terminated_without_cleanup_and_requeued() -> None:
    cfg = FakeConfig(codex=FakeCodexConfig(stall_timeout_ms=60_000))
    state = make_state(make_entry(make_issue(), started_at=NOW - timedelta(seconds=61)))
    host = FakeHost()

    state = await rc.reconcile_stalled_runs(state, cfg=cfg, deps=deps_for(host))

    assert host.terminated == [
        TerminateCall("id-1", cleanup_workspace=False, reason=rc.REASON_STALLED)
    ]
    assert host.retries == [
        RetryCall("id-1", attempt=1, identifier="ENG-1", error=rc.REASON_STALLED)
    ]
    assert "id-1" not in state.running
    assert state.retry_attempts["id-1"].attempt == 1


async def test_fresh_event_keeps_a_long_running_agent_alive() -> None:
    """Regression guard: measuring from ``started_at`` here would kill live work."""
    cfg = FakeConfig(codex=FakeCodexConfig(stall_timeout_ms=60_000))
    state = make_state(
        make_entry(
            make_issue(),
            started_at=NOW - timedelta(hours=4),
            last_event_at=NOW - timedelta(seconds=10),
        )
    )
    host = FakeHost()

    await rc.reconcile_stalled_runs(state, cfg=cfg, deps=deps_for(host))

    assert host.terminated == []


async def test_stale_event_stalls_even_though_the_run_is_young() -> None:
    cfg = FakeConfig(codex=FakeCodexConfig(stall_timeout_ms=30_000))
    state = make_state(
        make_entry(
            make_issue(),
            started_at=NOW - timedelta(seconds=40),
            last_event_at=NOW - timedelta(seconds=31),
        )
    )
    host = FakeHost()

    await rc.reconcile_stalled_runs(state, cfg=cfg, deps=deps_for(host))

    assert host.terminated_ids == ["id-1"]


@pytest.mark.parametrize("timeout", [0, -1, -600_000])
async def test_non_positive_stall_timeout_disables_detection(timeout: int) -> None:
    """SPEC 8.5: ``<= 0`` means "no deadline", not "everything is stalled"."""
    cfg = FakeConfig(codex=FakeCodexConfig(stall_timeout_ms=timeout))
    state = make_state(make_entry(make_issue(), started_at=NOW - timedelta(days=30)))
    host = FakeHost()

    state = await rc.reconcile_stalled_runs(state, cfg=cfg, deps=deps_for(host))

    assert host.terminated == []
    assert host.retries == []
    assert "id-1" in state.running


async def test_stall_retry_attempt_increments_from_the_running_entry() -> None:
    """SPEC 16.6 ``next_attempt_from(running_entry)``."""
    cfg = FakeConfig(codex=FakeCodexConfig(stall_timeout_ms=1_000))
    state = make_state(
        make_entry(make_issue(), started_at=NOW - timedelta(seconds=10), retry_attempt=3)
    )
    host = FakeHost()

    await rc.reconcile_stalled_runs(state, cfg=cfg, deps=deps_for(host))

    assert host.retries[0].attempt == 4


def test_next_attempt_for_first_run_is_one() -> None:
    assert rc.next_attempt_for(make_entry(make_issue(), retry_attempt=None)) == 1
    assert rc.next_attempt_for(make_entry(make_issue(), retry_attempt=1)) == 2


async def test_stalled_run_is_excluded_from_the_tracker_refresh() -> None:
    """SPEC 16.3 reads ``running_ids`` *after* the stall pass."""
    cfg = FakeConfig(codex=FakeCodexConfig(stall_timeout_ms=60_000))
    alive = make_issue("id-alive", identifier="ENG-A")
    stalled = make_issue("id-stalled", identifier="ENG-S")
    state = make_state(
        make_entry(alive, started_at=NOW - timedelta(seconds=5)),
        make_entry(stalled, started_at=NOW - timedelta(seconds=600)),
    )
    tracker = FakeTracker(by_ids=[alive])
    host = FakeHost()

    await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert tracker.id_calls == [["id-alive"]]
    assert host.terminated_ids == ["id-stalled"]


# ==========================================================================
# SPEC 8.5 Part B / 16.3 — tracker state refresh
# ==========================================================================


async def test_terminal_state_terminates_and_cleans_the_workspace() -> None:
    cfg = FakeConfig()
    running = make_issue()
    state = make_state(make_entry(running))
    tracker = FakeTracker(by_ids=[make_issue(state="Done")])
    host = FakeHost()

    state = await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert host.call_for("id-1") == TerminateCall(
        "id-1", cleanup_workspace=True, reason=rc.REASON_TERMINAL
    )
    assert state.running == {}
    assert host.retries == []


async def test_active_and_routable_updates_the_snapshot_and_leaves_the_worker_alone() -> None:
    cfg = FakeConfig()
    entry = make_entry(make_issue(state="In Progress", title="old title"))
    state = make_state(entry)
    refreshed = make_issue(state="In Review", title="new title")
    tracker = FakeTracker(by_ids=[refreshed])
    host = FakeHost()

    state = await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert host.terminated == []
    assert host.retries == []
    assert state.running["id-1"].issue is refreshed
    assert state.running["id-1"].issue.state == "In Review"


async def test_snapshot_update_preserves_the_rest_of_the_running_entry() -> None:
    cfg = FakeConfig()
    sentinel = object()
    entry = make_entry(make_issue(), worker_handle=sentinel, retry_attempt=2)
    state = make_state(entry)
    tracker = FakeTracker(by_ids=[make_issue(state="In Review")])

    state = await rc.reconcile_running_issues(
        state, cfg=cfg, tracker=tracker, deps=deps_for(FakeHost())
    )

    updated = state.running["id-1"]
    assert updated.worker_handle is sentinel
    assert updated.retry_attempt == 2
    assert updated.started_at == NOW
    assert updated.workspace_path == "/ws/ENG-1"


async def test_active_but_not_dispatchable_terminates_without_cleanup() -> None:
    cfg = FakeConfig()
    state = make_state(make_entry(make_issue()))
    tracker = FakeTracker(by_ids=[make_issue(state="In Progress", dispatchable=False)])
    host = FakeHost()

    await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert host.call_for("id-1") == TerminateCall(
        "id-1", cleanup_workspace=False, reason=rc.REASON_UNROUTABLE
    )


async def test_active_but_missing_required_label_terminates_without_cleanup() -> None:
    cfg = FakeConfig(required_labels=("agent",))
    state = make_state(make_entry(make_issue()))
    tracker = FakeTracker(by_ids=[make_issue(state="In Progress", labels=("bug",))])
    host = FakeHost()

    await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert host.call_for("id-1").cleanup_workspace is False
    assert host.call_for("id-1").reason == rc.REASON_UNROUTABLE


async def test_neither_active_nor_terminal_terminates_without_cleanup() -> None:
    cfg = FakeConfig()
    state = make_state(make_entry(make_issue()))
    tracker = FakeTracker(by_ids=[make_issue(state="Backlog")])
    host = FakeHost()

    await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert host.call_for("id-1") == TerminateCall(
        "id-1", cleanup_workspace=False, reason=rc.REASON_NOT_ACTIVE
    )


async def test_id_absent_from_a_successful_refresh_terminates_without_cleanup() -> None:
    """SPEC 11.1/16.3: omission means "no longer visible", not "unchanged"."""
    cfg = FakeConfig()
    state = make_state(make_entry(make_issue()))
    tracker = FakeTracker(by_ids=[])
    host = FakeHost()

    state = await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert host.call_for("id-1") == TerminateCall(
        "id-1", cleanup_workspace=False, reason=rc.REASON_MISSING
    )
    assert state.running == {}


async def test_terminal_state_wins_when_a_state_is_both_active_and_terminal() -> None:
    """SPEC 16.3 tests the terminal list first, so cleanup still happens."""
    cfg = FakeConfig(active_states=("in progress", "done"), terminal_states=("done",))
    state = make_state(make_entry(make_issue()))
    tracker = FakeTracker(by_ids=[make_issue(state="Done")])
    host = FakeHost()

    await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert host.call_for("id-1").cleanup_workspace is True
    assert host.call_for("id-1").reason == rc.REASON_TERMINAL


async def test_state_comparison_is_case_and_whitespace_insensitive() -> None:
    cfg = FakeConfig()
    state = make_state(make_entry(make_issue()))
    tracker = FakeTracker(by_ids=[make_issue(state="  DONE  ")])
    host = FakeHost()

    await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert host.call_for("id-1").cleanup_workspace is True


async def test_refresh_failure_keeps_every_worker_running() -> None:
    """One transient tracker error must not look like "everything vanished"."""
    cfg = FakeConfig()
    ids = ["id-1", "id-2", "id-3"]
    state = make_state(
        *(make_entry(make_issue(i, identifier=f"ENG-{i}")) for i in ids),
    )
    tracker = FakeTracker(ids_error=TrackerRequestError("connection reset"))
    host = FakeHost()
    logger = RecordingLogger()

    state = await rc.reconcile_running_issues(
        state, cfg=cfg, tracker=tracker, deps=deps_for(host, logger=logger)
    )

    assert host.terminated == []
    assert host.retries == []
    assert sorted(state.running) == ids
    assert state.claimed == set(ids)
    assert any("keeping workers running" in msg for msg in logger.levels_for("debug"))


async def test_refresh_failure_from_a_non_tracker_exception_is_also_survivable() -> None:
    cfg = FakeConfig()
    state = make_state(make_entry(make_issue()))
    tracker = FakeTracker(ids_error=RuntimeError("adapter blew up"))
    host = FakeHost()

    state = await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert host.terminated == []
    assert "id-1" in state.running


async def test_no_running_issues_is_a_no_op_without_any_provider_call() -> None:
    cfg = FakeConfig()
    state = OrchestratorState()
    tracker = FakeTracker(by_ids=[make_issue()])
    host = FakeHost()

    state = await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert tracker.id_calls == []
    assert host.terminated == []
    assert state.running == {}


async def test_refreshed_issue_that_is_not_running_is_ignored() -> None:
    cfg = FakeConfig()
    state = make_state(make_entry(make_issue()))
    stranger = make_issue("id-stranger", identifier="ENG-9", state="Done")
    tracker = FakeTracker(by_ids=[make_issue(state="In Progress"), stranger])
    host = FakeHost()

    state = await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert host.terminated == []
    assert "id-stranger" not in state.running


async def test_mixed_batch_applies_all_four_branches_in_one_tick() -> None:
    cfg = FakeConfig()
    running_ids = ["id-term", "id-ok", "id-unroutable", "id-inactive", "id-gone"]
    state = make_state(
        *(make_entry(make_issue(i, identifier=f"ENG-{i}")) for i in running_ids),
    )
    refreshed_ok = make_issue("id-ok", identifier="ENG-id-ok", state="In Review")
    tracker = FakeTracker(
        by_ids=[
            make_issue("id-term", identifier="ENG-id-term", state="Done"),
            refreshed_ok,
            make_issue(
                "id-unroutable",
                identifier="ENG-id-unroutable",
                state="In Progress",
                dispatchable=False,
            ),
            make_issue("id-inactive", identifier="ENG-id-inactive", state="Backlog"),
        ]
    )
    host = FakeHost()

    state = await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps_for(host))

    assert {c.issue_id: c.cleanup_workspace for c in host.terminated} == {
        "id-term": True,
        "id-unroutable": False,
        "id-inactive": False,
        "id-gone": False,
    }
    assert list(state.running) == ["id-ok"]
    assert state.running["id-ok"].issue is refreshed_ok
    assert host.retries == []


async def test_terminate_callback_may_mutate_in_place_and_return_none() -> None:
    cfg = FakeConfig()
    state = make_state(make_entry(make_issue()))
    tracker = FakeTracker(by_ids=[make_issue(state="Done")])
    host = FakeHost(terminate_returns_state=False)

    returned = await rc.reconcile_running_issues(
        state, cfg=cfg, tracker=tracker, deps=deps_for(host)
    )

    assert returned is state
    assert returned.running == {}


async def test_entry_removed_while_reconciling_is_skipped_not_double_terminated() -> None:
    cfg = FakeConfig()
    state = make_state(
        make_entry(make_issue("id-a", identifier="ENG-A")),
        make_entry(make_issue("id-b", identifier="ENG-B")),
    )
    tracker = FakeTracker(
        by_ids=[
            make_issue("id-a", identifier="ENG-A", state="Done"),
            make_issue("id-b", identifier="ENG-B", state="Done"),
        ]
    )
    host = FakeHost()

    async def terminate_both(
        st: OrchestratorState, issue_id: str, *, cleanup_workspace: bool, reason: str
    ) -> OrchestratorState:
        await host.terminate_running_issue(
            st, issue_id, cleanup_workspace=cleanup_workspace, reason=reason
        )
        st.running.pop("id-b", None)  # simulate a concurrent worker exit
        return st

    deps = rc.ReconcileDeps(
        terminate_running_issue=terminate_both,
        schedule_retry=host.schedule_retry,
        now=lambda: NOW,
        routable=fake_routable,
        logger=RecordingLogger(),
    )

    state = await rc.reconcile_running_issues(state, cfg=cfg, tracker=tracker, deps=deps)

    assert host.terminated_ids == ["id-a"]
    assert state.running == {}


# ==========================================================================
# Pure planning surface
# ==========================================================================


def test_plan_reconciliation_orders_refreshed_first_then_missing() -> None:
    cfg = FakeConfig()
    decisions = rc.plan_reconciliation(
        ["id-1", "id-missing", "id-2"],
        [make_issue("id-2", state="Done"), make_issue("id-1", state="In Progress")],
        cfg,
        routable=fake_routable,
    )

    assert [(d.issue_id, d.action) for d in decisions] == [
        ("id-2", rc.ReconcileAction.TERMINATE_AND_CLEAN),
        ("id-1", rc.ReconcileAction.UPDATE_SNAPSHOT),
        ("id-missing", rc.ReconcileAction.TERMINATE_NO_CLEANUP),
    ]
    assert decisions[2].reason == rc.REASON_MISSING


def test_plan_reconciliation_ignores_duplicate_refreshed_records() -> None:
    cfg = FakeConfig()
    decisions = rc.plan_reconciliation(
        ["id-1"],
        [make_issue("id-1", state="Done"), make_issue("id-1", state="In Progress")],
        cfg,
        routable=fake_routable,
    )

    assert len(decisions) == 1
    assert decisions[0].action is rc.ReconcileAction.TERMINATE_AND_CLEAN


def test_only_the_terminal_branch_requests_workspace_cleanup() -> None:
    cfg = FakeConfig()
    cases = {
        "Done": True,
        "In Progress": False,
        "Backlog": False,
    }
    for state_name, expect_cleanup in cases.items():
        decision = rc.classify_refreshed_issue(
            make_issue(state=state_name), cfg, routable=fake_routable
        )
        assert decision.cleanup_workspace is expect_cleanup, state_name

    unroutable = rc.classify_refreshed_issue(
        make_issue(state="In Progress", dispatchable=False), cfg, routable=fake_routable
    )
    assert unroutable.cleanup_workspace is False
    assert unroutable.terminates is True


def test_plan_stall_terminations_is_empty_for_an_idle_orchestrator() -> None:
    assert rc.plan_stall_terminations(OrchestratorState(), stall_timeout_ms=1, now=NOW) == []


# ==========================================================================
# Dependency seams
# ==========================================================================


def test_deps_defaults_to_the_scheduling_module_seam() -> None:
    deps = rc.ReconcileDeps(
        terminate_running_issue=lambda *a, **k: None, schedule_retry=lambda *a, **k: None
    )
    assert deps.is_routable is rc._default_routable


def test_default_routable_delegates_to_scheduling_issue_routable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam must call the sibling, never reimplement SPEC 8.2 locally."""
    calls: list[tuple[Issue, Any]] = []
    stub = types.ModuleType("symphony.orchestrator.scheduling")

    def issue_routable(issue: Issue, cfg: Any) -> bool:
        calls.append((issue, cfg))
        return False

    stub.issue_routable = issue_routable  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "symphony.orchestrator.scheduling", stub)

    cfg = FakeConfig()
    issue = make_issue(state="In Progress")
    decision = rc.classify_refreshed_issue(issue, cfg)

    assert calls == [(issue, cfg)]
    assert decision.action is rc.ReconcileAction.TERMINATE_NO_CLEANUP
    assert decision.reason == rc.REASON_UNROUTABLE


# ==========================================================================
# SPEC 8.6 — startup terminal workspace cleanup
# ==========================================================================


async def test_startup_cleanup_removes_each_terminal_workspace() -> None:
    cfg = FakeConfig(terminal_states=("Done", "Canceled"))
    tracker = FakeTracker(
        by_states=[
            make_issue("id-1", identifier="ENG-1", state="Done"),
            make_issue("id-2", identifier="ENG-2", state="Canceled"),
        ]
    )
    workspaces = FakeWorkspaces()

    removed = await rc.startup_terminal_workspace_cleanup(
        cfg=cfg, tracker=tracker, workspaces=workspaces, logger=RecordingLogger()
    )

    assert tracker.state_calls == [["Done", "Canceled"]]
    assert workspaces.calls == ["ENG-1", "ENG-2"]
    assert removed == ["ENG-1", "ENG-2"]


async def test_startup_cleanup_reports_only_workspaces_that_existed() -> None:
    cfg = FakeConfig()
    tracker = FakeTracker(
        by_states=[
            make_issue("id-1", identifier="ENG-1", state="Done"),
            make_issue("id-2", identifier="ENG-2", state="Done"),
        ]
    )
    workspaces = FakeWorkspaces(missing=("ENG-2",))

    removed = await rc.startup_terminal_workspace_cleanup(
        cfg=cfg, tracker=tracker, workspaces=workspaces, logger=RecordingLogger()
    )

    assert workspaces.calls == ["ENG-1", "ENG-2"]
    assert removed == ["ENG-1"]


async def test_startup_cleanup_fetch_failure_warns_and_continues_startup() -> None:
    cfg = FakeConfig()
    tracker = FakeTracker(states_error=TrackerRequestError("gateway timeout"))
    workspaces = FakeWorkspaces()
    logger = RecordingLogger()

    removed = await rc.startup_terminal_workspace_cleanup(
        cfg=cfg, tracker=tracker, workspaces=workspaces, logger=logger
    )

    assert removed == []
    assert workspaces.calls == []
    warnings = logger.levels_for("warning")
    assert len(warnings) == 1
    assert "terminal issue fetch failed" in warnings[0]


async def test_startup_cleanup_survives_one_unremovable_workspace() -> None:
    cfg = FakeConfig()
    tracker = FakeTracker(
        by_states=[
            make_issue("id-1", identifier="ENG-1", state="Done"),
            make_issue("id-2", identifier="ENG-2", state="Done"),
            make_issue("id-3", identifier="ENG-3", state="Done"),
        ]
    )
    workspaces = FakeWorkspaces(failing=("ENG-2",))
    logger = RecordingLogger()

    removed = await rc.startup_terminal_workspace_cleanup(
        cfg=cfg, tracker=tracker, workspaces=workspaces, logger=logger
    )

    assert workspaces.calls == ["ENG-1", "ENG-2", "ENG-3"]
    assert removed == ["ENG-1", "ENG-3"]
    assert any("failed to remove terminal workspace" in m for m in logger.levels_for("warning"))


async def test_startup_cleanup_with_no_terminal_states_makes_no_provider_call() -> None:
    cfg = FakeConfig(terminal_states=())
    tracker = FakeTracker(by_states=[make_issue(state="Done")])
    workspaces = FakeWorkspaces()

    removed = await rc.startup_terminal_workspace_cleanup(
        cfg=cfg, tracker=tracker, workspaces=workspaces, logger=RecordingLogger()
    )

    assert tracker.state_calls == []
    assert workspaces.calls == []
    assert removed == []


async def test_startup_cleanup_never_touches_non_terminal_issues() -> None:
    """Startup cleanup is driven purely by the terminal-state query (SPEC 8.6)."""
    cfg = FakeConfig(terminal_states=("done",))
    tracker = FakeTracker(by_states=[make_issue("id-1", identifier="ENG-1", state="done")])
    workspaces = FakeWorkspaces()

    await rc.startup_terminal_workspace_cleanup(
        cfg=cfg, tracker=tracker, workspaces=workspaces, logger=RecordingLogger()
    )

    assert tracker.state_calls == [["done"]]
    assert tracker.id_calls == []
    assert workspaces.calls == ["ENG-1"]
