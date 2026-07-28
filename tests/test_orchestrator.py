"""Conformance tests for ``symphony.orchestrator.core`` — SPEC 17.4.

Every sibling this module depends on (``scheduling``, ``retry``, ``reconcile``,
``workflow.config``, ``agent.runner``, ``observability.logging``,
``workspace.manager``) is faked here: the real implementations are being written
concurrently and none of them is imported. The fakes implement the *spec* rules
(SPEC 8.2, 8.3, 8.4) rather than mirroring an implementation, so a wrong
orchestrator cannot be rescued by an agreeable stub.

Time is fully injected. There are no wall-clock sleeps.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from symphony.models import Issue, OrchestratorState, RunPhase, normalize_state
from symphony.orchestrator.core import Orchestrator, OrchestratorDeps

# --------------------------------------------------------------------------
# Injected clock (SPEC-independent test infrastructure)
# --------------------------------------------------------------------------

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class FakeTimer:
    __slots__ = ("callback", "cancelled", "due_ms", "seq")

    def __init__(self, due_ms: float, seq: int, callback) -> None:
        self.due_ms = due_ms
        self.seq = seq
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeClock:
    """Deterministic clock + timer wheel."""

    def __init__(self) -> None:
        self._mono = 0.0
        self._wall = EPOCH
        self._seq = 0
        self.timers: list[FakeTimer] = []

    def now(self) -> datetime:
        return self._wall

    def monotonic_ms(self) -> float:
        return self._mono

    def call_later_ms(self, delay_ms: float, callback) -> FakeTimer:
        self._seq += 1
        timer = FakeTimer(self._mono + max(float(delay_ms), 0.0), self._seq, callback)
        self.timers.append(timer)
        return timer

    @property
    def pending(self) -> list[FakeTimer]:
        return [t for t in self.timers if not t.cancelled]

    def _set(self, mono_ms: float) -> None:
        delta = mono_ms - self._mono
        if delta <= 0:
            return
        self._mono = mono_ms
        self._wall = self._wall + timedelta(milliseconds=delta)

    def advance(self, ms: float) -> None:
        target = self._mono + ms
        while True:
            due = [t for t in self.timers if not t.cancelled and t.due_ms <= target]
            if not due:
                break
            timer = min(due, key=lambda t: (t.due_ms, t.seq))
            self.timers.remove(timer)
            self._set(timer.due_ms)
            timer.callback()
        self.timers = [t for t in self.timers if not t.cancelled]
        self._set(target)


# --------------------------------------------------------------------------
# Fake collaborators
# --------------------------------------------------------------------------


@dataclass
class FakeCodexConfig:
    stall_timeout_ms: int = 300_000
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    command: str = "codex app-server"


@dataclass
class FakeConfig:
    """Stand-in for ``symphony.workflow.config.ServiceConfig``."""

    poll_interval_ms: int = 30_000
    max_concurrent_agents: int = 10
    max_retry_backoff_ms: int = 300_000
    max_turns: int = 20
    required_labels: tuple[str, ...] = ()
    active_states: tuple[str, ...] = ("Todo", "In Progress")
    terminal_states: tuple[str, ...] = ("Done", "Canceled")
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)
    codex: FakeCodexConfig = field(default_factory=FakeCodexConfig)

    def is_active(self, state: str) -> bool:
        return normalize_state(state) in {normalize_state(s) for s in self.active_states}

    def is_terminal(self, state: str) -> bool:
        return normalize_state(state) in {normalize_state(s) for s in self.terminal_states}

    def slot_limit_for_state(self, state: str) -> int:
        return self.max_concurrent_agents_by_state.get(
            normalize_state(state), self.max_concurrent_agents
        )


class FakeTracker:
    """Stand-in for a ``TrackerAdapter`` read kernel (SPEC 11.1)."""

    def __init__(self) -> None:
        self.by_state: list[Issue] = []
        self.by_id: dict[str, Issue] = {}
        self.state_error: BaseException | None = None
        self.id_error: BaseException | None = None
        self.state_calls: list[list[str]] = []
        self.id_calls: list[list[str]] = []
        self.id_gate: asyncio.Event | None = None
        self.id_gate_seen: asyncio.Event | None = None

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        self.state_calls.append(list(state_names))
        if self.state_error is not None:
            raise self.state_error
        wanted = {normalize_state(s) for s in state_names}
        return [i for i in self.by_state if i.normalized_state in wanted]

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        self.id_calls.append(list(issue_ids))
        if self.id_gate is not None:
            if self.id_gate_seen is not None:
                self.id_gate_seen.set()
            await self.id_gate.wait()
        if self.id_error is not None:
            raise self.id_error
        return [self.by_id[i] for i in issue_ids if i in self.by_id]


class FakeRunner:
    """Stand-in for ``symphony.agent.runner.AgentRunner`` (SPEC 10.7)."""

    def __init__(self) -> None:
        self.started: list[tuple[str, int | None]] = []
        self.spawn_error: BaseException | None = None
        self._gates: dict[str, asyncio.Event] = {}
        self._outcomes: dict[str, BaseException | None] = {}

    def run_attempt(self, issue: Issue, attempt: int | None):
        self.started.append((issue.id, attempt))
        if self.spawn_error is not None:
            raise self.spawn_error
        return self._attempt(issue.id)

    async def _attempt(self, issue_id: str) -> None:
        gate = self._gates.setdefault(issue_id, asyncio.Event())
        await gate.wait()
        outcome = self._outcomes.get(issue_id)
        if outcome is not None:
            raise outcome

    def finish(self, issue_id: str, error: BaseException | None = None) -> None:
        self._outcomes[issue_id] = error
        self._gates.setdefault(issue_id, asyncio.Event()).set()


class FakeWorkspaces:
    def __init__(self) -> None:
        self.cleaned: list[str] = []
        self.error: BaseException | None = None

    async def cleanup(self, identifier: str) -> bool:
        # A real WorkspaceManager does filesystem work through asyncio.to_thread,
        # so cleanup yields to the event loop. Modelling that is what lets
        # cancelled workers actually settle mid-tick.
        for _ in range(3):
            await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        self.cleaned.append(identifier)
        return True


class FakeValidator:
    def __init__(self) -> None:
        self.calls = 0
        self.error: BaseException | None = None

    def __call__(self, config: Any) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def bind(self, **fields: Any) -> RecordingLogger:
        return self

    def _add(self, level: str, msg: str, fields: dict[str, Any]) -> None:
        self.records.append((level, msg, fields))

    def debug(self, msg: str, **fields: Any) -> None:
        self._add("debug", msg, fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._add("info", msg, fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._add("warning", msg, fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._add("error", msg, fields)

    def messages(self, level: str | None = None) -> list[str]:
        return [m for lvl, m, _ in self.records if level is None or lvl == level]


@dataclass
class FakeAgentEvent:
    """Stand-in for ``symphony.agent.events.AgentEvent`` (SPEC 10.4)."""

    event: str
    timestamp: datetime
    codex_app_server_pid: str | None = None
    usage: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Fake scheduling / retry policy — implements SPEC 8.2, 8.3, 8.4 directly
# --------------------------------------------------------------------------


def spec_sort_for_dispatch(issues) -> list[Issue]:
    """SPEC 8.2 sorting: priority bucket 1..4, then oldest created_at, then id."""

    def key(issue: Issue):
        priority = issue.priority
        in_bucket = isinstance(priority, int) and 1 <= priority <= 4
        return (
            0 if in_bucket else 1,
            priority if in_bucket else 0,
            0 if issue.created_at else 1,
            issue.created_at or EPOCH,
            issue.identifier,
        )

    return sorted(issues, key=key)


def spec_issue_routable(issue: Issue, config: FakeConfig) -> bool:
    """SPEC 8.2: routable == dispatchable AND every required label present."""
    return issue.dispatchable and issue.has_labels(config.required_labels)


def spec_available_slots(state: OrchestratorState, config: FakeConfig) -> int:
    return max(config.max_concurrent_agents - state.running_count(), 0)


def spec_has_state_slot(issue: Issue, state: OrchestratorState, config: FakeConfig) -> bool:
    return state.running_count_for_state(issue.state) < config.slot_limit_for_state(issue.state)


def spec_should_dispatch(issue: Issue, state: OrchestratorState, config: FakeConfig) -> bool:
    if not config.is_active(issue.state) or config.is_terminal(issue.state):
        return False
    if not spec_issue_routable(issue, config):
        return False
    if issue.id in state.running or issue.id in state.claimed:
        return False
    if spec_available_slots(state, config) <= 0:
        return False
    return spec_has_state_slot(issue, state, config)


def spec_backoff_delay_ms(attempt: int, max_backoff_ms: int) -> int:
    return min(10_000 * 2 ** (attempt - 1), max_backoff_ms)


def totals_from_payload(payload: dict[str, Any]) -> tuple[int, int, int] | None:
    """Absolute thread totals only (SPEC 13.5); deltas are ignored."""
    usage = payload.get("total_token_usage")
    if not isinstance(usage, dict):
        return None
    return (
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
        int(usage.get("total_tokens", 0)),
    )


def limits_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    limits = payload.get("rate_limits")
    return limits if isinstance(limits, dict) else None


def make_deps(**overrides: Any) -> OrchestratorDeps:
    values: dict[str, Any] = {
        "sort_for_dispatch": spec_sort_for_dispatch,
        "issue_routable": spec_issue_routable,
        "should_dispatch": spec_should_dispatch,
        "available_slots": spec_available_slots,
        "has_state_slot": spec_has_state_slot,
        "backoff_delay_ms": spec_backoff_delay_ms,
        "continuation_delay_ms": 1000,
        "extract_token_totals": totals_from_payload,
        "extract_rate_limits": limits_from_payload,
    }
    values.update(overrides)
    return OrchestratorDeps(**values)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def make_issue(
    issue_id: str,
    *,
    state: str = "Todo",
    identifier: str | None = None,
    priority: int | None = None,
    labels: tuple[str, ...] = (),
    dispatchable: bool = True,
    created_at: datetime | None = None,
) -> Issue:
    ident = identifier or issue_id.upper()
    return Issue(
        id=issue_id,
        identifier=ident,
        title=f"title {ident}",
        state=state,
        dispatchable=dispatchable,
        priority=priority,
        labels=labels,
        created_at=created_at,
    )


@dataclass
class Harness:
    orch: Orchestrator
    tracker: FakeTracker
    runner: FakeRunner
    workspaces: FakeWorkspaces
    clock: FakeClock
    config: FakeConfig
    validator: FakeValidator
    log: RecordingLogger

    @property
    def state(self) -> OrchestratorState:
        return self.orch.state


def build_harness(config: FakeConfig | None = None, **deps_overrides: Any) -> Harness:
    cfg = config or FakeConfig()
    tracker = FakeTracker()
    runner = FakeRunner()
    workspaces = FakeWorkspaces()
    clock = FakeClock()
    validator = FakeValidator()
    log = RecordingLogger()
    orch = Orchestrator(
        config=cfg,
        tracker=tracker,
        runner=runner,
        workspaces=workspaces,
        deps=make_deps(**deps_overrides),
        validate=validator,
        clock=clock,
        logger=log,
    )
    return Harness(orch, tracker, runner, workspaces, clock, cfg, validator, log)


@pytest.fixture
async def h():
    harness = build_harness()
    await harness.orch.start(initial_tick=False)
    try:
        yield harness
    finally:
        await harness.orch.stop()


async def settle(harness: Harness) -> None:
    """Let worker done-callbacks land, then process the whole mailbox."""
    for _ in range(3):
        await asyncio.sleep(0)
    await harness.orch.drain()


async def finish_worker(
    harness: Harness, issue_id: str, error: BaseException | None = None
) -> None:
    task = harness.state.running[issue_id].worker_handle
    harness.runner.finish(issue_id, error)
    await asyncio.gather(task, return_exceptions=True)
    await settle(harness)


async def dispatch(harness: Harness, *issues: Issue) -> None:
    """Run one tick with ``issues`` as the tracker's candidate set."""
    harness.tracker.by_state = list(issues)
    await harness.orch.tick()


# ==========================================================================
# SPEC 16.1 — service startup
# ==========================================================================


async def test_start_performs_startup_terminal_workspace_cleanup():
    harness = build_harness()
    harness.tracker.by_state = [
        make_issue("d1", state="Done", identifier="ENG-1"),
        make_issue("d2", state="Canceled", identifier="ENG-2"),
    ]
    await harness.orch.start(initial_tick=False)
    try:
        assert harness.tracker.state_calls[0] == ["Done", "Canceled"]
        assert harness.workspaces.cleaned == ["ENG-1", "ENG-2"]
    finally:
        await harness.orch.stop()


async def test_start_continues_when_terminal_fetch_fails():
    harness = build_harness()
    harness.tracker.state_error = RuntimeError("provider down")
    await harness.orch.start(initial_tick=False)
    try:
        assert harness.orch.started
        assert harness.workspaces.cleaned == []
        assert "startup terminal cleanup skipped" in harness.log.messages("warning")
    finally:
        await harness.orch.stop()


async def test_start_fails_when_startup_validation_fails():
    harness = build_harness()
    harness.validator.error = RuntimeError("codex.command missing")
    with pytest.raises(RuntimeError, match=re.escape("codex.command missing")):
        await harness.orch.start(initial_tick=False)
    assert not harness.orch.started
    # SPEC 6.3: startup validation runs before startup cleanup.
    assert harness.tracker.state_calls == []


# ==========================================================================
# SPEC 8.1 / 16.2 — poll-and-dispatch tick
# ==========================================================================


async def test_tick_dispatches_in_spec_sort_order(h: Harness):
    old = EPOCH - timedelta(days=3)
    new = EPOCH - timedelta(days=1)
    await dispatch(
        h,
        make_issue("c", identifier="ENG-C", priority=None, created_at=new),
        make_issue("b", identifier="ENG-B", priority=2, created_at=new),
        make_issue("a", identifier="ENG-A", priority=2, created_at=old),
        make_issue("d", identifier="ENG-D", priority=9, created_at=old),
    )
    assert [issue_id for issue_id, _ in h.runner.started] == ["a", "b", "d", "c"]
    assert all(attempt is None for _, attempt in h.runner.started)


async def test_tick_breaks_when_global_slots_are_exhausted(h: Harness):
    h.config.max_concurrent_agents = 2
    await dispatch(
        h,
        make_issue("a", identifier="ENG-A", priority=1),
        make_issue("b", identifier="ENG-B", priority=2),
        make_issue("c", identifier="ENG-C", priority=3),
    )
    assert [issue_id for issue_id, _ in h.runner.started] == ["a", "b"]
    assert h.state.running_count() == 2


async def test_tick_honors_per_state_slot_limit(h: Harness):
    h.config.max_concurrent_agents_by_state = {"in progress": 1}
    await dispatch(
        h,
        make_issue("a", state="In Progress", identifier="ENG-A", priority=1),
        make_issue("b", state="In Progress", identifier="ENG-B", priority=2),
        make_issue("t", state="Todo", identifier="ENG-T", priority=3),
    )
    assert [issue_id for issue_id, _ in h.runner.started] == ["a", "t"]


async def test_tick_skips_non_dispatchable_and_unlabeled_issues(h: Harness):
    h.config.required_labels = ("Symphony",)
    await dispatch(
        h,
        make_issue("no-flag", identifier="ENG-1", labels=("symphony",), dispatchable=False),
        make_issue("no-label", identifier="ENG-2", labels=("other",)),
        make_issue("ok", identifier="ENG-3", labels=("SYMPHONY  ".strip().lower(), "x")),
    )
    assert [issue_id for issue_id, _ in h.runner.started] == ["ok"]


async def test_validation_failure_skips_dispatch_but_reconciliation_already_ran(h: Harness):
    issue = make_issue("a", identifier="ENG-A")
    await dispatch(h, issue)
    assert "a" in h.state.running

    # The issue goes terminal at the same moment the workflow file breaks.
    done = make_issue("a", state="Done", identifier="ENG-A")
    h.tracker.by_id = {"a": done}
    h.tracker.by_state = [make_issue("b", identifier="ENG-B")]
    h.validator.error = RuntimeError("invalid workflow")

    await h.orch.tick()

    assert "a" not in h.state.running
    assert h.workspaces.cleaned == ["ENG-A"]
    assert "a" not in h.state.claimed
    assert [issue_id for issue_id, _ in h.runner.started] == ["a"]
    assert "dispatch validation failed" in h.log.messages("error")


async def test_tick_skips_dispatch_when_candidate_fetch_fails(h: Harness):
    h.tracker.state_error = RuntimeError("tracker 503")
    await h.orch.tick()
    assert h.runner.started == []
    assert "candidate fetch failed" in h.log.messages("error")


async def test_tick_never_dispatches_a_running_or_claimed_issue(h: Harness):
    """SPEC 7.4 — the claim/running check is enforced by core, not only policy."""
    issue = make_issue("a", identifier="ENG-A")
    await dispatch(h, issue)
    assert h.runner.started == [("a", None)]

    # A policy regression that says "always dispatch" must still not duplicate.
    h.orch.deps = make_deps(should_dispatch=lambda issue, state, cfg: True)
    h.tracker.by_id = {"a": issue}
    await dispatch(h, issue)
    assert h.runner.started == [("a", None)]

    # Same guard for an issue that is claimed by a pending retry rather than running.
    await finish_worker(h, "a")
    assert h.state.claim_state("a").value == "RetryQueued"
    await dispatch(h, issue)
    assert h.runner.started == [("a", None)]


async def test_tick_reschedule_arms_next_tick_at_current_poll_interval(h: Harness):
    h.config.poll_interval_ms = 5_000
    await h.orch.apply_config(h.config)
    await h.orch.tick(reschedule=True)
    calls = len(h.tracker.state_calls)

    h.clock.advance(4_999)
    await h.orch.drain()
    assert len(h.tracker.state_calls) == calls

    h.clock.advance(2)
    await h.orch.drain()
    assert len(h.tracker.state_calls) == calls + 1


async def test_observer_failure_does_not_break_the_tick(h: Harness):
    seen: list[int] = []
    h.orch.add_observer(lambda state: (_ for _ in ()).throw(RuntimeError("dashboard down")))
    h.orch.add_observer(lambda state: seen.append(state.running_count()))
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    assert seen == [1]
    assert "observer failed" in h.log.messages("warning")


# ==========================================================================
# SPEC 16.4 — dispatch one issue
# ==========================================================================


async def test_dispatch_records_running_entry_and_takes_claim(h: Harness):
    issue = make_issue("a", identifier="ENG-A")
    await dispatch(h, issue)

    entry = h.state.running["a"]
    assert entry.issue is issue
    assert entry.identifier == "ENG-A"
    assert entry.started_at == h.clock.now()
    assert entry.retry_attempt is None
    assert entry.phase is RunPhase.PREPARING_WORKSPACE
    assert isinstance(entry.worker_handle, asyncio.Task)
    assert h.state.claimed == {"a"}
    assert h.state.claim_state("a").value == "Running"


async def test_dispatch_clears_pending_retry_entry_and_cancels_its_timer(h: Harness):
    issue = make_issue("a", identifier="ENG-A")
    await dispatch(h, issue)
    await finish_worker(h, "a")
    retry_timer = h.state.retry_attempts["a"].timer_handle
    assert retry_timer in h.clock.pending

    h.tracker.by_id = {"a": issue}
    h.clock.advance(1000)
    await h.orch.drain()

    assert "a" not in h.state.retry_attempts
    assert retry_timer.cancelled or retry_timer not in h.clock.timers
    assert h.state.running["a"].retry_attempt == 1


async def test_dispatch_removes_any_pending_retry_entry_and_orphans_no_timer(h: Harness):
    """SPEC 16.4 ends with ``state.retry_attempts.remove(issue.id)``.

    Driven white-box through :meth:`Orchestrator.invoke` because the two
    production callers each already consume the retry entry; the removal exists
    so no code path can leave a live timer pointing at a running issue.
    """
    issue = make_issue("a", identifier="ENG-A")

    def prime(orch: Orchestrator):
        orch.schedule_retry("a", 3, identifier="ENG-A", error="stale")
        return orch.state.retry_attempts["a"].timer_handle

    timer = await h.orch.invoke(prime)
    assert timer in h.clock.pending

    await h.orch.invoke(lambda orch: orch._dispatch_issue(issue, 3))

    assert "a" not in h.state.retry_attempts
    assert timer.cancelled
    assert h.state.running["a"].retry_attempt == 3

    # The dead timer must not resurrect anything when its deadline passes.
    h.clock.advance(120_000)
    await h.orch.drain()
    assert h.state.running["a"].worker_handle is not None
    assert h.runner.started == [("a", 3)]


async def test_failed_spawn_schedules_a_retry_and_holds_the_claim(h: Harness):
    h.runner.spawn_error = RuntimeError("no event loop capacity")
    await dispatch(h, make_issue("a", identifier="ENG-A"))

    assert h.state.running == {}
    entry = h.state.retry_attempts["a"]
    assert entry.attempt == 1
    assert entry.error == "failed to spawn agent"
    assert entry.identifier == "ENG-A"
    # The claim is held, not leaked: the retry owns the issue until it resolves.
    assert h.state.claimed == {"a"}
    assert h.state.claim_state("a").value == "RetryQueued"


# ==========================================================================
# SPEC 7.1 / 16.6 — worker exit
# ==========================================================================


async def test_normal_exit_schedules_a_one_second_continuation_retry(h: Harness):
    """SPEC 7.1: a clean exit does not mean the issue is finished."""
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    due_before = h.clock.monotonic_ms()
    await finish_worker(h, "a")

    entry = h.state.retry_attempts["a"]
    assert entry.attempt == 1
    assert entry.error is None  # the marker that distinguishes continuation
    assert entry.due_at_ms == pytest.approx(due_before + 1000)
    assert h.state.completed == {"a"}
    assert h.state.claimed == {"a"}
    assert h.state.running == {}


async def test_abnormal_exit_schedules_backoff_retry_with_error(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    due_before = h.clock.monotonic_ms()
    await finish_worker(h, "a", RuntimeError("agent turn error"))

    entry = h.state.retry_attempts["a"]
    assert entry.attempt == 1
    assert entry.error == "worker exited: RuntimeError: agent turn error"
    assert entry.due_at_ms == pytest.approx(due_before + 10_000)
    assert "a" not in h.state.completed
    assert h.state.claimed == {"a"}


async def test_repeated_failures_escalate_backoff_and_respect_the_cap(h: Harness):
    h.config.max_retry_backoff_ms = 25_000
    issue = make_issue("a", identifier="ENG-A")
    h.tracker.by_id = {"a": issue}
    await dispatch(h, issue)

    delays: list[float] = []
    for _ in range(4):
        before = h.clock.monotonic_ms()
        await finish_worker(h, "a", RuntimeError("boom"))
        delays.append(h.state.retry_attempts["a"].due_at_ms - before)
        h.clock.advance(h.state.retry_attempts["a"].due_at_ms - h.clock.monotonic_ms())
        await h.orch.drain()

    assert delays == [10_000, 20_000, 25_000, 25_000]
    assert h.state.running["a"].retry_attempt == 4


async def test_worker_exit_accumulates_runtime_seconds(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.clock.advance(4_500)
    await h.orch.drain()
    await finish_worker(h, "a")
    assert h.state.codex_totals.seconds_running == pytest.approx(4.5)


async def test_stale_worker_exit_after_termination_is_ignored(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    worker = h.state.running["a"].worker_handle
    h.tracker.by_id = {"a": make_issue("a", state="Done", identifier="ENG-A")}
    h.tracker.by_state = []

    await h.orch.tick()
    assert h.state.running == {}
    assert h.state.claimed == set()
    assert h.state.retry_attempts == {}

    # The cancelled worker's exit must not resurrect a retry or a claim.
    await asyncio.gather(worker, return_exceptions=True)
    await settle(h)
    assert h.state.retry_attempts == {}
    assert h.state.claimed == set()
    assert h.state.completed == set()


# ==========================================================================
# SPEC 8.4 / 16.6 — retry timer handling
# ==========================================================================


async def _queue_retry(h: Harness, issue: Issue) -> None:
    await dispatch(h, issue)
    await finish_worker(h, "a", RuntimeError("boom"))
    h.tracker.by_state = []


async def test_retry_timer_redispatches_with_the_stored_attempt(h: Harness):
    issue = make_issue("a", identifier="ENG-A")
    await _queue_retry(h, issue)
    h.tracker.by_id = {"a": issue}

    h.clock.advance(10_000)
    await h.orch.drain()

    assert h.runner.started == [("a", None), ("a", 1)]
    assert h.state.running["a"].retry_attempt == 1
    assert "a" not in h.state.retry_attempts


async def test_retry_timer_releases_claim_when_issue_is_no_longer_visible(h: Harness):
    await _queue_retry(h, make_issue("a", identifier="ENG-A"))
    h.tracker.by_id = {}

    h.clock.advance(10_000)
    await h.orch.drain()

    assert h.state.claimed == set()
    assert h.state.retry_attempts == {}
    assert h.workspaces.cleaned == []
    assert h.runner.started == [("a", None)]


async def test_retry_timer_cleans_workspace_and_releases_on_terminal_state(h: Harness):
    await _queue_retry(h, make_issue("a", identifier="ENG-A"))
    h.tracker.by_id = {"a": make_issue("a", state="Done", identifier="ENG-A")}

    h.clock.advance(10_000)
    await h.orch.drain()

    assert h.workspaces.cleaned == ["ENG-A"]
    assert h.state.claimed == set()
    assert h.state.retry_attempts == {}


async def test_retry_timer_releases_claim_when_no_longer_routable(h: Harness):
    await _queue_retry(h, make_issue("a", identifier="ENG-A"))
    h.tracker.by_id = {"a": make_issue("a", identifier="ENG-A", dispatchable=False)}

    h.clock.advance(10_000)
    await h.orch.drain()

    assert h.state.claimed == set()
    assert h.state.retry_attempts == {}
    assert h.workspaces.cleaned == []


async def test_retry_timer_requeues_with_explicit_error_when_slots_are_exhausted(h: Harness):
    h.config.max_concurrent_agents = 1
    issue_a = make_issue("a", identifier="ENG-A")
    await _queue_retry(h, issue_a)
    h.tracker.by_id = {"a": issue_a, "b": make_issue("b", identifier="ENG-B")}

    # Occupy the only slot with a different issue.
    await dispatch(h, make_issue("b", identifier="ENG-B"))
    assert h.state.running_count() == 1

    h.clock.advance(10_000)
    await h.orch.drain()

    entry = h.state.retry_attempts["a"]
    assert entry.attempt == 2
    assert entry.error == "no available orchestrator slots"
    assert entry.identifier == "ENG-A"
    assert h.state.claimed == {"a", "b"}


async def test_retry_timer_requeues_when_only_the_per_state_slot_is_full(h: Harness):
    """SPEC 8.3 — global slots free is not enough; the per-state limit also binds."""
    h.config.max_concurrent_agents = 5
    h.config.max_concurrent_agents_by_state = {"todo": 1}
    issue_a = make_issue("a", identifier="ENG-A")
    await _queue_retry(h, issue_a)
    h.tracker.by_id = {"a": issue_a}

    await dispatch(h, make_issue("b", state="Todo", identifier="ENG-B"))
    assert h.state.running_count() == 1
    assert spec_available_slots(h.state, h.config) == 4  # global slots remain

    h.clock.advance(10_000)
    await h.orch.drain()

    entry = h.state.retry_attempts["a"]
    assert entry.attempt == 2
    assert entry.error == "no available orchestrator slots"
    assert h.runner.started == [("a", None), ("b", None)]


async def test_retry_timer_requeues_when_the_id_refresh_fails(h: Harness):
    await _queue_retry(h, make_issue("a", identifier="ENG-A"))
    h.tracker.id_error = RuntimeError("tracker 500")

    h.clock.advance(10_000)
    await h.orch.drain()

    entry = h.state.retry_attempts["a"]
    assert entry.attempt == 2
    assert entry.error == "retry refresh failed"
    assert h.state.claimed == {"a"}


# ==========================================================================
# SPEC 8.5 / 16.3 — reconciliation
# ==========================================================================


async def test_reconcile_is_a_noop_with_no_running_issues(h: Harness):
    await h.orch.tick()
    assert h.tracker.id_calls == []


async def test_reconcile_refreshes_the_running_issue_snapshot(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A", state="Todo"))
    moved = make_issue("a", identifier="ENG-A", state="In Progress")
    h.tracker.by_id = {"a": moved}
    h.tracker.by_state = []

    await h.orch.tick()

    assert h.state.running["a"].issue is moved
    assert h.state.running_count_for_state("In Progress") == 1


async def test_reconcile_terminal_state_stops_worker_and_cleans_workspace(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    worker = h.state.running["a"].worker_handle
    h.tracker.by_id = {"a": make_issue("a", state="Done", identifier="ENG-A")}
    h.tracker.by_state = []

    await h.orch.tick()

    assert h.state.running == {}
    assert h.workspaces.cleaned == ["ENG-A"]
    assert h.state.claimed == set()
    await asyncio.gather(worker, return_exceptions=True)
    assert worker.cancelled()


async def test_reconcile_non_active_state_stops_worker_without_cleanup(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.tracker.by_id = {"a": make_issue("a", state="Backlog", identifier="ENG-A")}
    h.tracker.by_state = []

    await h.orch.tick()

    assert h.state.running == {}
    assert h.workspaces.cleaned == []
    assert h.state.claimed == set()


async def test_reconcile_active_but_unroutable_stops_worker_without_cleanup(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.tracker.by_id = {"a": make_issue("a", identifier="ENG-A", dispatchable=False)}
    h.tracker.by_state = []

    await h.orch.tick()

    assert h.state.running == {}
    assert h.workspaces.cleaned == []


async def test_reconcile_missing_issue_stops_worker_without_cleanup(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.tracker.by_id = {}
    h.tracker.by_state = []

    await h.orch.tick()

    assert h.state.running == {}
    assert h.workspaces.cleaned == []
    assert h.state.claimed == set()


async def test_reconcile_refresh_failure_keeps_workers_running(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.tracker.id_error = RuntimeError("tracker 502")
    h.tracker.by_state = []

    await h.orch.tick()

    assert "a" in h.state.running
    assert h.state.claimed == {"a"}
    # Wording belongs to symphony.orchestrator.reconcile, which owns SPEC 8.5.
    assert "reconciliation state refresh failed; keeping workers running" in h.log.messages("debug")


async def test_stall_detection_terminates_and_queues_a_retry(h: Harness):
    h.config.codex.stall_timeout_ms = 60_000
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    worker = h.state.running["a"].worker_handle

    h.clock.advance(60_001)
    await h.orch.tick()

    assert h.state.running == {}
    assert h.workspaces.cleaned == []
    entry = h.state.retry_attempts["a"]
    assert entry.attempt == 1
    # Wording belongs to symphony.orchestrator.reconcile, which owns SPEC 8.5 Part A.
    assert "stall" in entry.error.lower() or "no coding-agent activity" in entry.error
    assert h.state.claimed == {"a"}
    await asyncio.gather(worker, return_exceptions=True)
    assert worker.cancelled()


async def test_stall_clock_restarts_from_the_last_agent_event(h: Harness):
    h.config.codex.stall_timeout_ms = 60_000
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.tracker.by_id = {"a": make_issue("a", identifier="ENG-A")}

    h.clock.advance(50_000)
    h.orch.report_agent_event("a", FakeAgentEvent(event="turn_completed", timestamp=h.clock.now()))
    await h.orch.drain()

    h.clock.advance(50_000)
    await h.orch.tick()
    assert "a" in h.state.running

    h.clock.advance(10_002)
    await h.orch.tick()
    assert "a" not in h.state.running


async def test_stall_detection_disabled_when_timeout_is_not_positive(h: Harness):
    h.config.codex.stall_timeout_ms = 0
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.tracker.by_id = {"a": make_issue("a", identifier="ENG-A")}

    h.clock.advance(10_000_000)
    await h.orch.tick()

    assert "a" in h.state.running
    assert h.state.retry_attempts == {}


# ==========================================================================
# SPEC 7.3 / 13.5 — agent updates
# ==========================================================================


async def test_agent_event_updates_live_session_fields(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    stamp = h.clock.now()
    h.orch.report_agent_event(
        "a",
        FakeAgentEvent(
            event="session_started",
            timestamp=stamp,
            codex_app_server_pid="4242",
            payload={"thread_id": "th-1", "turn_id": "tu-9", "message": "hello"},
        ),
    )
    await h.orch.drain()

    session = h.state.running["a"].session
    assert session.last_codex_event == "session_started"
    assert session.last_codex_timestamp == stamp
    assert session.codex_app_server_pid == "4242"
    assert session.session_id == "th-1-tu-9"
    assert session.last_codex_message == "hello"
    assert h.state.running["a"].recent_events[-1]["event"] == "session_started"


async def test_absolute_token_totals_accumulate_as_deltas(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    for total in (100, 250, 250):
        h.orch.report_agent_event(
            "a",
            FakeAgentEvent(
                event="notification",
                timestamp=h.clock.now(),
                payload={
                    "total_token_usage": {
                        "input_tokens": total,
                        "output_tokens": total // 2,
                        "total_tokens": total + total // 2,
                    }
                },
            ),
        )
    await h.orch.drain()

    session = h.state.running["a"].session
    assert session.codex_input_tokens == 250
    assert session.codex_output_tokens == 125
    assert h.state.codex_totals.input_tokens == 250
    assert h.state.codex_totals.output_tokens == 125
    assert h.state.codex_totals.total_tokens == 375


async def test_delta_style_payloads_are_ignored_for_totals(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.orch.report_agent_event(
        "a",
        FakeAgentEvent(
            event="notification",
            timestamp=h.clock.now(),
            payload={"last_token_usage": {"input_tokens": 999, "total_tokens": 999}},
        ),
    )
    await h.orch.drain()
    assert h.state.codex_totals.total_tokens == 0


async def test_latest_rate_limit_payload_is_tracked(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    for used in (10, 40):
        h.orch.report_agent_event(
            "a",
            FakeAgentEvent(
                event="notification",
                timestamp=h.clock.now(),
                payload={"rate_limits": {"used_percent": used}},
            ),
        )
    await h.orch.drain()
    assert h.state.codex_rate_limits == {"used_percent": 40}


async def test_agent_event_for_an_unknown_issue_is_dropped(h: Harness):
    h.orch.report_agent_event("ghost", FakeAgentEvent(event="notification", timestamp=EPOCH))
    await h.orch.drain()
    assert h.state.running == {}
    assert h.state.codex_totals.total_tokens == 0


async def test_report_phase_updates_the_running_entry(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.orch.report_phase("a", RunPhase.STREAMING_TURN)
    await h.orch.drain()
    assert h.state.running["a"].phase is RunPhase.STREAMING_TURN


# ==========================================================================
# SPEC 7 — the orchestrator is the sole mutator
# ==========================================================================


async def test_worker_exit_is_not_applied_while_a_tick_is_in_flight(h: Harness):
    """SPEC 7: worker outcomes are *reported*, never applied by the worker.

    Fails if ``_on_worker_done`` mutates state from the task callback: the
    reconcile continuation would observe a running map that changed underneath
    it mid-read-modify-write.
    """
    issue_a = make_issue("a", identifier="ENG-A", priority=1)
    issue_b = make_issue("b", identifier="ENG-B", priority=2)
    await dispatch(h, issue_a, issue_b)
    assert set(h.state.running) == {"a", "b"}

    h.tracker.by_id = {"a": issue_a, "b": issue_b}
    h.tracker.by_state = []
    h.tracker.id_gate = asyncio.Event()
    h.tracker.id_gate_seen = asyncio.Event()

    observed: dict[str, set[str]] = {}
    original = h.tracker.fetch_issues_by_ids

    async def watching_fetch(issue_ids):
        result = await original(issue_ids)
        observed["after_gate"] = set(h.state.running)
        return result

    h.tracker.fetch_issues_by_ids = watching_fetch

    tick = asyncio.create_task(h.orch.tick())
    await h.tracker.id_gate_seen.wait()

    worker = h.state.running["a"].worker_handle
    h.runner.finish("a")
    await asyncio.gather(worker, return_exceptions=True)
    await asyncio.sleep(0)

    # The worker has fully exited, but the orchestrator has not processed it.
    assert "a" in h.state.running
    assert h.state.retry_attempts == {}

    h.tracker.id_gate.set()
    await tick

    assert observed["after_gate"] == {"a", "b"}

    await settle(h)
    assert "a" not in h.state.running
    assert list(h.state.retry_attempts) == ["a"]
    assert h.state.retry_attempts["a"].error is None


async def test_injected_reconciler_replaces_the_builtin_and_drives_public_mutators():
    """The seam for ``symphony.orchestrator.reconcile`` (no CONTRACTS signature)."""
    harness = build_harness()
    calls: list[str] = []

    async def reconcile(orch: Orchestrator) -> None:
        calls.append("reconcile")
        if "a" in orch.state.running:
            await orch.terminate_running_issue(
                "a", cleanup_workspace=True, reason="external reconciler"
            )

    harness.orch.reconciler = reconcile
    await harness.orch.start(initial_tick=False)
    try:
        await dispatch(harness, make_issue("a", identifier="ENG-A"))
        assert calls == ["reconcile"]
        assert "a" in harness.state.running

        harness.tracker.by_state = []
        await harness.orch.tick()

        assert calls == ["reconcile", "reconcile"]
        assert harness.state.running == {}
        assert harness.workspaces.cleaned == ["ENG-A"]
        assert harness.state.claimed == set()
        # The built-in tracker refresh is fully replaced, not merely augmented.
        assert harness.tracker.id_calls == []
    finally:
        await harness.orch.stop()


async def test_stale_exit_does_not_kill_the_worker_that_replaced_it(h: Harness):
    """SPEC 16.6 — a worker exit is matched by task identity, not by issue id.

    Reconciliation terminates the run and releases the claim, the *same* tick
    re-dispatches the issue, and only then does the cancelled worker's exit
    reach the mailbox. Without the identity check that stale exit would delete
    the healthy replacement and schedule a bogus retry.
    """
    candidate = make_issue("a", identifier="ENG-A", state="Todo")
    await dispatch(h, candidate)
    first_worker = h.state.running["a"].worker_handle

    # Terminal in the tracker (so reconcile stops it) but still a live candidate
    # in the same tick's state query (so dispatch picks it straight back up).
    h.tracker.by_id = {"a": make_issue("a", state="Done", identifier="ENG-A")}
    h.tracker.by_state = [candidate]

    await h.orch.tick()

    second_worker = h.state.running["a"].worker_handle
    assert second_worker is not first_worker
    assert h.workspaces.cleaned == ["ENG-A"]
    assert h.runner.started == [("a", None), ("a", None)]

    await asyncio.gather(first_worker, return_exceptions=True)
    await settle(h)

    assert h.state.running["a"].worker_handle is second_worker
    assert not second_worker.done()
    assert h.state.retry_attempts == {}
    assert h.state.claimed == {"a"}


async def test_report_agent_event_defers_mutation_until_drained(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.orch.report_agent_event("a", FakeAgentEvent(event="turn_completed", timestamp=EPOCH))
    assert h.state.running["a"].session.last_codex_event is None
    await h.orch.drain()
    assert h.state.running["a"].session.last_codex_event == "turn_completed"


async def test_simultaneous_worker_exits_each_produce_exactly_one_retry(h: Harness):
    issues = [make_issue(f"i{n}", identifier=f"ENG-{n}", priority=n) for n in (1, 2, 3)]
    await dispatch(h, *issues)
    assert h.state.running_count() == 3

    tasks = [h.state.running[i.id].worker_handle for i in issues]
    h.clock.advance(2_000)
    for issue in issues:
        h.runner.finish(issue.id)
    await asyncio.gather(*tasks, return_exceptions=True)
    await settle(h)

    assert h.state.running == {}
    assert sorted(h.state.retry_attempts) == ["i1", "i2", "i3"]
    assert all(entry.attempt == 1 for entry in h.state.retry_attempts.values())
    assert h.state.completed == {"i1", "i2", "i3"}
    assert h.state.codex_totals.seconds_running == pytest.approx(6.0)


async def test_invoke_runs_inside_the_mailbox_and_returns_a_value(h: Harness):
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    identifiers = await h.orch.invoke(lambda o: sorted(o.state.running))
    assert identifiers == ["a"]

    with pytest.raises(KeyError):
        await h.orch.invoke(lambda o: o.state.running["missing"])


# ==========================================================================
# SPEC 6.2 / 8.1 — reload
# ==========================================================================


async def test_apply_config_updates_effective_poll_interval_and_limits(h: Harness):
    replacement = FakeConfig(poll_interval_ms=1_234, max_concurrent_agents=3)
    await h.orch.apply_config(replacement)

    assert h.orch.config is replacement
    assert h.state.poll_interval_ms == 1_234
    assert h.state.max_concurrent_agents == 3


async def test_stop_cancels_timers_and_workers():
    harness = build_harness()
    await harness.orch.start(initial_tick=False)
    await dispatch(harness, make_issue("a", identifier="ENG-A"))
    await finish_worker(harness, "a")
    assert harness.state.retry_attempts

    await dispatch(harness, make_issue("b", identifier="ENG-B"))
    worker = harness.state.running["b"].worker_handle

    await harness.orch.stop()

    assert harness.state.retry_attempts == {}
    assert harness.clock.pending == []
    assert worker.cancelled()
    assert not harness.orch.started


# --------------------------------------------------------------------------
# Reconciliation ownership (CONTRACTS ownership map, SPEC 8.5 / 16.3)
#
# SPEC 8.5 was briefly implemented twice -- once here and once in
# symphony.orchestrator.reconcile -- which is exactly the kind of duplication
# that drifts silently. These pin the delegation so it cannot come back.
# --------------------------------------------------------------------------


async def test_the_default_reconciler_delegates_to_the_reconcile_module(h: Harness):
    from symphony.orchestrator.core import module_reconciler

    assert h.orch.reconciler is module_reconciler


async def test_an_explicit_none_selects_the_built_in_fallback(h: Harness):
    from symphony.orchestrator.core import Orchestrator

    orch = Orchestrator(
        config=h.config,
        tracker=h.tracker,
        runner=h.orch.runner,
        workspaces=h.workspaces,
        reconciler=None,
        validate=h.orch._validate,
        clock=h.clock,
        logger=h.log,
    )
    assert orch.reconciler is None


async def test_reconciliation_actually_runs_through_the_module(h: Harness):
    """The delegation is load-bearing, not just a wired-up attribute."""
    import symphony.orchestrator.reconcile as reconcile_module

    calls: list[str] = []
    original = reconcile_module.reconcile_running_issues

    async def spy(state, **kwargs):
        calls.append("reconcile_running_issues")
        return await original(state, **kwargs)

    # Dispatch first: it ticks, and that tick would be counted too.
    await dispatch(h, make_issue("a", identifier="ENG-A"))
    h.tracker.by_state = []

    reconcile_module.reconcile_running_issues = spy
    try:
        await h.orch.tick()
    finally:
        reconcile_module.reconcile_running_issues = original

    assert calls == ["reconcile_running_issues"]
