"""Retry queue and backoff arithmetic — SPEC 8.4.

Two delay regimes coexist here and conflating them is the classic error, so
they are named separately and never share a code path:

* **Continuation** (SPEC 7.3 *Worker Exit (normal)*, 16.6) — a *clean* worker
  exit schedules attempt ``1`` after a fixed :data:`CONTINUATION_DELAY_MS`.
  This is not a failure path; it is how the orchestrator re-checks whether a
  still-active issue needs another worker session.
* **Failure** (SPEC 7.3 *Worker Exit (abnormal)*, 8.4) — an abnormal exit,
  timeout, stall, or slot exhaustion schedules
  ``min(10000 * 2^(attempt - 1), agent.max_retry_backoff_ms)``.

:class:`RetryQueue` owns timer creation, cancellation, and replacement. SPEC
8.4 makes replacement a correctness requirement rather than hygiene: creating a
retry entry MUST cancel any existing timer for the same issue, because a leaked
timer re-dispatches an issue that is already claimed — the exact double
dispatch the claim mechanism (SPEC 7.4) exists to prevent.

The clock and the timer factory are injected. ``RetryEntry.due_at_ms`` is a
*monotonic* reading (SPEC 4.1.7) precisely so it can be driven synthetically in
tests without wall-clock sleeps.

Boundaries this module deliberately does not cross:

* It never touches ``state.claimed``. SPEC 16.4/16.6 place claim acquisition and
  release in the dispatch and retry-timer handlers, not in ``schedule_retry``.
* It never fetches from the tracker. The SPEC 8.4 *retry handling behavior*
  list (refresh, release, re-dispatch, requeue) belongs to the orchestrator
  core; this queue only delivers the "timer fired" edge to it.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Iterator
from functools import partial
from typing import Any, Protocol, runtime_checkable

from symphony.models import OrchestratorState, RetryEntry

__all__ = [
    "BASE_RETRY_DELAY_MS",
    "CONTINUATION_DELAY_MS",
    "DEFAULT_MAX_RETRY_BACKOFF_MS",
    "DELAY_TYPE_CONTINUATION",
    "DELAY_TYPE_FAILURE",
    "MonotonicClockMs",
    "RetryDueCallback",
    "RetryQueue",
    "TimerFactory",
    "TimerHandle",
    "asyncio_timer_factory",
    "backoff_delay_ms",
    "monotonic_ms",
    "next_attempt",
]

#: Fixed delay for a continuation retry after a clean worker exit (SPEC 8.4).
CONTINUATION_DELAY_MS = 1000

#: Base of the failure backoff series; attempt 1 waits exactly this (SPEC 8.4).
BASE_RETRY_DELAY_MS = 10_000

#: Spec default for ``agent.max_retry_backoff_ms`` — 5 minutes (SPEC 8.4, 6.4).
DEFAULT_MAX_RETRY_BACKOFF_MS = 300_000

#: ``delay_type`` option value used by the SPEC 16.6 ``schedule_retry`` call.
DELAY_TYPE_CONTINUATION = "continuation"
DELAY_TYPE_FAILURE = "failure"


# --------------------------------------------------------------------------
# SPEC 8.4 — backoff arithmetic
# --------------------------------------------------------------------------


def backoff_delay_ms(attempt: int, max_backoff_ms: int) -> int:
    """Failure-retry delay ``min(10000 * 2^(attempt - 1), max_backoff_ms)`` (SPEC 8.4).

    ``attempt`` is 1-based (SPEC 4.1.7), so attempt 1 yields
    :data:`BASE_RETRY_DELAY_MS` — not a doubling of it.
    """
    shift = max(attempt, 1) - 1
    # The exponent is clamped *before* shifting. ``attempt`` is unbounded, and
    # ``BASE << (attempt - 1)`` would allocate an arbitrarily large integer long
    # before ``min`` could discard it. Doubling past this ceiling cannot change
    # the result because the shifted value already exceeds the cap.
    ceiling_shift = (max(max_backoff_ms, 0) // BASE_RETRY_DELAY_MS).bit_length()
    return max(0, min(BASE_RETRY_DELAY_MS << min(shift, ceiling_shift), max_backoff_ms))


def next_attempt(attempt: int | None) -> int:
    """Advance a 1-based retry counter (SPEC 4.1.7, 16.4 ``next_attempt``).

    A first run carries no attempt at all (SPEC 12.3), so its first *failure*
    enters the retry queue at attempt ``1`` and waits :data:`BASE_RETRY_DELAY_MS`.
    """
    if attempt is None or attempt < 1:
        return 1
    return attempt + 1


# --------------------------------------------------------------------------
# Injected clock / timer seams
# --------------------------------------------------------------------------


@runtime_checkable
class TimerHandle(Protocol):
    """Runtime-specific timer reference stored on ``RetryEntry.timer_handle``."""

    def cancel(self) -> None: ...


#: ``(delay_ms, callback) -> handle``. Delay is milliseconds, matching the rest
#: of the spec's time units; the default factory converts for asyncio.
TimerFactory = Callable[[float, Callable[[], None]], TimerHandle]

#: Monotonic milliseconds, the clock ``due_at_ms`` is expressed in (SPEC 4.1.7).
MonotonicClockMs = Callable[[], float]

#: Called with ``issue_id`` when a retry comes due. May be sync or async; per
#: SPEC 16.6 it is responsible for popping the entry.
RetryDueCallback = Callable[[str], Awaitable[None] | None]


def monotonic_ms() -> float:
    """Default clock: monotonic milliseconds (SPEC 4.1.7)."""
    return time.monotonic() * 1000.0


def asyncio_timer_factory(delay_ms: float, callback: Callable[[], None]) -> TimerHandle:
    """Default timer: one ``loop.call_later`` per retry entry.

    Requires a running event loop; the orchestrator schedules retries from
    inside its poll loop (SPEC 8.1).
    """
    loop = asyncio.get_running_loop()
    return loop.call_later(max(delay_ms, 0.0) / 1000.0, callback)


# --------------------------------------------------------------------------
# SPEC 8.4 — retry entry creation, replacement, cancellation
# --------------------------------------------------------------------------


class RetryQueue:
    """Owns the ``state.retry_attempts`` map and its timers (SPEC 8.4).

    Entries are written straight into :class:`~symphony.models.OrchestratorState`
    so that ``state.claim_state()`` reports ``RETRY_QUEUED`` (SPEC 7.1) without
    a second source of truth.

    ``max_backoff_ms`` is a plain public attribute so a workflow reload
    (SPEC 6.2) can re-point it without rebuilding the queue.
    """

    def __init__(
        self,
        state: OrchestratorState,
        *,
        on_due: RetryDueCallback | None = None,
        max_backoff_ms: int = DEFAULT_MAX_RETRY_BACKOFF_MS,
        clock: MonotonicClockMs = monotonic_ms,
        timer_factory: TimerFactory = asyncio_timer_factory,
        logger: Any = None,
    ) -> None:
        self.state = state
        self.max_backoff_ms = max_backoff_ms
        self._on_due = on_due
        self._clock = clock
        self._timer_factory = timer_factory
        self._logger = logger

        # Globally unique, never-reused generation per armed timer. A timer
        # already queued on the event loop when it is cancelled still runs its
        # callback; the generation check is what makes that firing inert.
        self._sequence = 0
        self._generations: dict[str, int] = {}
        self._pending: set[asyncio.Future[Any]] = set()

        self.scheduled_count = 0
        self.replaced_count = 0
        self.cancelled_count = 0
        self.fired_count = 0
        self.dropped_stale_count = 0
        self.handler_error_count = 0

    # -- inspection -------------------------------------------------------

    @property
    def entries(self) -> dict[str, RetryEntry]:
        """Live view of ``state.retry_attempts`` (SPEC 4.1.8)."""
        return self.state.retry_attempts

    def get(self, issue_id: str) -> RetryEntry | None:
        return self.state.retry_attempts.get(issue_id)

    def __contains__(self, issue_id: object) -> bool:
        return issue_id in self.state.retry_attempts

    def __len__(self) -> int:
        return len(self.state.retry_attempts)

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(self.state.retry_attempts))

    def stats(self) -> dict[str, int]:
        """Counters that make timer replacement observable (SPEC 13.3)."""
        return {
            "pending": len(self.state.retry_attempts),
            "scheduled": self.scheduled_count,
            "replaced": self.replaced_count,
            "cancelled": self.cancelled_count,
            "fired": self.fired_count,
            "dropped_stale": self.dropped_stale_count,
            "handler_errors": self.handler_error_count,
        }

    def __repr__(self) -> str:
        return (
            f"RetryQueue(pending={len(self.state.retry_attempts)}, "
            f"max_backoff_ms={self.max_backoff_ms}, "
            f"scheduled={self.scheduled_count}, replaced={self.replaced_count}, "
            f"cancelled={self.cancelled_count}, fired={self.fired_count})"
        )

    # -- delay selection --------------------------------------------------

    def delay_for(self, attempt: int, *, delay_type: str = DELAY_TYPE_FAILURE) -> int:
        """Resolve the delay for one retry regime (SPEC 8.4)."""
        if delay_type == DELAY_TYPE_CONTINUATION:
            return CONTINUATION_DELAY_MS
        if delay_type == DELAY_TYPE_FAILURE:
            return backoff_delay_ms(attempt, self.max_backoff_ms)
        raise ValueError(
            f"unknown retry delay_type {delay_type!r}; "
            f"expected {DELAY_TYPE_CONTINUATION!r} or {DELAY_TYPE_FAILURE!r}"
        )

    # -- scheduling -------------------------------------------------------

    def schedule(
        self,
        issue_id: str,
        *,
        attempt: int,
        identifier: str | None = None,
        error: str | None = None,
        delay_type: str = DELAY_TYPE_FAILURE,
    ) -> RetryEntry:
        """Create a retry entry, replacing any existing one (SPEC 8.4, 16.6).

        Mirrors the SPEC 16.6 ``schedule_retry(state, issue_id, attempt, opts)``
        call, including its ``delay_type`` option.
        """
        delay_ms = self.delay_for(attempt, delay_type=delay_type)

        # SPEC 8.4: "Cancel any existing retry timer for the same issue."
        if self._cancel(issue_id):
            self.replaced_count += 1
            self._log(
                "info",
                "retry timer replaced",
                issue_id=issue_id,
                issue_identifier=identifier,
            )

        self._sequence += 1
        generation = self._sequence
        entry = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=self._clock() + delay_ms,
            timer_handle=None,
            error=error,
        )
        self.state.retry_attempts[issue_id] = entry
        self._generations[issue_id] = generation
        try:
            entry.timer_handle = self._timer_factory(
                float(delay_ms), partial(self._fire, issue_id, generation)
            )
        except BaseException:
            # Never leave an entry that no timer will ever wake: it would read
            # as RETRY_QUEUED forever and permanently block the issue.
            self.state.retry_attempts.pop(issue_id, None)
            self._generations.pop(issue_id, None)
            raise

        self.scheduled_count += 1
        self._log(
            "info",
            "retry scheduled",
            issue_id=issue_id,
            issue_identifier=identifier,
            attempt=attempt,
            delay_ms=delay_ms,
            delay_type=delay_type,
            error=error,
        )
        return entry

    def schedule_continuation(self, issue_id: str, *, identifier: str | None = None) -> RetryEntry:
        """Clean worker exit: attempt 1 after the fixed continuation delay (SPEC 16.6).

        This is not a failure path, so the entry carries no ``error``.
        """
        return self.schedule(
            issue_id,
            attempt=1,
            identifier=identifier,
            delay_type=DELAY_TYPE_CONTINUATION,
        )

    def schedule_failure(
        self,
        issue_id: str,
        *,
        attempt: int,
        identifier: str | None = None,
        error: str | None = None,
    ) -> RetryEntry:
        """Abnormal exit, stall, or slot exhaustion: exponential backoff (SPEC 8.4)."""
        return self.schedule(
            issue_id,
            attempt=attempt,
            identifier=identifier,
            error=error,
            delay_type=DELAY_TYPE_FAILURE,
        )

    # -- cancellation -----------------------------------------------------

    def cancel(self, issue_id: str) -> bool:
        """Cancel and drop a pending retry; ``True`` if one existed (SPEC 16.4)."""
        if self._cancel(issue_id):
            self.cancelled_count += 1
            self._log("info", "retry cancelled", issue_id=issue_id)
            return True
        return False

    def cancel_all(self) -> int:
        """Cancel every pending retry; returns how many were dropped."""
        count = 0
        for issue_id in tuple(self.state.retry_attempts):
            if self.cancel(issue_id):
                count += 1
        return count

    def pop(self, issue_id: str) -> RetryEntry | None:
        """Remove and return a retry entry (SPEC 16.6 ``retry_attempts.pop``).

        Also cancels the timer, which is a no-op for the already-fired handle
        the retry handler itself is running under.
        """
        entry = self.state.retry_attempts.pop(issue_id, None)
        self._generations.pop(issue_id, None)
        if entry is not None:
            _cancel_handle(entry.timer_handle)
        return entry

    def _cancel(self, issue_id: str) -> bool:
        """Cancel without touching counters, so callers can classify the reason."""
        entry = self.state.retry_attempts.pop(issue_id, None)
        # Dropping the generation is what makes a callback that is already
        # queued on the event loop inert when it eventually runs.
        self._generations.pop(issue_id, None)
        if entry is None:
            return False
        _cancel_handle(entry.timer_handle)
        return True

    # -- firing -----------------------------------------------------------

    def _fire(self, issue_id: str, generation: int) -> None:
        """Timer callback: deliver the SPEC 7.3 ``Retry Timer Fired`` edge."""
        if self._generations.get(issue_id) != generation:
            # Superseded or cancelled between arming and running.
            self.dropped_stale_count += 1
            return
        entry = self.state.retry_attempts.get(issue_id)
        if entry is None:
            self.dropped_stale_count += 1
            return

        self.fired_count += 1
        if self._on_due is None:
            return
        try:
            # Per SPEC 16.6 the handler pops the entry; leaving it in place
            # keeps the issue RETRY_QUEUED until the handler decides.
            result = self._on_due(issue_id)
        except Exception as exc:
            # A bad handler must not tear down the orchestrator's event loop.
            self.handler_error_count += 1
            self._log("error", "retry handler raised", issue_id=issue_id, error=str(exc))
            return
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            self._pending.add(task)
            task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Future[Any]) -> None:
        self._pending.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.handler_error_count += 1
            self._log("error", "retry handler raised", error=str(exc))

    # -- logging seam -----------------------------------------------------

    def _log(self, level: str, message: str, **fields: Any) -> None:
        """Log through an injected ``StructuredLogger`` if the orchestrator gave us one.

        The logger is injected rather than imported so this module stays usable
        (and testable) without ``symphony.observability.logging``.
        """
        if self._logger is None:
            return
        emit = getattr(self._logger, level, None)
        if emit is None:
            return
        emit(message, **{k: v for k, v in fields.items() if v is not None})


def _cancel_handle(handle: Any) -> None:
    """Cancel a timer handle defensively; a missing handle is not an error."""
    if handle is None:
        return
    cancel = getattr(handle, "cancel", None)
    if cancel is not None:
        cancel()
