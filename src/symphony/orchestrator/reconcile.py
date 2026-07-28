"""Active-run reconciliation and startup terminal cleanup — SPEC 8.5, 8.6, 16.3.

Reconciliation is the mechanism that keeps the in-memory ``running`` map honest
about a tracker that other humans and systems are editing while agents work.
It runs *before* dispatch on every tick (SPEC 7.4, 8.1) and has two parts:

* Part A (:func:`reconcile_stalled_runs`) — kill sessions that stopped emitting
  activity and queue a retry.
* Part B — refresh the tracker state of every still-running issue and terminate,
  update, or leave each run alone.

:func:`reconcile_running_issues` is the tick entry point and performs both, in
that order.

The four Part B branches differ in exactly one consequential way: whether the
workspace is cleaned. Only a *terminal* tracker state cleans it. Everything else
that terminates does so without cleanup, because the issue may well come back and
the workspace is expensive, reusable state (SPEC 9.1).

This module owns the *decisions*. Killing a worker, releasing a claim, cleaning a
workspace, and arming a retry timer belong to ``symphony.orchestrator.core``,
which supplies them through :class:`ReconcileDeps`. That split keeps the branch
table pure, testable, and introspectable from a REPL: :func:`plan_reconciliation`
and :func:`plan_stall_terminations` answer "what would reconciliation do?"
without touching a tracker, a process, or the filesystem.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from symphony.models import Issue, OrchestratorState, RunningEntry, RunPhase

if TYPE_CHECKING:  # pragma: no cover - typing only
    from symphony.workflow.config import ServiceConfig

__all__ = [
    "REASON_MISSING",
    "REASON_NOT_ACTIVE",
    "REASON_STALLED",
    "REASON_TERMINAL",
    "REASON_UNROUTABLE",
    "ReconcileAction",
    "ReconcileDecision",
    "ReconcileDeps",
    "StallDecision",
    "classify_refreshed_issue",
    "elapsed_ms_for_entry",
    "next_attempt_for",
    "plan_reconciliation",
    "plan_stall_terminations",
    "reconcile_running_issues",
    "reconcile_stalled_runs",
    "startup_terminal_workspace_cleanup",
]


# --------------------------------------------------------------------------
# Termination reasons (stable strings; they reach logs and retry entries)
# --------------------------------------------------------------------------

#: SPEC 8.5 Part B — tracker state is terminal. The only branch that cleans up.
REASON_TERMINAL = "issue reached a terminal tracker state"

#: SPEC 8.5 Part B — still active, but ``dispatchable``/labels no longer match.
REASON_UNROUTABLE = "issue is active but no longer routable"

#: SPEC 8.5 Part B — state is in neither the active nor the terminal list.
REASON_NOT_ACTIVE = "issue state is neither active nor terminal"

#: SPEC 16.3 — the ID was running but absent from a *successful* refresh.
REASON_MISSING = "issue is no longer visible in the configured tracker scope"

#: SPEC 8.5 Part A — no coding-agent activity inside ``codex.stall_timeout_ms``.
REASON_STALLED = "no coding-agent activity within codex.stall_timeout_ms"


# --------------------------------------------------------------------------
# Collaborator protocols
# --------------------------------------------------------------------------


@runtime_checkable
class IssueFetcher(Protocol):
    """The two-operation read kernel this module needs (SPEC 11.1)."""

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]: ...

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]: ...


@runtime_checkable
class WorkspaceCleaner(Protocol):
    """The one ``WorkspaceManager`` operation startup cleanup needs (SPEC 9)."""

    async def cleanup(self, identifier: str) -> bool: ...


class LoggerLike(Protocol):
    """Structured logger surface (SPEC 13.1)."""

    def debug(self, msg: str, **fields: Any) -> None: ...

    def info(self, msg: str, **fields: Any) -> None: ...

    def warning(self, msg: str, **fields: Any) -> None: ...

    def error(self, msg: str, **fields: Any) -> None: ...


#: ``core.terminate_running_issue`` (SPEC 16.3). May be sync or async; may return
#: the next state or ``None`` when it mutates in place.
TerminateFn = Callable[..., "OrchestratorState | Awaitable[OrchestratorState | None] | None"]

#: ``core.schedule_retry`` (SPEC 8.4, 16.6). Same sync/async tolerance.
ScheduleRetryFn = Callable[..., "OrchestratorState | Awaitable[OrchestratorState | None] | None"]

#: ``scheduling.issue_routable`` (SPEC 8.2).
RoutableFn = Callable[[Issue, "ServiceConfig"], bool]


class _NullLogger:
    """Fallback used when no logger is injected and observability is absent."""

    def debug(self, msg: str, **fields: Any) -> None:
        return None

    info = warning = error = debug


_FALLBACK_LOGGER: LoggerLike = _NullLogger()
_RESOLVED_LOGGER: LoggerLike | None = None


def _module_logger() -> LoggerLike:
    """Resolve the structured logger lazily so import order stays free."""
    global _RESOLVED_LOGGER
    if _RESOLVED_LOGGER is None:
        try:
            from symphony.observability.logging import get_logger

            _RESOLVED_LOGGER = get_logger(__name__)
        except Exception:  # pragma: no cover - observability not installed
            _RESOLVED_LOGGER = _FALLBACK_LOGGER
    return _RESOLVED_LOGGER


def _default_routable(issue: Issue, cfg: ServiceConfig) -> bool:
    """Defer to ``scheduling.issue_routable`` (SPEC 8.2).

    Imported at call time rather than module scope so that reconciliation stays
    importable and unit-testable without the scheduling module, and so tests can
    inject a fake through :attr:`ReconcileDeps.routable`.
    """
    from symphony.orchestrator.scheduling import issue_routable

    return issue_routable(issue, cfg)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ReconcileDeps:
    """Everything reconciliation needs from its host (SPEC 16.3).

    ``terminate_running_issue`` is invoked as
    ``fn(state, issue_id, cleanup_workspace=..., reason=...)`` and
    ``schedule_retry`` as
    ``fn(state, issue_id, attempt=..., identifier=..., error=...)``. Both may be
    sync or async and may return the next :class:`OrchestratorState` or ``None``
    if they mutate the state object in place.
    """

    terminate_running_issue: TerminateFn
    schedule_retry: ScheduleRetryFn
    now: Callable[[], datetime] = _utcnow
    routable: RoutableFn | None = None
    logger: LoggerLike | None = None

    @property
    def log(self) -> LoggerLike:
        return self.logger if self.logger is not None else _module_logger()

    @property
    def is_routable(self) -> RoutableFn:
        return self.routable if self.routable is not None else _default_routable


# --------------------------------------------------------------------------
# SPEC 8.5 Part A — stall detection (pure)
# --------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than raising on subtraction."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def elapsed_ms_for_entry(entry: RunningEntry, now: datetime) -> float:
    """Milliseconds of silence for one running issue (SPEC 8.5 Part A).

    Measured from ``last_codex_timestamp`` when any event has been seen, else
    from ``started_at``. A run that has never emitted an event is therefore
    judged on total age, while an active one is judged on time since its last
    event.
    """
    reference = entry.session.last_codex_timestamp if entry.session is not None else None
    if reference is None:
        reference = entry.started_at
    if reference is None:  # pragma: no cover - RunningEntry always sets started_at
        return 0.0
    return (_as_utc(now) - _as_utc(reference)).total_seconds() * 1000.0


def next_attempt_for(entry: RunningEntry) -> int:
    """``next_attempt_from(running_entry)`` of SPEC 16.6.

    Attempts are 1-based (SPEC 4.1.7); a first run carries ``retry_attempt=None``
    and its next attempt is ``1``.
    """
    current = entry.retry_attempt
    if isinstance(current, int) and not isinstance(current, bool) and current >= 1:
        return current + 1
    return 1


@dataclass(frozen=True, slots=True)
class StallDecision:
    """One stalled run to terminate and requeue (SPEC 8.5 Part A)."""

    issue_id: str
    identifier: str
    elapsed_ms: float
    attempt: int
    reason: str = REASON_STALLED
    phase: RunPhase = RunPhase.STALLED


def plan_stall_terminations(
    state: OrchestratorState, *, stall_timeout_ms: int, now: datetime
) -> list[StallDecision]:
    """List the runs that breached the stall timeout (SPEC 8.5 Part A).

    A non-positive ``stall_timeout_ms`` disables stall detection entirely — it
    means "no stall deadline", never "every run is instantly stalled".
    The comparison is strict (``elapsed > timeout``), so a run sitting exactly on
    the deadline survives one more tick.
    """
    if stall_timeout_ms <= 0:
        return []
    decisions: list[StallDecision] = []
    for issue_id, entry in list(state.running.items()):
        elapsed = elapsed_ms_for_entry(entry, now)
        if elapsed > stall_timeout_ms:
            decisions.append(
                StallDecision(
                    issue_id=issue_id,
                    identifier=entry.identifier,
                    elapsed_ms=elapsed,
                    attempt=next_attempt_for(entry),
                )
            )
    return decisions


# --------------------------------------------------------------------------
# SPEC 8.5 Part B / 16.3 — tracker state refresh (pure)
# --------------------------------------------------------------------------


class ReconcileAction(StrEnum):
    """What Part B decided for one running issue (SPEC 8.5)."""

    UPDATE_SNAPSHOT = "update_snapshot"
    TERMINATE_AND_CLEAN = "terminate_and_clean"
    TERMINATE_NO_CLEANUP = "terminate_no_cleanup"


@dataclass(frozen=True, slots=True)
class ReconcileDecision:
    """One Part B outcome, carrying the refreshed snapshot when there is one."""

    issue_id: str
    action: ReconcileAction
    reason: str
    phase: RunPhase | None = None
    issue: Issue | None = None

    @property
    def terminates(self) -> bool:
        return self.action is not ReconcileAction.UPDATE_SNAPSHOT

    @property
    def cleanup_workspace(self) -> bool:
        """Only a terminal tracker state cleans the workspace (SPEC 8.5)."""
        return self.action is ReconcileAction.TERMINATE_AND_CLEAN


def classify_refreshed_issue(
    issue: Issue, cfg: ServiceConfig, *, routable: RoutableFn | None = None
) -> ReconcileDecision:
    """Apply the SPEC 8.5 Part B branch table to one refreshed issue.

    Order matters: terminal is tested first, so a state configured as both active
    and terminal terminates *and* cleans (SPEC 16.3).
    """
    check = routable if routable is not None else _default_routable

    if cfg.is_terminal(issue.state):
        return ReconcileDecision(
            issue_id=issue.id,
            action=ReconcileAction.TERMINATE_AND_CLEAN,
            reason=REASON_TERMINAL,
            phase=RunPhase.CANCELED_BY_RECONCILIATION,
            issue=issue,
        )

    if cfg.is_active(issue.state):
        if check(issue, cfg):
            return ReconcileDecision(
                issue_id=issue.id,
                action=ReconcileAction.UPDATE_SNAPSHOT,
                reason="issue is still active and routable",
                phase=None,
                issue=issue,
            )
        return ReconcileDecision(
            issue_id=issue.id,
            action=ReconcileAction.TERMINATE_NO_CLEANUP,
            reason=REASON_UNROUTABLE,
            phase=RunPhase.CANCELED_BY_RECONCILIATION,
            issue=issue,
        )

    return ReconcileDecision(
        issue_id=issue.id,
        action=ReconcileAction.TERMINATE_NO_CLEANUP,
        reason=REASON_NOT_ACTIVE,
        phase=RunPhase.CANCELED_BY_RECONCILIATION,
        issue=issue,
    )


def plan_reconciliation(
    running_ids: Sequence[str],
    refreshed: Iterable[Issue],
    cfg: ServiceConfig,
    *,
    routable: RoutableFn | None = None,
) -> list[ReconcileDecision]:
    """Turn one successful refresh into an ordered decision list (SPEC 16.3).

    Callers MUST only pass the result of a *successful* refresh. A failed refresh
    produces no decisions at all — treating it as an empty result would terminate
    every live session on one transient network error (SPEC 8.5).

    Refreshed issues that are not in ``running_ids`` are ignored; the orchestrator
    only reconciles what it dispatched. IDs that were running but absent from the
    result are terminated without cleanup, because SPEC 11.1 defines omission as
    "no longer visible in scope", not "unchanged".
    """
    known = list(dict.fromkeys(running_ids))
    known_set = set(known)
    decisions: list[ReconcileDecision] = []
    returned: set[str] = set()

    for issue in refreshed:
        if issue.id not in known_set or issue.id in returned:
            continue
        returned.add(issue.id)
        decisions.append(classify_refreshed_issue(issue, cfg, routable=routable))

    for issue_id in known:
        if issue_id not in returned:
            decisions.append(
                ReconcileDecision(
                    issue_id=issue_id,
                    action=ReconcileAction.TERMINATE_NO_CLEANUP,
                    reason=REASON_MISSING,
                    phase=RunPhase.CANCELED_BY_RECONCILIATION,
                )
            )
    return decisions


# --------------------------------------------------------------------------
# Async drivers
# --------------------------------------------------------------------------


async def _next_state(result: Any, fallback: OrchestratorState) -> OrchestratorState:
    """Accept sync or async host callbacks that may mutate state in place."""
    if inspect.isawaitable(result):
        result = await result
    return result if isinstance(result, OrchestratorState) else fallback


async def reconcile_stalled_runs(
    state: OrchestratorState, *, cfg: ServiceConfig, deps: ReconcileDeps
) -> OrchestratorState:
    """Terminate stalled runs and queue their retries (SPEC 8.5 Part A).

    Termination does **not** clean the workspace: a stall is a session failure,
    the issue is still active, and the retry will reuse the same workspace
    (SPEC 9.1).
    """
    log = deps.log
    decisions = plan_stall_terminations(
        state, stall_timeout_ms=cfg.codex.stall_timeout_ms, now=deps.now()
    )
    for decision in decisions:
        if decision.issue_id not in state.running:
            continue
        log.warning(
            "terminating stalled run",
            issue_id=decision.issue_id,
            issue_identifier=decision.identifier,
            elapsed_ms=round(decision.elapsed_ms),
            stall_timeout_ms=cfg.codex.stall_timeout_ms,
            phase=decision.phase.value,
        )
        state = await _next_state(
            deps.terminate_running_issue(
                state,
                decision.issue_id,
                cleanup_workspace=False,
                reason=decision.reason,
            ),
            state,
        )
        state = await _next_state(
            deps.schedule_retry(
                state,
                decision.issue_id,
                attempt=decision.attempt,
                identifier=decision.identifier,
                error=decision.reason,
            ),
            state,
        )
    return state


async def reconcile_running_issues(
    state: OrchestratorState,
    *,
    cfg: ServiceConfig,
    tracker: IssueFetcher,
    deps: ReconcileDeps,
) -> OrchestratorState:
    """Run both reconciliation parts for one tick (SPEC 8.5, 16.3).

    Stall detection runs first, so runs it terminates are gone from ``running``
    before the tracker refresh and are never refreshed.

    With no running issues this is a no-op and issues no provider request. If the
    refresh fails, every worker keeps running and the next tick tries again.
    """
    log = deps.log
    state = await reconcile_stalled_runs(state, cfg=cfg, deps=deps)

    running_ids = list(state.running)
    if not running_ids:
        return state

    try:
        refreshed = await tracker.fetch_issues_by_ids(list(running_ids))
    except Exception as exc:
        # SPEC 8.5: a failed refresh is not evidence that anything changed.
        log.debug(
            "reconciliation state refresh failed; keeping workers running",
            running=len(running_ids),
            error_category=getattr(exc, "category", type(exc).__name__),
            error=str(exc),
        )
        return state

    for decision in plan_reconciliation(running_ids, refreshed, cfg, routable=deps.is_routable):
        entry = state.running.get(decision.issue_id)
        if entry is None:
            # The worker exited while we were awaiting; nothing left to reconcile.
            continue

        if not decision.terminates:
            if decision.issue is not None:
                entry.issue = decision.issue
                log.debug(
                    "refreshed running issue snapshot",
                    issue_id=decision.issue_id,
                    issue_identifier=entry.identifier,
                    state=decision.issue.state,
                )
            continue

        log.info(
            "terminating run after reconciliation",
            issue_id=decision.issue_id,
            issue_identifier=entry.identifier,
            reason=decision.reason,
            cleanup_workspace=decision.cleanup_workspace,
        )
        state = await _next_state(
            deps.terminate_running_issue(
                state,
                decision.issue_id,
                cleanup_workspace=decision.cleanup_workspace,
                reason=decision.reason,
            ),
            state,
        )

    return state


# --------------------------------------------------------------------------
# SPEC 8.6 — Startup terminal workspace cleanup
# --------------------------------------------------------------------------


async def startup_terminal_workspace_cleanup(
    *,
    cfg: ServiceConfig,
    tracker: IssueFetcher,
    workspaces: WorkspaceCleaner,
    logger: LoggerLike | None = None,
) -> list[str]:
    """Remove workspaces of issues already terminal at startup (SPEC 8.6).

    Returns the identifiers whose workspace was actually removed. A failed
    terminal-issues fetch logs a warning and returns empty: startup continues,
    and stale workspaces are simply cleaned on a later restart or by
    reconciliation when the issue is next observed terminal.
    """
    log = logger if logger is not None else _module_logger()
    terminal_states = list(cfg.terminal_states)
    if not terminal_states:
        log.debug("startup terminal cleanup skipped: no terminal states configured")
        return []

    try:
        issues = await tracker.fetch_issues_by_states(terminal_states)
    except Exception as exc:
        log.warning(
            "startup terminal workspace cleanup skipped: terminal issue fetch failed",
            error_category=getattr(exc, "category", type(exc).__name__),
            error=str(exc),
        )
        return []

    removed: list[str] = []
    for issue in issues:
        try:
            was_removed = await workspaces.cleanup(issue.identifier)
        except Exception as exc:
            # One unremovable directory must not abort cleanup of the rest.
            log.warning(
                "failed to remove terminal workspace",
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                error_category=getattr(exc, "category", type(exc).__name__),
                error=str(exc),
            )
            continue
        if was_removed:
            removed.append(issue.identifier)

    log.info(
        "startup terminal workspace cleanup complete",
        terminal_issues=len(issues),
        workspaces_removed=len(removed),
    )
    return removed
