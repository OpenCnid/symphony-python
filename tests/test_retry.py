"""Tests for SPEC 8.4 retry/backoff — `symphony.orchestrator.retry`.

Everything is driven by an injected clock and an injected timer factory, so no
test sleeps and no test depends on a sibling module's real implementation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from symphony.models import ClaimState, OrchestratorState
from symphony.orchestrator.retry import (
    BASE_RETRY_DELAY_MS,
    CONTINUATION_DELAY_MS,
    DEFAULT_MAX_RETRY_BACKOFF_MS,
    DELAY_TYPE_CONTINUATION,
    DELAY_TYPE_FAILURE,
    RetryQueue,
    asyncio_timer_factory,
    backoff_delay_ms,
    monotonic_ms,
    next_attempt,
)

# --------------------------------------------------------------------------
# Deterministic seams
# --------------------------------------------------------------------------


class FakeClock:
    """Injected monotonic-milliseconds clock (SPEC 4.1.7 `due_at_ms`)."""

    def __init__(self, now_ms: float = 1_000_000.0) -> None:
        self.now_ms = now_ms

    def __call__(self) -> float:
        return self.now_ms

    def advance(self, delta_ms: float) -> None:
        self.now_ms += delta_ms


class FakeTimer:
    def __init__(self, factory: FakeTimers, delay_ms: float, callback: Any) -> None:
        self.factory = factory
        self.delay_ms = delay_ms
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        self.factory.cancelled.append(self)

    def fire(self) -> None:
        """Run the callback exactly as a real event loop would."""
        self.callback()


class FakeTimers:
    """Timer factory that records every timer it creates and cancels."""

    def __init__(self) -> None:
        self.created: list[FakeTimer] = []
        self.cancelled: list[FakeTimer] = []
        self.fail_next = False

    def __call__(self, delay_ms: float, callback: Any) -> FakeTimer:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("no running event loop")
        timer = FakeTimer(self, delay_ms, callback)
        self.created.append(timer)
        return timer

    @property
    def live(self) -> list[FakeTimer]:
        return [t for t in self.created if not t.cancelled]


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, message: str, **fields: Any) -> None:
        self.records.append(("info", message, fields))

    def error(self, message: str, **fields: Any) -> None:
        self.records.append(("error", message, fields))

    def messages(self, level: str | None = None) -> list[str]:
        return [m for lvl, m, _ in self.records if level is None or lvl == level]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def timers() -> FakeTimers:
    return FakeTimers()


@pytest.fixture
def state() -> OrchestratorState:
    return OrchestratorState()


def make_queue(
    state: OrchestratorState,
    clock: FakeClock,
    timers: FakeTimers,
    *,
    on_due: Any = None,
    max_backoff_ms: int = DEFAULT_MAX_RETRY_BACKOFF_MS,
    logger: Any = None,
) -> RetryQueue:
    return RetryQueue(
        state,
        on_due=on_due,
        max_backoff_ms=max_backoff_ms,
        clock=clock,
        timer_factory=timers,
        logger=logger,
    )


# --------------------------------------------------------------------------
# SPEC 8.4 — backoff arithmetic
# --------------------------------------------------------------------------


def test_continuation_delay_is_the_spec_constant() -> None:
    """SPEC 8.4: clean-exit continuation retries use a fixed 1000 ms."""
    assert CONTINUATION_DELAY_MS == 1000


def test_backoff_base_case_attempt_one_is_10000_not_20000() -> None:
    """SPEC 8.4: `10000 * 2^(attempt - 1)` with a 1-based attempt (SPEC 4.1.7)."""
    assert backoff_delay_ms(1, DEFAULT_MAX_RETRY_BACKOFF_MS) == 10_000


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        (1, 10_000),
        (2, 20_000),
        (3, 40_000),
        (4, 80_000),
        (5, 160_000),
        (6, 300_000),  # 320_000 clamped by the default cap
        (7, 300_000),
        (12, 300_000),
    ],
)
def test_backoff_series_with_default_cap(attempt: int, expected: int) -> None:
    assert backoff_delay_ms(attempt, DEFAULT_MAX_RETRY_BACKOFF_MS) == expected


def test_backoff_doubles_until_the_cap_engages() -> None:
    """Every uncapped step is exactly twice the previous one."""
    series = [backoff_delay_ms(a, DEFAULT_MAX_RETRY_BACKOFF_MS) for a in range(1, 6)]
    assert series == [10_000, 20_000, 40_000, 80_000, 160_000]
    assert all(series[i] == 2 * series[i - 1] for i in range(1, len(series)))


def test_backoff_cap_uses_configured_max_not_the_default() -> None:
    """SPEC 17.4: "Retry backoff cap uses configured `agent.max_retry_backoff_ms`"."""
    assert backoff_delay_ms(3, 45_000) == 40_000  # below the cap, untouched
    assert backoff_delay_ms(4, 45_000) == 45_000  # 80_000 clamped
    assert backoff_delay_ms(9, 45_000) == 45_000


def test_backoff_cap_below_base_delay_wins() -> None:
    """A cap tighter than the 10 s base clamps even attempt 1."""
    assert backoff_delay_ms(1, 5_000) == 5_000
    assert backoff_delay_ms(6, 5_000) == 5_000


def test_backoff_large_attempt_returns_cap_without_overflow() -> None:
    """A huge attempt must not build a giant integer before `min` applies."""
    assert backoff_delay_ms(10**9, DEFAULT_MAX_RETRY_BACKOFF_MS) == 300_000
    assert backoff_delay_ms(2**31, 45_000) == 45_000


def test_backoff_never_returns_a_negative_delay() -> None:
    """A non-positive cap must not produce a nonsensical timer delay."""
    assert backoff_delay_ms(1, 0) == 0
    assert backoff_delay_ms(5, -1) == 0


def test_backoff_attempt_below_one_clamps_to_the_base_delay() -> None:
    """A 0 or negative attempt must not yield a fractional half-base delay."""
    assert backoff_delay_ms(0, DEFAULT_MAX_RETRY_BACKOFF_MS) == BASE_RETRY_DELAY_MS
    assert backoff_delay_ms(-3, DEFAULT_MAX_RETRY_BACKOFF_MS) == BASE_RETRY_DELAY_MS


def test_next_attempt_is_one_based_from_a_first_run() -> None:
    """SPEC 12.3: first run carries no attempt; its first retry is attempt 1."""
    assert next_attempt(None) == 1
    assert next_attempt(1) == 2
    assert next_attempt(7) == 8


# --------------------------------------------------------------------------
# SPEC 8.4 / 16.6 — the two delay regimes must not be conflated
# --------------------------------------------------------------------------


def test_continuation_is_not_the_failure_delay_for_the_same_attempt(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """Both regimes use attempt 1; only the failure regime waits 10 s."""
    q = make_queue(state, clock, timers)
    assert q.delay_for(1, delay_type=DELAY_TYPE_CONTINUATION) == 1_000
    assert q.delay_for(1, delay_type=DELAY_TYPE_FAILURE) == 10_000


def test_continuation_delay_ignores_the_attempt_number(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    assert q.delay_for(9, delay_type=DELAY_TYPE_CONTINUATION) == CONTINUATION_DELAY_MS


def test_unknown_delay_type_is_rejected(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    with pytest.raises(ValueError, match="unknown retry delay_type"):
        q.delay_for(1, delay_type="exponential-ish")


def test_max_backoff_reload_affects_later_schedules(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """SPEC 6.2: a workflow reload re-points the cap without rebuilding the queue."""
    q = make_queue(state, clock, timers)
    assert q.delay_for(5) == 160_000
    q.max_backoff_ms = 20_000
    assert q.delay_for(5) == 20_000


# --------------------------------------------------------------------------
# SPEC 8.4 — retry entry creation
# --------------------------------------------------------------------------


def test_schedule_failure_stores_every_required_field(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """SPEC 17.4: "Retry queue entries include attempt, due time, identifier, and error"."""
    q = make_queue(state, clock, timers)
    entry = q.schedule_failure("ISS-1", attempt=3, identifier="ENG-11", error="worker exited: 1")

    assert entry.issue_id == "ISS-1"
    assert entry.identifier == "ENG-11"
    assert entry.attempt == 3
    assert entry.error == "worker exited: 1"
    assert entry.due_at_ms == clock.now_ms + 40_000
    assert entry.timer_handle is timers.created[-1]
    assert state.retry_attempts["ISS-1"] is entry


def test_due_at_ms_is_read_from_the_injected_monotonic_clock(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    clock.advance(5_000)
    entry = q.schedule_failure("ISS-1", attempt=1)
    assert entry.due_at_ms == 1_005_000 + 10_000


def test_scheduled_issue_reports_retry_queued_claim_state(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """SPEC 7.1: a retry-queued issue is not dispatchable by the poll tick."""
    q = make_queue(state, clock, timers)
    assert state.claim_state("ISS-1") is ClaimState.UNCLAIMED
    q.schedule_failure("ISS-1", attempt=1)
    assert state.claim_state("ISS-1") is ClaimState.RETRY_QUEUED


def test_schedule_continuation_uses_attempt_one_and_no_error(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """SPEC 16.6: a clean exit schedules attempt 1 with `delay_type: continuation`."""
    q = make_queue(state, clock, timers)
    entry = q.schedule_continuation("ISS-1", identifier="ENG-11")

    assert entry.attempt == 1
    assert entry.error is None
    assert entry.due_at_ms == clock.now_ms + CONTINUATION_DELAY_MS
    assert timers.created[-1].delay_ms == pytest.approx(1_000.0)


def test_schedule_passes_the_computed_delay_to_the_timer_factory(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    q.schedule_failure("ISS-1", attempt=4)
    assert timers.created[-1].delay_ms == pytest.approx(80_000.0)


def test_schedule_dispatcher_matches_the_named_regimes(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """The SPEC 16.6 `delay_type` option and the named helpers agree."""
    q = make_queue(state, clock, timers)
    a = q.schedule("A", attempt=1, delay_type=DELAY_TYPE_CONTINUATION)
    b = q.schedule("B", attempt=1, delay_type=DELAY_TYPE_FAILURE)
    assert a.due_at_ms - clock.now_ms == CONTINUATION_DELAY_MS
    assert b.due_at_ms - clock.now_ms == BASE_RETRY_DELAY_MS


def test_schedule_does_not_touch_the_claimed_set(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """SPEC 16.4/16.6 place claim acquisition and release outside `schedule_retry`."""
    q = make_queue(state, clock, timers)
    q.schedule_failure("ISS-1", attempt=1)
    assert state.claimed == set()
    q.pop("ISS-1")
    assert state.claimed == set()


def test_timer_factory_failure_leaves_no_orphan_entry(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """An entry with no timer would read as RETRY_QUEUED forever."""
    q = make_queue(state, clock, timers)
    timers.fail_next = True
    with pytest.raises(RuntimeError):
        q.schedule_failure("ISS-1", attempt=1)
    assert "ISS-1" not in state.retry_attempts
    assert state.claim_state("ISS-1") is ClaimState.UNCLAIMED


# --------------------------------------------------------------------------
# SPEC 8.4 — "Cancel any existing retry timer for the same issue"
# --------------------------------------------------------------------------


def test_rescheduling_cancels_the_previous_timer(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    q.schedule_failure("ISS-1", attempt=1, error="first")
    first = timers.created[0]

    q.schedule_failure("ISS-1", attempt=2, error="second")
    second = timers.created[1]

    assert first.cancelled is True
    assert second.cancelled is False
    assert timers.cancelled == [first]
    assert timers.live == [second]
    assert q.replaced_count == 1


def test_rescheduling_leaves_exactly_one_entry_holding_the_latest_values(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    q.schedule_failure("ISS-1", attempt=1, identifier="ENG-11", error="first")
    entry = q.schedule_failure("ISS-1", attempt=2, identifier="ENG-11", error="second")

    assert list(state.retry_attempts) == ["ISS-1"]
    assert state.retry_attempts["ISS-1"] is entry
    assert entry.attempt == 2
    assert entry.error == "second"
    assert entry.timer_handle is timers.created[1]


def test_a_superseded_timer_that_still_fires_is_inert(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """The double-dispatch guard: a cancelled-but-already-queued callback must no-op.

    Cancelling an event-loop timer does not unwind a callback the loop has
    already picked up, so replacement is only correct if the stale firing is
    also rejected (SPEC 7.4).
    """
    fired: list[str] = []
    q = make_queue(state, clock, timers, on_due=fired.append)

    q.schedule_failure("ISS-1", attempt=1)
    stale = timers.created[0]
    q.schedule_failure("ISS-1", attempt=2)

    stale.fire()

    assert fired == []
    assert q.fired_count == 0
    assert q.dropped_stale_count == 1
    assert state.claim_state("ISS-1") is ClaimState.RETRY_QUEUED


def test_the_replacement_timer_still_fires_normally(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    fired: list[str] = []
    q = make_queue(state, clock, timers, on_due=fired.append)

    q.schedule_failure("ISS-1", attempt=1)
    q.schedule_failure("ISS-1", attempt=2)
    timers.created[1].fire()

    assert fired == ["ISS-1"]
    assert q.fired_count == 1


def test_repeated_replacement_leaks_no_live_timers(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    for attempt in range(1, 6):
        q.schedule_failure("ISS-1", attempt=attempt)

    assert len(timers.created) == 5
    assert len(timers.live) == 1
    assert timers.live[0] is timers.created[-1]
    assert q.replaced_count == 4


def test_replacement_is_scoped_to_one_issue(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    q.schedule_failure("ISS-1", attempt=1)
    q.schedule_failure("ISS-2", attempt=1)
    q.schedule_failure("ISS-1", attempt=2)

    assert timers.cancelled == [timers.created[0]]
    assert set(state.retry_attempts) == {"ISS-1", "ISS-2"}


def test_replacement_is_logged_when_a_logger_is_injected(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    logger = RecordingLogger()
    q = make_queue(state, clock, timers, logger=logger)
    q.schedule_failure("ISS-1", attempt=1)
    q.schedule_failure("ISS-1", attempt=2)

    assert "retry timer replaced" in logger.messages("info")


# --------------------------------------------------------------------------
# SPEC 16.4 / 16.6 — cancellation and removal
# --------------------------------------------------------------------------


def test_cancel_drops_the_entry_and_the_timer(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """SPEC 16.4: dispatch removes the issue from `retry_attempts`."""
    q = make_queue(state, clock, timers)
    q.schedule_failure("ISS-1", attempt=1)

    assert q.cancel("ISS-1") is True
    assert timers.created[0].cancelled is True
    assert "ISS-1" not in state.retry_attempts
    assert state.claim_state("ISS-1") is ClaimState.UNCLAIMED
    assert q.cancelled_count == 1


def test_cancel_of_an_unknown_issue_is_false(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    assert q.cancel("nope") is False
    assert q.cancelled_count == 0


def test_a_cancelled_timer_that_still_fires_is_inert(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    fired: list[str] = []
    q = make_queue(state, clock, timers, on_due=fired.append)
    q.schedule_failure("ISS-1", attempt=1)
    q.cancel("ISS-1")

    timers.created[0].fire()

    assert fired == []
    assert q.dropped_stale_count == 1


def test_cancel_all_clears_every_pending_timer(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    for issue_id in ("A", "B", "C"):
        q.schedule_failure(issue_id, attempt=1)

    assert q.cancel_all() == 3
    assert state.retry_attempts == {}
    assert all(t.cancelled for t in timers.created)
    assert q.cancel_all() == 0


def test_pop_returns_the_entry_and_cancels_its_timer(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """SPEC 16.6: the retry handler pops the entry first."""
    q = make_queue(state, clock, timers)
    scheduled = q.schedule_failure("ISS-1", attempt=2, identifier="ENG-11")

    popped = q.pop("ISS-1")

    assert popped is scheduled
    assert popped.attempt == 2
    assert "ISS-1" not in state.retry_attempts
    assert timers.created[0].cancelled is True
    assert q.pop("ISS-1") is None


def test_a_pop_makes_a_later_firing_of_the_same_timer_inert(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    fired: list[str] = []
    q = make_queue(state, clock, timers, on_due=fired.append)
    q.schedule_failure("ISS-1", attempt=1)
    q.pop("ISS-1")

    timers.created[0].fire()

    assert fired == []
    assert q.dropped_stale_count == 1


# --------------------------------------------------------------------------
# SPEC 7.3 — "Retry Timer Fired"
# --------------------------------------------------------------------------


def test_firing_invokes_the_callback_with_the_issue_id(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    seen: list[str] = []
    q = make_queue(state, clock, timers, on_due=seen.append)
    q.schedule_failure("ISS-1", attempt=1)

    timers.created[0].fire()

    assert seen == ["ISS-1"]
    assert q.fired_count == 1


def test_firing_leaves_the_entry_for_the_handler_to_pop(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    """SPEC 16.6 pops inside `on_retry_timer`; the queue must not pop first."""
    observed: list[Any] = []

    def on_due(issue_id: str) -> None:
        observed.append(state.retry_attempts.get(issue_id))

    q = make_queue(state, clock, timers, on_due=on_due)
    entry = q.schedule_failure("ISS-1", attempt=1)

    timers.created[0].fire()

    assert observed == [entry]


def test_firing_without_a_callback_is_a_no_op(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    q.schedule_failure("ISS-1", attempt=1)
    timers.created[0].fire()
    assert q.fired_count == 1


def test_a_raising_handler_does_not_propagate_into_the_timer_callback(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    logger = RecordingLogger()

    def boom(issue_id: str) -> None:
        raise RuntimeError("tracker refresh exploded")

    q = make_queue(state, clock, timers, on_due=boom, logger=logger)
    q.schedule_failure("ISS-1", attempt=1)

    timers.created[0].fire()  # must not raise

    assert q.handler_error_count == 1
    assert "retry handler raised" in logger.messages("error")


async def test_an_async_handler_is_scheduled_on_the_running_loop(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    seen: list[str] = []

    async def on_due(issue_id: str) -> None:
        seen.append(issue_id)

    q = make_queue(state, clock, timers, on_due=on_due)
    q.schedule_failure("ISS-1", attempt=1)

    timers.created[0].fire()
    assert seen == []  # not yet: the coroutine was handed to the loop
    await asyncio.sleep(0)

    assert seen == ["ISS-1"]
    assert q.handler_error_count == 0


async def test_a_raising_async_handler_is_recorded_not_lost(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    async def on_due(issue_id: str) -> None:
        raise RuntimeError("tracker refresh exploded")

    q = make_queue(state, clock, timers, on_due=on_due)
    q.schedule_failure("ISS-1", attempt=1)

    timers.created[0].fire()
    await asyncio.sleep(0)  # runs the coroutine to its exception
    await asyncio.sleep(0)  # runs the done-callback `call_soon` scheduled by it

    assert q.handler_error_count == 1


# --------------------------------------------------------------------------
# Default seams
# --------------------------------------------------------------------------


def test_monotonic_ms_is_monotonic_milliseconds() -> None:
    first = monotonic_ms()
    second = monotonic_ms()
    assert second >= first
    assert isinstance(first, float)


async def test_default_timer_factory_arms_a_loop_timer_at_the_right_time() -> None:
    """Structural check on the real asyncio seam; no sleeping."""
    loop = asyncio.get_running_loop()
    handle = asyncio_timer_factory(2_500.0, lambda: None)
    try:
        assert isinstance(handle, asyncio.TimerHandle)
        assert handle.when() == pytest.approx(loop.time() + 2.5, abs=0.2)
    finally:
        handle.cancel()


def test_default_timer_factory_requires_a_running_loop() -> None:
    with pytest.raises(RuntimeError):
        asyncio_timer_factory(1_000.0, lambda: None)


async def test_queue_defaults_use_the_real_asyncio_timer(state: OrchestratorState) -> None:
    """The default construction must work unmodified inside the poll loop."""
    q = RetryQueue(state)
    entry = q.schedule_continuation("ISS-1", identifier="ENG-11")
    try:
        assert isinstance(entry.timer_handle, asyncio.TimerHandle)
        assert q.max_backoff_ms == DEFAULT_MAX_RETRY_BACKOFF_MS
    finally:
        assert q.cancel_all() == 1


# --------------------------------------------------------------------------
# Observability surface
# --------------------------------------------------------------------------


def test_stats_report_the_replacement_and_cancellation_counters(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers, on_due=lambda _issue_id: None)
    q.schedule_failure("ISS-1", attempt=1)
    stale = timers.created[0]
    q.schedule_failure("ISS-1", attempt=2)
    stale.fire()
    timers.created[1].fire()
    q.schedule_failure("ISS-2", attempt=1)
    q.cancel("ISS-2")

    assert q.stats() == {
        "pending": 1,
        "scheduled": 3,
        "replaced": 1,
        "cancelled": 1,
        "fired": 1,
        "dropped_stale": 1,
        "handler_errors": 0,
    }


def test_queue_is_introspectable_as_a_mapping_of_pending_issues(
    state: OrchestratorState, clock: FakeClock, timers: FakeTimers
) -> None:
    q = make_queue(state, clock, timers)
    q.schedule_failure("ISS-1", attempt=1)
    q.schedule_continuation("ISS-2")

    assert len(q) == 2
    assert "ISS-1" in q
    assert "ISS-3" not in q
    assert sorted(q) == ["ISS-1", "ISS-2"]
    assert q.get("ISS-2") is state.retry_attempts["ISS-2"]
    assert q.get("ISS-3") is None
    assert q.entries is state.retry_attempts
    assert "pending=2" in repr(q)
