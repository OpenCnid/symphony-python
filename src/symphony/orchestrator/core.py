"""Orchestrator core — SPEC 7, 8.1, 16.1-16.4, 16.6.

SPEC 7 opens with the invariant that everything else rests on: *the orchestrator
is the only component that mutates scheduling state, and all worker outcomes are
reported back to it and converted into explicit state transitions*.

This module honors that literally. :class:`Orchestrator` owns a single
:class:`asyncio.Queue` mailbox and one task that drains it. Poll ticks, worker
exits, retry-timer firings, agent updates, and config reloads all arrive as
commands on that mailbox; every write to :class:`~symphony.models.OrchestratorState`
happens inside the mailbox task and nowhere else. Worker tasks and timer
callbacks never touch state — they only enqueue. That is what makes duplicate
dispatch and lost-update races structurally impossible rather than merely
unlikely (SPEC 7.4).

The public surface is deliberately flat and named so a Recursive Language Model
driving this system from a REPL can hold a live orchestrator and read its state
without a debugger: ``orch.state``, ``orch.config``, ``orch.deps``, plus
``await orch.tick()``, ``await orch.invoke(fn)``, and ``await orch.drain()``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from symphony.models import (
    Issue,
    LiveSession,
    OrchestratorState,
    RetryEntry,
    RunningEntry,
    RunPhase,
)
from symphony.models import session_id as build_session_id

if TYPE_CHECKING:  # pragma: no cover - siblings are written concurrently
    from symphony.trackers.base import TrackerAdapter
    from symphony.workflow.config import ServiceConfig

__all__ = [
    "AgentEventLike",
    "AgentRunnerLike",
    "AsyncioClock",
    "Clock",
    "Orchestrator",
    "OrchestratorDeps",
    "RECENT_EVENT_LIMIT",
    "TimerHandle",
    "WorkspaceCleaner",
]

T = TypeVar("T")

#: How many humanized agent events are retained per running entry for the
#: SPEC 13.3 snapshot surface. Bounded so a chatty session cannot grow state
#: without limit.
RECENT_EVENT_LIMIT = 20

#: SPEC 7.2 distinguishes terminal reasons because retry logic and logs differ.
#: Error categories (SPEC 10.6) that mean "timed out" rather than "failed".
_TIMEOUT_CATEGORIES = frozenset({"turn_timeout", "response_timeout", "hook_timeout"})


# --------------------------------------------------------------------------
# Injected collaborators (structural protocols — siblings are written elsewhere)
# --------------------------------------------------------------------------


class TimerHandle(Protocol):
    """Anything cancellable, e.g. :class:`asyncio.TimerHandle`."""

    def cancel(self) -> None: ...


class Clock(Protocol):
    """Time and timer source, injected so tests never sleep on the wall clock."""

    def now(self) -> datetime:
        """Current UTC instant, used for ``started_at`` and stall elapsed."""

    def monotonic_ms(self) -> float:
        """Monotonic reading in milliseconds; ``RetryEntry.due_at_ms`` basis."""

    def call_later_ms(self, delay_ms: float, callback: Callable[[], None]) -> TimerHandle:
        """Invoke ``callback`` after ``delay_ms``; the handle MUST be cancellable."""


class AsyncioClock:
    """Default :class:`Clock` bound to the running event loop."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ms(self) -> float:
        return time.monotonic() * 1000.0

    def call_later_ms(self, delay_ms: float, callback: Callable[[], None]) -> TimerHandle:
        loop = asyncio.get_running_loop()
        return loop.call_later(max(float(delay_ms), 0.0) / 1000.0, callback)


class WorkspaceCleaner(Protocol):
    """The one workspace operation the orchestrator needs (CONTRACTS ``WorkspaceManager``)."""

    async def cleanup(self, identifier: str) -> bool: ...


class AgentRunnerLike(Protocol):
    """CONTRACTS ``symphony.agent.runner.AgentRunner`` (SPEC 10.7, 16.5)."""

    async def run_attempt(self, issue: Issue, attempt: int | None) -> None: ...


class AgentEventLike(Protocol):
    """CONTRACTS ``symphony.agent.events.AgentEvent`` (SPEC 10.4)."""

    event: str
    timestamp: datetime
    codex_app_server_pid: str | None
    usage: dict[str, Any] | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OrchestratorDeps:
    """Sibling pure functions the orchestrator calls, per CONTRACTS.md section 3.

    Bundled and injectable so this module can be exercised before
    ``scheduling.py`` / ``retry.py`` / ``agent/events.py`` exist, and so a REPL
    driver can inspect exactly which policy is bound (``orch.deps``).
    """

    sort_for_dispatch: Callable[[Iterable[Issue]], list[Issue]]
    issue_routable: Callable[[Issue, Any], bool]
    should_dispatch: Callable[[Issue, OrchestratorState, Any], bool]
    available_slots: Callable[[OrchestratorState, Any], int]
    has_state_slot: Callable[[Issue, OrchestratorState, Any], bool]
    backoff_delay_ms: Callable[[int, int], int]
    continuation_delay_ms: int
    extract_token_totals: Callable[[dict[str, Any]], tuple[int, int, int] | None]
    extract_rate_limits: Callable[[dict[str, Any]], dict[str, Any] | None]

    @classmethod
    def load(cls) -> OrchestratorDeps:
        """Bind the real sibling implementations named in CONTRACTS.md section 3."""
        scheduling = importlib.import_module("symphony.orchestrator.scheduling")
        retry = importlib.import_module("symphony.orchestrator.retry")
        return cls(
            sort_for_dispatch=scheduling.sort_for_dispatch,
            issue_routable=scheduling.issue_routable,
            should_dispatch=scheduling.should_dispatch,
            available_slots=scheduling.available_slots,
            has_state_slot=scheduling.has_state_slot,
            backoff_delay_ms=retry.backoff_delay_ms,
            continuation_delay_ms=retry.CONTINUATION_DELAY_MS,
            extract_token_totals=_optional_callable(
                "symphony.agent.events", "extract_token_totals", _no_token_totals
            ),
            extract_rate_limits=_optional_callable(
                "symphony.agent.events", "extract_rate_limits", _no_rate_limits
            ),
        )


def _no_token_totals(_payload: dict[str, Any]) -> tuple[int, int, int] | None:
    return None


def _no_rate_limits(_payload: dict[str, Any]) -> dict[str, Any] | None:
    return None


def _optional_callable(module: str, name: str, fallback: Any) -> Any:
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):  # pragma: no cover - sibling not landed yet
        return fallback


# --------------------------------------------------------------------------
# Mailbox commands — the only way state is ever touched
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Tick:
    reschedule: bool = True
    future: asyncio.Future[None] | None = None


@dataclass(slots=True)
class _WorkerExit:
    issue_id: str
    task: Any
    normal: bool
    reason: str
    phase: RunPhase


@dataclass(slots=True)
class _RetryFired:
    issue_id: str


@dataclass(slots=True)
class _AgentUpdate:
    issue_id: str
    event: Any


@dataclass(slots=True)
class _PhaseUpdate:
    issue_id: str
    phase: RunPhase
    error: str | None = None


@dataclass(slots=True)
class _ApplyConfig:
    config: Any
    future: asyncio.Future[None] | None = None


@dataclass(slots=True)
class _Invoke:
    fn: Callable[[Orchestrator], Any]
    future: asyncio.Future[Any]


@dataclass(slots=True)
class _Stop:
    pass


# --------------------------------------------------------------------------
# Fallback logger — SPEC 14.2: log-sink problems MUST NOT crash the orchestrator
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _FallbackLogger:
    """Stand-in used when ``symphony.observability.logging`` is unavailable."""

    name: str
    fields: dict[str, Any] = field(default_factory=dict)

    def bind(self, **fields: Any) -> _FallbackLogger:
        merged = dict(self.fields)
        merged.update(fields)
        return _FallbackLogger(self.name, merged)

    def _emit(self, level: int, msg: str, fields: dict[str, Any]) -> None:
        merged = dict(self.fields)
        merged.update(fields)
        rendered = " ".join(f"{k}={v}" for k, v in merged.items())
        logging.getLogger(self.name).log(level, "%s %s", msg, rendered)

    def debug(self, msg: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, msg, fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._emit(logging.INFO, msg, fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._emit(logging.WARNING, msg, fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._emit(logging.ERROR, msg, fields)


def _default_logger() -> Any:
    try:
        module = importlib.import_module("symphony.observability.logging")
        return module.get_logger("symphony.orchestrator")
    except (ImportError, AttributeError):  # pragma: no cover - sibling not landed yet
        return _FallbackLogger("symphony.orchestrator")


def _lazy_validate(config: Any) -> None:
    """Default dispatch preflight — CONTRACTS ``validate_dispatch_config`` (SPEC 6.3)."""
    module = importlib.import_module("symphony.workflow.config")
    module.validate_dispatch_config(config)


class _Unset:
    """Sentinel distinguishing "no reconciler given" from an explicit ``None``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()


async def module_reconciler(orch: Orchestrator) -> None:
    """Default reconciler: delegate SPEC 8.5 / 16.3 to ``orchestrator.reconcile``.

    CONTRACTS.md assigns the SPEC 8.5 branch table to
    ``symphony.orchestrator.reconcile``, so that module owns the *decision* and
    this orchestrator owns the *mutation*. The bridge below is the whole seam:
    ``reconcile`` threads a ``state`` argument through its callbacks because it
    is written as a pure planner over any state, while this orchestrator holds
    exactly one state and ignores the parameter.

    Running here means running inside the mailbox task, so driving the public
    mutators keeps the SPEC 7 sole-mutator invariant intact.
    """
    reconcile = importlib.import_module("symphony.orchestrator.reconcile")

    def _terminate(_state: Any, issue_id: str, **kw: Any) -> Any:
        return orch.terminate_running_issue(issue_id, **kw)

    def _retry(_state: Any, issue_id: str, **kw: Any) -> Any:
        attempt = kw.pop("attempt")
        return orch.schedule_retry(issue_id, attempt, **kw)

    await reconcile.reconcile_running_issues(
        orch.state,
        cfg=orch.config,
        tracker=orch.tracker,
        deps=reconcile.ReconcileDeps(
            terminate_running_issue=_terminate,
            schedule_retry=_retry,
            now=orch.clock.now,
            logger=orch.log,
        ),
    )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _normalize_attempt(attempt: int | None) -> int | None:
    """SPEC 12.3: ``None`` on a first run, ``>= 1`` for retries/continuations."""
    if attempt is None:
        return None
    try:
        value = int(attempt)
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


def _next_attempt(attempt: int | None) -> int:
    """SPEC 16.4 ``next_attempt`` / 16.6 ``next_attempt_from``."""
    normalized = _normalize_attempt(attempt)
    return (normalized or 0) + 1


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _elapsed_ms(now: datetime, since: datetime) -> float:
    return max((_as_utc(now) - _as_utc(since)).total_seconds() * 1000.0, 0.0)


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


class Orchestrator:
    """The single authority over scheduling state (SPEC 7).

    Everything that could mutate :attr:`state` is funnelled through one mailbox
    drained by one task, so read-modify-write sequences are never interleaved:

    * poll ticks (SPEC 8.1 / 16.2)
    * dispatch and claim bookkeeping (SPEC 16.4)
    * worker exits, normal and abnormal (SPEC 16.6)
    * retry-timer firings (SPEC 8.4 / 16.6)
    * agent update events (SPEC 7.3, 13.5)
    * config reloads (SPEC 6.2)
    """

    def __init__(
        self,
        *,
        config: ServiceConfig,
        tracker: TrackerAdapter,
        runner: AgentRunnerLike,
        workspaces: WorkspaceCleaner,
        deps: OrchestratorDeps | None = None,
        reconciler: Callable[[Orchestrator], Any] | _Unset | None = _UNSET,
        validate: Callable[[Any], None] | None = None,
        clock: Clock | None = None,
        logger: Any | None = None,
    ) -> None:
        self.config = config
        self.tracker = tracker
        self.runner = runner
        self.workspaces = workspaces
        # Omitted -> delegate to symphony.orchestrator.reconcile, which owns the
        # SPEC 8.5 branch table. Explicit None -> use the built-in fallback.
        self.reconciler: Callable[[Orchestrator], Any] | None = (
            module_reconciler if isinstance(reconciler, _Unset) else reconciler
        )
        self.deps = deps if deps is not None else OrchestratorDeps.load()
        self.clock: Clock = clock if clock is not None else AsyncioClock()
        self.log = logger if logger is not None else _default_logger()
        self._validate = validate if validate is not None else _lazy_validate

        # SPEC 16.1 — the initial runtime state.
        self.state = OrchestratorState(
            poll_interval_ms=int(getattr(config, "poll_interval_ms", 30_000)),
            max_concurrent_agents=int(getattr(config, "max_concurrent_agents", 10)),
        )

        self._mailbox: asyncio.Queue[Any] = asyncio.Queue()
        self._loop_task: asyncio.Task[None] | None = None
        self._tick_handle: TimerHandle | None = None
        self._closing = False
        self._observers: list[Callable[[OrchestratorState], None]] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    async def start(self, *, initial_tick: bool = True) -> None:
        """Validate, clean stale terminal workspaces, then run (SPEC 16.1).

        Startup validation failure fails startup by propagating (SPEC 6.3);
        the mailbox loop is not started in that case.
        """
        if self.started:
            raise RuntimeError("orchestrator already started")

        self._closing = False
        self._validate(self.config)
        await self._startup_terminal_cleanup()

        self._loop_task = asyncio.create_task(self._mailbox_loop(), name="symphony-orchestrator")
        if initial_tick:
            self._mailbox.put_nowait(_Tick(reschedule=True))

    async def run_forever(self) -> None:
        """Await the mailbox loop; returns once :meth:`stop` completes."""
        if self._loop_task is None:
            raise RuntimeError("orchestrator not started")
        await self._loop_task

    async def stop(self) -> None:
        """Cancel timers and workers, drain the mailbox, and halt the loop.

        SPEC 14.3: scheduler state is intentionally in-memory. Nothing here is
        persisted; restart recovery is tracker- and filesystem-driven.
        """
        self._closing = True

        if self._tick_handle is not None:
            self._tick_handle.cancel()
            self._tick_handle = None

        for entry in self.state.retry_attempts.values():
            _cancel_handle(entry.timer_handle)
        self.state.retry_attempts.clear()

        workers = [
            entry.worker_handle
            for entry in self.state.running.values()
            if isinstance(entry.worker_handle, asyncio.Task)
        ]
        for task in workers:
            task.cancel()

        loop_task, self._loop_task = self._loop_task, None
        if loop_task is not None and not loop_task.done():
            self._mailbox.put_nowait(_Stop())
            await loop_task

        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def aclose(self) -> None:
        """Alias for :meth:`stop`, for ``contextlib.aclosing`` users."""
        await self.stop()

    # -- serialized entry points ------------------------------------------

    async def tick(self, *, reschedule: bool = False) -> None:
        """Run exactly one poll-and-dispatch tick and await its completion.

        ``reschedule`` defaults to ``False`` so a manual or test-driven tick does
        not start the periodic chain; :meth:`start` schedules ``reschedule=True``.
        """
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._mailbox.put_nowait(_Tick(reschedule=reschedule, future=future))
        await future

    async def apply_config(self, config: ServiceConfig) -> None:
        """Re-apply reloaded workflow config to live behavior (SPEC 6.2, 8.1)."""
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._mailbox.put_nowait(_ApplyConfig(config=config, future=future))
        await future

    async def invoke(self, fn: Callable[[Orchestrator], T]) -> T:
        """Run ``fn(self)`` inside the mailbox task and return its result.

        The supported way for an RLM driver, HTTP handler, or test to read or
        mutate :attr:`state` without breaking the SPEC 7 sole-mutator invariant.
        """
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._mailbox.put_nowait(_Invoke(fn=fn, future=future))
        return await future

    async def drain(self) -> None:
        """Block until every queued command has been processed."""
        await self._mailbox.join()

    def report_agent_event(self, issue_id: str, event: AgentEventLike) -> None:
        """Worker-side callback for SPEC 10.4 events. Never mutates state directly."""
        self._mailbox.put_nowait(_AgentUpdate(issue_id=issue_id, event=event))

    def report_phase(self, issue_id: str, phase: RunPhase, error: str | None = None) -> None:
        """Worker-side callback for SPEC 7.2 run-attempt phase transitions."""
        self._mailbox.put_nowait(_PhaseUpdate(issue_id=issue_id, phase=phase, error=error))

    def request_tick(self, delay_ms: float = 0.0) -> None:
        """Schedule a tick without awaiting it (SPEC 16.1 ``schedule_tick``)."""
        if delay_ms <= 0:
            self._mailbox.put_nowait(_Tick(reschedule=True))
            return
        self._arm_tick_timer(delay_ms)

    def add_observer(self, callback: Callable[[OrchestratorState], None]) -> None:
        """Register a SPEC 8.1 step 6 state-change consumer."""
        self._observers.append(callback)

    def remove_observer(self, callback: Callable[[OrchestratorState], None]) -> None:
        if callback in self._observers:
            self._observers.remove(callback)

    # -- mailbox loop ------------------------------------------------------

    async def _mailbox_loop(self) -> None:
        while True:
            command = await self._mailbox.get()
            try:
                if isinstance(command, _Stop):
                    return
                await self._handle(command)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # SPEC 14.2 — the loop must survive anything
                self.log.error(
                    "orchestrator command failed",
                    command=type(command).__name__,
                    error=_describe(exc),
                )
            finally:
                self._mailbox.task_done()

    async def _handle(self, command: Any) -> None:
        if isinstance(command, _Tick):
            await self._handle_tick(command)
        elif isinstance(command, _WorkerExit):
            await self._handle_worker_exit(command)
        elif isinstance(command, _RetryFired):
            await self._handle_retry_timer(command.issue_id)
        elif isinstance(command, _AgentUpdate):
            self._handle_agent_update(command)
        elif isinstance(command, _PhaseUpdate):
            self._handle_phase_update(command)
        elif isinstance(command, _ApplyConfig):
            self._handle_apply_config(command)
        elif isinstance(command, _Invoke):
            self._handle_invoke(command)
        else:  # pragma: no cover - defensive
            self.log.warning("unknown orchestrator command", command=type(command).__name__)

    # -- SPEC 16.2 poll-and-dispatch tick ----------------------------------

    async def _handle_tick(self, command: _Tick) -> None:
        try:
            await self._run_tick()
        finally:
            self._notify()
            if command.reschedule and not self._closing:
                self._arm_tick_timer(self.state.poll_interval_ms)
            if command.future is not None and not command.future.done():
                command.future.set_result(None)

    async def _run_tick(self) -> None:
        """SPEC 8.1 / 16.2.

        Order is load-bearing: reconcile first, then validate, then fetch, then
        dispatch. A service whose workflow file is broken still stops runs whose
        issues went terminal, because reconciliation already happened.
        """
        await self._reconcile_running_issues()

        if self._closing:
            return

        try:
            self._validate(self.config)
        except Exception as exc:  # SPEC 6.3 / 14.2 — skip dispatch, stay alive
            self.log.error("dispatch validation failed", error=_describe(exc))
            return

        try:
            issues = await self.tracker.fetch_issues_by_states(list(self.config.active_states))
        except Exception as exc:  # SPEC 14.2 — skip this tick, retry next tick
            self.log.error("candidate fetch failed", error=_describe(exc))
            return

        for issue in self.deps.sort_for_dispatch(issues):
            if self.deps.available_slots(self.state, self.config) <= 0:
                break
            # SPEC 7.4: claimed and running checks are REQUIRED before launching
            # any worker. Enforced here as well as inside should_dispatch so a
            # policy regression cannot produce a duplicate worker.
            if issue.id in self.state.running or issue.id in self.state.claimed:
                continue
            if self.deps.should_dispatch(issue, self.state, self.config):
                self._dispatch_issue(issue, None)

    # -- SPEC 16.4 dispatch one issue --------------------------------------

    def _dispatch_issue(self, issue: Issue, attempt: int | None) -> None:
        """Spawn a worker and take the claim (SPEC 16.4).

        A failed spawn schedules a retry instead of leaving the issue claimed and
        forgotten, and dispatch clears any pending retry entry for the issue.
        """
        if issue.id in self.state.running:  # never two workers for one issue
            return

        try:
            worker = self._spawn_worker(issue, attempt)
        except Exception as exc:
            self.log.error(
                "failed to spawn agent",
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                error=_describe(exc),
            )
            self.schedule_retry(
                issue.id,
                _next_attempt(attempt),
                identifier=issue.identifier,
                error="failed to spawn agent",
            )
            return

        self.state.running[issue.id] = RunningEntry(
            issue=issue,
            identifier=issue.identifier,
            started_at=self.clock.now(),
            worker_handle=worker,
            session=LiveSession(),
            retry_attempt=_normalize_attempt(attempt),
            phase=RunPhase.PREPARING_WORKSPACE,
        )
        self.state.claimed.add(issue.id)
        self._cancel_retry(issue.id)

        self.log.info(
            "dispatched issue",
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=_normalize_attempt(attempt),
            state=issue.state,
        )

    def _spawn_worker(self, issue: Issue, attempt: int | None) -> asyncio.Task[None]:
        coroutine = self.runner.run_attempt(issue, attempt)
        task = asyncio.create_task(coroutine, name=f"symphony-worker:{issue.identifier}")
        task.add_done_callback(lambda done: self._on_worker_done(issue.id, done))
        return task

    def _on_worker_done(self, issue_id: str, task: asyncio.Task[None]) -> None:
        """Worker-task callback. Enqueues only — SPEC 7 forbids mutating here."""
        if task.cancelled():
            normal, reason, phase = False, "cancelled", RunPhase.CANCELED_BY_RECONCILIATION
        else:
            exc = task.exception()
            if exc is None:
                normal, reason, phase = True, "normal", RunPhase.SUCCEEDED
            else:
                category = getattr(exc, "category", None)
                phase = RunPhase.TIMED_OUT if category in _TIMEOUT_CATEGORIES else RunPhase.FAILED
                normal, reason = False, _describe(exc)
        self._mailbox.put_nowait(
            _WorkerExit(issue_id=issue_id, task=task, normal=normal, reason=reason, phase=phase)
        )

    # -- SPEC 16.6 worker exit ---------------------------------------------

    async def _handle_worker_exit(self, command: _WorkerExit) -> None:
        entry = self.state.running.get(command.issue_id)
        if entry is None or entry.worker_handle is not command.task:
            # Already terminated by reconciliation, or superseded by a newer
            # dispatch. Termination did the bookkeeping; this exit is stale.
            return

        del self.state.running[command.issue_id]
        entry.phase = command.phase
        self._add_runtime_seconds(entry)

        if command.normal:
            # SPEC 7.1: a clean exit does not mean the issue is finished. The
            # orchestrator schedules a ~1s continuation retry so it can re-check
            # whether the issue is still active and needs another worker session.
            # This is distinct from the in-worker turn loop of SPEC 16.5.
            self.state.completed.add(command.issue_id)
            self.schedule_retry(
                command.issue_id,
                1,
                identifier=entry.identifier,
                error=None,
                delay_ms=self.deps.continuation_delay_ms,
            )
            self.log.info(
                "worker exited normally",
                issue_id=command.issue_id,
                issue_identifier=entry.identifier,
            )
        else:
            entry.last_error = command.reason
            self.schedule_retry(
                command.issue_id,
                _next_attempt(entry.retry_attempt),
                identifier=entry.identifier,
                error=f"worker exited: {command.reason}",
            )
            self.log.warning(
                "worker exited abnormally",
                issue_id=command.issue_id,
                issue_identifier=entry.identifier,
                reason=command.reason,
            )

        self._notify()

    # -- SPEC 8.4 / 16.6 retry ---------------------------------------------

    def schedule_retry(
        self,
        issue_id: str,
        attempt: int,
        *,
        identifier: str | None,
        error: str | None,
        delay_ms: float | None = None,
    ) -> None:
        """Create or replace the retry entry for ``issue_id`` (SPEC 8.4).

        Public so an injected reconciler can drive it. MUST be called from
        inside the mailbox task (SPEC 7); from outside, use :meth:`invoke`.

        ``delay_ms`` is supplied only for the SPEC 7.1 continuation retry, whose
        delay is the fixed ``CONTINUATION_DELAY_MS`` and whose ``error`` is
        ``None``. Everything else is failure-driven and uses exponential backoff
        capped by ``agent.max_retry_backoff_ms``. The ``error`` field is
        therefore the durable marker distinguishing the two in state.
        """
        self._cancel_retry(issue_id)
        if self._closing:
            self.state.claimed.discard(issue_id)
            return

        if delay_ms is None:
            delay_ms = self.deps.backoff_delay_ms(attempt, self.config.max_retry_backoff_ms)
        delay_ms = max(float(delay_ms), 0.0)

        handle = self.clock.call_later_ms(
            delay_ms, lambda: self._mailbox.put_nowait(_RetryFired(issue_id))
        )
        self.state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=self.clock.monotonic_ms() + delay_ms,
            timer_handle=handle,
            error=error,
        )
        # SPEC 7.1: claimed issues are either Running or RetryQueued. Holding the
        # claim across the retry window is what prevents a duplicate dispatch on
        # the next tick.
        self.state.claimed.add(issue_id)

    def _cancel_retry(self, issue_id: str) -> None:
        entry = self.state.retry_attempts.pop(issue_id, None)
        if entry is not None:
            _cancel_handle(entry.timer_handle)

    async def _handle_retry_timer(self, issue_id: str) -> None:
        """SPEC 8.4 retry handling / SPEC 16.6 ``on_retry_timer``."""
        entry = self.state.retry_attempts.pop(issue_id, None)
        if entry is None:
            return
        _cancel_handle(entry.timer_handle)

        if self._closing:
            self.state.claimed.discard(issue_id)
            return

        try:
            refreshed = await self.tracker.fetch_issues_by_ids([issue_id])
        except Exception as exc:
            self.log.warning("retry refresh failed", issue_id=issue_id, error=_describe(exc))
            self.schedule_retry(
                issue_id,
                entry.attempt + 1,
                identifier=entry.identifier,
                error="retry refresh failed",
            )
            self._notify()
            return

        issue = next((candidate for candidate in refreshed if candidate.id == issue_id), None)
        if issue is None:
            # SPEC 8.4 step 2 — omission means "no longer visible", release.
            self._release_claim(issue_id, "issue no longer visible")
            self._notify()
            return

        if self.config.is_terminal(issue.state):
            # SPEC 8.4 step 3 — terminal transitions observed on a retry refresh
            # clean the workspace before releasing.
            await self._cleanup_workspace(issue.identifier)
            self._release_claim(issue_id, "issue reached terminal state")
            self._notify()
            return

        if not self._retry_dispatch_allowed(issue):
            self._release_claim(issue_id, "issue no longer active or routable")
            self._notify()
            return

        if not self._has_slot_for(issue):
            self.schedule_retry(
                issue_id,
                entry.attempt + 1,
                identifier=issue.identifier,
                error="no available orchestrator slots",
            )
            self._notify()
            return

        self._dispatch_issue(issue, entry.attempt)
        self._notify()

    def _retry_dispatch_allowed(self, issue: Issue) -> bool:
        """SPEC 16.6 ``retry_dispatch_allowed(..., ignore_existing_claim=issue_id)``.

        The issue's own claim is deliberately ignored — it is claimed *because*
        this retry holds it. Claims held by any other path still block.
        """
        if issue.id in self.state.running:
            return False
        if self.config.is_terminal(issue.state) or not self.config.is_active(issue.state):
            return False
        return bool(self.deps.issue_routable(issue, self.config))

    def _has_slot_for(self, issue: Issue) -> bool:
        """SPEC 8.3 — global and per-state slots must both be available."""
        if self.deps.available_slots(self.state, self.config) <= 0:
            return False
        return bool(self.deps.has_state_slot(issue, self.state, self.config))

    def _release_claim(self, issue_id: str, reason: str) -> None:
        """SPEC 7.1 ``Released``."""
        self.state.claimed.discard(issue_id)
        self._cancel_retry(issue_id)
        self.log.info("released claim", issue_id=issue_id, reason=reason)

    # -- SPEC 8.5 / 16.3 reconciliation ------------------------------------

    async def _reconcile_running_issues(self) -> None:
        """SPEC 16.3, called first on every tick (SPEC 7.4, 8.1).

        The default reconciler is :func:`module_reconciler`, which delegates the
        SPEC 8.5 branch table to ``symphony.orchestrator.reconcile`` — the module
        CONTRACTS.md assigns those sections to. The fallback below stays as a
        self-contained implementation for hosts that inject ``reconciler=None``
        explicitly, and as the reference the delegating path is checked against.

        Either way the work runs inside the mailbox task and mutates only
        through :meth:`terminate_running_issue` and :meth:`schedule_retry`, so
        the SPEC 7 sole-mutator invariant holds.
        """
        if self.reconciler is not None:
            result = self.reconciler(self)
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                await result
            return

        await self._reconcile_stalled_runs()

        running_ids = list(self.state.running)
        if not running_ids:
            return

        try:
            refreshed = await self.tracker.fetch_issues_by_ids(running_ids)
        except Exception as exc:  # SPEC 8.5 — keep workers, try again next tick
            self.log.debug("state refresh failed; keep workers running", error=_describe(exc))
            return

        for issue in refreshed:
            if issue.id not in self.state.running:
                continue
            if self.config.is_terminal(issue.state):
                await self.terminate_running_issue(
                    issue.id,
                    cleanup_workspace=True,
                    retry=False,
                    reason="issue reached terminal state",
                    phase=RunPhase.CANCELED_BY_RECONCILIATION,
                )
            elif self.config.is_active(issue.state) and self.deps.issue_routable(
                issue, self.config
            ):
                self.state.running[issue.id].issue = issue
            else:
                await self.terminate_running_issue(
                    issue.id,
                    cleanup_workspace=False,
                    retry=False,
                    reason="issue no longer active or routable",
                    phase=RunPhase.CANCELED_BY_RECONCILIATION,
                )

        returned = {issue.id for issue in refreshed}
        for missing_id in running_ids:
            if missing_id in returned:
                continue
            await self.terminate_running_issue(
                missing_id,
                cleanup_workspace=False,
                retry=False,
                reason="issue no longer visible",
                phase=RunPhase.CANCELED_BY_RECONCILIATION,
            )

    async def _reconcile_stalled_runs(self) -> None:
        """SPEC 8.5 Part A — stall detection kills the worker and queues a retry."""
        timeout_ms = int(getattr(self.config.codex, "stall_timeout_ms", 0) or 0)
        if timeout_ms <= 0:
            return

        now = self.clock.now()
        stalled: list[tuple[str, float]] = []
        for issue_id, entry in self.state.running.items():
            since = entry.session.last_codex_timestamp or entry.started_at
            elapsed = _elapsed_ms(now, since)
            if elapsed > timeout_ms:
                stalled.append((issue_id, elapsed))

        for issue_id, elapsed in stalled:
            await self.terminate_running_issue(
                issue_id,
                cleanup_workspace=False,
                retry=True,
                reason=f"stalled: no agent activity for {elapsed:.0f}ms",
                phase=RunPhase.STALLED,
            )

    async def terminate_running_issue(
        self,
        issue_id: str,
        *,
        cleanup_workspace: bool,
        reason: str,
        retry: bool = False,
        phase: RunPhase = RunPhase.CANCELED_BY_RECONCILIATION,
    ) -> None:
        """SPEC 16.3 ``terminate_running_issue``.

        Public so an injected reconciler can drive it. MUST be called from
        inside the mailbox task (SPEC 7); from outside, use :meth:`invoke`.

        The running entry is removed here rather than when the cancelled worker
        task finally settles, so a later tick cannot observe it and terminate it
        twice. The stale :class:`_WorkerExit` is dropped by identity check.
        """
        entry = self.state.running.pop(issue_id, None)
        if entry is None:
            return

        entry.phase = phase
        entry.last_error = reason
        self._add_runtime_seconds(entry)

        if isinstance(entry.worker_handle, asyncio.Task):
            entry.worker_handle.cancel()

        if cleanup_workspace:
            await self._cleanup_workspace(entry.identifier)

        if retry:
            self.schedule_retry(
                issue_id,
                _next_attempt(entry.retry_attempt),
                identifier=entry.identifier,
                error=reason,
            )
            self.log.warning(
                "terminated running issue with retry",
                issue_id=issue_id,
                issue_identifier=entry.identifier,
                reason=reason,
            )
        else:
            self._release_claim(issue_id, reason)
            self.log.info(
                "terminated running issue",
                issue_id=issue_id,
                issue_identifier=entry.identifier,
                reason=reason,
                workspace_cleaned=cleanup_workspace,
            )

    # -- SPEC 8.6 startup terminal workspace cleanup -----------------------

    async def _startup_terminal_cleanup(self) -> None:
        """SPEC 8.6. A failed terminal fetch logs a warning and startup continues."""
        terminal = list(getattr(self.config, "terminal_states", ()) or ())
        if not terminal:
            return
        try:
            issues = await self.tracker.fetch_issues_by_states(terminal)
        except Exception as exc:
            self.log.warning("startup terminal cleanup skipped", error=_describe(exc))
            return
        for issue in issues:
            await self._cleanup_workspace(issue.identifier)

    async def _cleanup_workspace(self, identifier: str | None) -> bool:
        if not identifier:
            return False
        try:
            return bool(await self.workspaces.cleanup(identifier))
        except Exception as exc:  # SPEC 14.2 — never crash the orchestrator
            self.log.warning(
                "workspace cleanup failed", issue_identifier=identifier, error=_describe(exc)
            )
            return False

    # -- SPEC 7.3 / 13.5 agent updates -------------------------------------

    def _handle_agent_update(self, command: _AgentUpdate) -> None:
        entry = self.state.running.get(command.issue_id)
        if entry is None:
            return

        event = command.event
        session = entry.session
        session.last_codex_event = getattr(event, "event", None)
        timestamp = getattr(event, "timestamp", None)
        session.last_codex_timestamp = timestamp if timestamp is not None else self.clock.now()

        pid = getattr(event, "codex_app_server_pid", None)
        if pid:
            session.codex_app_server_pid = str(pid)

        payload = getattr(event, "payload", None) or {}
        self._absorb_session_ids(session, payload)
        self._absorb_token_totals(session, payload)

        limits = self.deps.extract_rate_limits(payload)
        if limits:
            self.state.codex_rate_limits = limits

        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            session.last_codex_message = message

        entry.recent_events.append(
            {
                "event": session.last_codex_event,
                "timestamp": session.last_codex_timestamp,
            }
        )
        if len(entry.recent_events) > RECENT_EVENT_LIMIT:
            del entry.recent_events[:-RECENT_EVENT_LIMIT]

    @staticmethod
    def _absorb_session_ids(session: LiveSession, payload: dict[str, Any]) -> None:
        thread_id = payload.get("thread_id") or payload.get("threadId")
        turn_id = payload.get("turn_id") or payload.get("turnId")
        if isinstance(thread_id, str) and thread_id:
            session.thread_id = thread_id
        if isinstance(turn_id, str) and turn_id:
            session.turn_id = turn_id
        if session.thread_id and session.turn_id:
            session.session_id = build_session_id(session.thread_id, session.turn_id)

    def _absorb_token_totals(self, session: LiveSession, payload: dict[str, Any]) -> None:
        """SPEC 13.5 — absolute totals only, accumulated as deltas."""
        totals = self.deps.extract_token_totals(payload)
        if totals is None:
            return
        absolute_in, absolute_out, absolute_total = (int(value) for value in totals)

        delta_in = max(absolute_in - session.last_reported_input_tokens, 0)
        delta_out = max(absolute_out - session.last_reported_output_tokens, 0)
        delta_total = max(absolute_total - session.last_reported_total_tokens, 0)

        session.codex_input_tokens += delta_in
        session.codex_output_tokens += delta_out
        session.codex_total_tokens += delta_total
        session.last_reported_input_tokens = absolute_in
        session.last_reported_output_tokens = absolute_out
        session.last_reported_total_tokens = absolute_total

        self.state.codex_totals.input_tokens += delta_in
        self.state.codex_totals.output_tokens += delta_out
        self.state.codex_totals.total_tokens += delta_total

    def _handle_phase_update(self, command: _PhaseUpdate) -> None:
        entry = self.state.running.get(command.issue_id)
        if entry is None:
            return
        entry.phase = command.phase
        if command.error is not None:
            entry.last_error = command.error

    # -- SPEC 6.2 config reload -------------------------------------------

    def _handle_apply_config(self, command: _ApplyConfig) -> None:
        try:
            self.config = command.config
            self.state.poll_interval_ms = int(getattr(command.config, "poll_interval_ms", 30_000))
            self.state.max_concurrent_agents = int(
                getattr(command.config, "max_concurrent_agents", 10)
            )
            # SPEC 8.1 — the effective poll interval follows the reloaded config.
            if self._tick_handle is not None and not self._closing:
                self._arm_tick_timer(self.state.poll_interval_ms)
            self.log.info(
                "applied reloaded config",
                poll_interval_ms=self.state.poll_interval_ms,
                max_concurrent_agents=self.state.max_concurrent_agents,
            )
            self._notify()
        finally:
            if command.future is not None and not command.future.done():
                command.future.set_result(None)

    def _handle_invoke(self, command: _Invoke) -> None:
        if command.future.done():
            return
        try:
            command.future.set_result(command.fn(self))
        except Exception as exc:
            command.future.set_exception(exc)

    # -- shared internals --------------------------------------------------

    def _arm_tick_timer(self, delay_ms: float) -> None:
        if self._tick_handle is not None:
            self._tick_handle.cancel()
        self._tick_handle = self.clock.call_later_ms(delay_ms, self._enqueue_periodic_tick)

    def _enqueue_periodic_tick(self) -> None:
        self._tick_handle = None
        if not self._closing:
            self._mailbox.put_nowait(_Tick(reschedule=True))

    def _add_runtime_seconds(self, entry: RunningEntry) -> None:
        """SPEC 13.5 — add run duration when a session ends, however it ends."""
        self.state.codex_totals.seconds_running += (
            _elapsed_ms(self.clock.now(), entry.started_at) / 1000.0
        )

    def _notify(self) -> None:
        """SPEC 8.1 step 6. Observer failures never propagate (SPEC 14.2)."""
        for observer in list(self._observers):
            try:
                observer(self.state)
            except Exception as exc:
                self.log.warning("observer failed", error=_describe(exc))


def _cancel_handle(handle: Any) -> None:
    if handle is None:
        return
    cancel = getattr(handle, "cancel", None)
    if callable(cancel):
        cancel()
