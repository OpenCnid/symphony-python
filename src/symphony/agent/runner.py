"""Agent runner — the worker body (SPEC 10.7, reference algorithm SPEC 16.5).

The runner is the *only* component that owns a coding-agent attempt end to end:
workspace preparation, prompt construction, app-server session lifecycle, and
the in-worker continuation turn loop described in SPEC 7.1 and 10.3.

Two continuation mechanisms exist by design and are easy to confuse:

* the **in-worker** loop implemented here, which keeps one live thread and one
  live subprocess and issues back-to-back turns while the issue stays active
  and routable (SPEC 7.1, 10.3, 16.5);
* the **orchestrator's** post-exit continuation retry (~1 s, SPEC 16.6), which
  spawns a *new* worker. That one is not implemented here.

Failure convention: ``run_attempt`` returning normally is SPEC 16.5's
``exit_normal()``; raising is ``fail_worker(...)``. The original sibling error is
re-raised unchanged so its SPEC 10.6 / 11.4 ``category`` survives to the
orchestrator; the SPEC 16.5 reason string is attached to the failure log instead
of replacing the error type.

Cleanup convention (SPEC 16.5, and the reason this module is structured the way
it is): once ``before_run`` has succeeded, *every* exit path runs the
``after_run`` hook best-effort, and every exit path that has a live session also
stops that session first. See :data:`EXIT_PATHS` for the machine-readable table.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from symphony.models import Issue, RunPhase, WorkflowDefinition, Workspace
from symphony.trackers.base import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from symphony.agent.events import AgentEvent
    from symphony.workflow.config import CodexConfig, ServiceConfig

__all__ = [
    "BREAK_ISSUE_MISSING",
    "BREAK_MAX_TURNS",
    "BREAK_NOT_ROUTABLE",
    "BREAK_REASONS",
    "BREAK_STATE_INACTIVE",
    "EXIT_PATHS",
    "FAIL_BEFORE_RUN",
    "FAIL_ISSUE_REFRESH",
    "FAIL_PROMPT",
    "FAIL_REASONS",
    "FAIL_SESSION_STARTUP",
    "FAIL_TURN",
    "FAIL_WORKSPACE",
    "AgentRunner",
    "AppServerClientLike",
    "AppServerFactory",
    "AppServerSessionLike",
    "HookRunnerLike",
    "LoggerLike",
    "TrackerLike",
    "WorkspaceManagerLike",
    "turn_title",
]


# --------------------------------------------------------------------------
# SPEC 16.5 — fail_worker reasons, verbatim from the reference algorithm
# --------------------------------------------------------------------------

FAIL_WORKSPACE = "workspace error"
FAIL_BEFORE_RUN = "before_run hook error"
FAIL_SESSION_STARTUP = "agent session startup error"
FAIL_PROMPT = "prompt error"
FAIL_TURN = "agent turn error"
FAIL_ISSUE_REFRESH = "issue state refresh error"

FAIL_REASONS: tuple[str, ...] = (
    FAIL_WORKSPACE,
    FAIL_BEFORE_RUN,
    FAIL_SESSION_STARTUP,
    FAIL_PROMPT,
    FAIL_TURN,
    FAIL_ISSUE_REFRESH,
)

# The four SPEC 16.5 `break` conditions. Each ends the turn loop *normally*:
# the attempt still exits via `exit_normal()` (SPEC 16.5) and the orchestrator
# still schedules its continuation retry (SPEC 7.1, 16.6).
BREAK_ISSUE_MISSING = "issue no longer visible"
BREAK_STATE_INACTIVE = "issue state no longer active"
BREAK_NOT_ROUTABLE = "issue no longer routable"
BREAK_MAX_TURNS = "max_turns reached"

BREAK_REASONS: tuple[str, ...] = (
    BREAK_ISSUE_MISSING,
    BREAK_STATE_INACTIVE,
    BREAK_NOT_ROUTABLE,
    BREAK_MAX_TURNS,
)

# Machine-readable transcription of the SPEC 16.5 cleanup ordering, kept next to
# the code it constrains so a conformance reviewer (or the RLM) can diff intent
# against behavior without re-reading the pseudocode.
# reason -> (stops_session, runs_after_run)
EXIT_PATHS: dict[str, tuple[bool, bool]] = {
    FAIL_WORKSPACE: (False, False),
    FAIL_BEFORE_RUN: (False, False),
    FAIL_SESSION_STARTUP: (False, True),
    FAIL_PROMPT: (True, True),
    FAIL_TURN: (True, True),
    FAIL_ISSUE_REFRESH: (True, True),
    BREAK_ISSUE_MISSING: (True, True),
    BREAK_STATE_INACTIVE: (True, True),
    BREAK_NOT_ROUTABLE: (True, True),
    BREAK_MAX_TURNS: (True, True),
}

_BEFORE_RUN = "before_run"
_AFTER_RUN = "after_run"


def turn_title(issue: Issue) -> str:
    """Issue-identifying turn metadata (SPEC 10.2): ``<identifier>: <title>``."""
    return f"{issue.identifier}: {issue.title}"


# --------------------------------------------------------------------------
# Collaborator protocols
#
# Declared structurally rather than imported, for two reasons: the sibling
# modules are written concurrently against CONTRACTS.md, and every collaborator
# has to be injectable so this module is testable in isolation.
# --------------------------------------------------------------------------


class WorkspaceManagerLike(Protocol):
    """``symphony.workspace.manager.WorkspaceManager`` (SPEC 9.2)."""

    async def create_for_issue(self, identifier: str) -> Workspace: ...


class HookRunnerLike(Protocol):
    """``symphony.workspace.hooks.HookRunner`` (SPEC 9.4)."""

    async def run(self, name: str, cwd: Path, *, fatal: bool) -> None: ...


class TrackerLike(Protocol):
    """The slice of ``TrackerAdapter`` the runner needs (SPEC 11.1, 10.5)."""

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]: ...

    def agent_tool_specs(self) -> list[ToolSpec]: ...

    def secret_environment_names(self) -> list[str]: ...

    async def execute_agent_tool(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult: ...


class AppServerSessionLike(Protocol):
    """``symphony.agent.app_server.AppServerSession`` (SPEC 10.2)."""

    thread_id: str

    async def stop(self) -> None: ...


class AppServerClientLike(Protocol):
    """``symphony.agent.app_server.AppServerClient`` (SPEC 10.1-10.3)."""

    async def start_session(self) -> AppServerSessionLike: ...

    async def run_turn(
        self, session: AppServerSessionLike, prompt: str, *, title: str | None = None
    ) -> None: ...


class AppServerFactory(Protocol):
    """Constructor shape of ``AppServerClient`` per CONTRACTS.md.

    The client binds its workspace at construction time, so the runner can only
    build it after ``create_for_issue`` has returned. That is why the runner
    holds a factory rather than a client.
    """

    def __call__(
        self,
        cfg: CodexConfig,
        *,
        workspace: Path,
        tool_specs: list[ToolSpec],
        tool_executor: ToolExecutor,
        on_event: Callable[[AgentEvent], None],
        secret_env_names: Sequence[str] = (),
        approval_decider: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> AppServerClientLike: ...


class LoggerLike(Protocol):
    """``symphony.observability.logging.StructuredLogger`` (SPEC 13.1)."""

    def debug(self, msg: str, **fields: Any) -> None: ...

    def info(self, msg: str, **fields: Any) -> None: ...

    def warning(self, msg: str, **fields: Any) -> None: ...

    def error(self, msg: str, **fields: Any) -> None: ...

    def bind(self, **fields: Any) -> LoggerLike: ...


#: Host-side tool execution, with the SPEC 10.5 issue context already bound by
#: the runner (SPEC 17.5: "the current normalized issue and ``native_ref`` are
#: available as internal tool context").
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]

PromptRenderer = Callable[[str, Issue, "int | None"], str]
ContinuationRenderer = Callable[[Issue, int, int], str]
RoutableCheck = Callable[[Issue, "ServiceConfig"], bool]
EventSink = Callable[[str, "AgentEvent"], None]


# --------------------------------------------------------------------------
# Lazy sibling resolution
#
# Every default below imports its sibling at call time. Import-time binding
# would make this module unimportable until ~6 concurrently written modules
# land, which would in turn make it untestable.
# --------------------------------------------------------------------------


def _canonical_approval_decider(method: str, params: Mapping[str, Any]) -> Any:
    """Bridge :mod:`symphony.agent.approvals` to the app-server's decider shape.

    The two modules speak different vocabularies on purpose. ``approvals``
    models the SPEC 10.5 *posture* — a swappable policy over
    ``ApprovalKind -> ApprovalDecision`` — while ``app_server`` needs a flat
    ``(approved, reason)`` verdict it can translate to whatever the targeted
    protocol calls that field. This adapter is the seam, so a deployment can
    swap the policy (``approvals.set_approval_policy(DENY_ALL)``) without
    touching the transport.

    Without this wiring the client falls back to its own local copy of the
    posture, and a policy swap silently has no effect.
    """
    from symphony.agent.app_server import ApprovalDecision as WireDecision
    from symphony.agent.approvals import decide_approval

    request: dict[str, Any] = {"method": method}
    request.update(params)
    decision = decide_approval(request)
    return WireDecision(approved=bool(decision.is_approval), reason=str(decision.value))


def _default_app_server_factory(
    cfg: CodexConfig,
    *,
    workspace: Path,
    tool_specs: list[ToolSpec],
    tool_executor: ToolExecutor,
    on_event: Callable[[AgentEvent], None],
    secret_env_names: Sequence[str] = (),
    approval_decider: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> AppServerClientLike:
    from symphony.agent.app_server import AppServerClient

    return AppServerClient(
        cfg,
        workspace=workspace,
        tool_specs=tool_specs,
        tool_executor=tool_executor,
        on_event=on_event,
        secret_env_names=secret_env_names,
        approval_decider=approval_decider or _canonical_approval_decider,
    )


def _default_render_prompt(template: str, issue: Issue, attempt: int | None) -> str:
    from symphony.workflow.template import render_prompt

    return render_prompt(template, issue, attempt)


def _default_render_continuation_prompt(issue: Issue, turn_number: int, max_turns: int) -> str:
    from symphony.workflow.template import render_continuation_prompt

    return render_continuation_prompt(issue, turn_number, max_turns)


def _default_issue_routable(issue: Issue, cfg: ServiceConfig) -> bool:
    from symphony.orchestrator.scheduling import issue_routable

    return issue_routable(issue, cfg)


def _noop_event_sink(issue_id: str, event: AgentEvent) -> None:
    """Default orchestrator channel: discard. A runner with no channel still runs."""


class _FallbackLogger:
    """Last-resort logger used when the observability module is unavailable.

    SPEC 14.2 is explicit that log-sink problems must not crash orchestration,
    so a missing sink degrades to silence rather than an ``ImportError`` from
    inside a worker.
    """

    __slots__ = ("_fields",)

    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def debug(self, msg: str, **fields: Any) -> None: ...

    def info(self, msg: str, **fields: Any) -> None: ...

    def warning(self, msg: str, **fields: Any) -> None: ...

    def error(self, msg: str, **fields: Any) -> None: ...

    def bind(self, **fields: Any) -> _FallbackLogger:
        return _FallbackLogger(**{**self._fields, **fields})


def _default_logger() -> LoggerLike:
    try:
        from symphony.observability.logging import get_logger
    except ImportError:  # pragma: no cover - depends on sibling module presence
        return _FallbackLogger()
    return get_logger("symphony.agent.runner")


@dataclass(slots=True)
class _AttemptContext:
    """Per-attempt mutable state shared by the turn loop and the tool executor.

    The tool executor closure must see the issue as of the *latest* refresh
    (SPEC 17.5), and the turn loop rebinds it after every successful turn, so
    the two need a shared cell rather than a captured value.
    """

    issue: Issue
    turn_number: int = 1


class AgentRunner:
    """Workspace + prompt + app-server client, per attempt (SPEC 10.7).

    Behavior, in SPEC 10.7's own numbering:

    1. create/reuse the workspace for the issue;
    2. build the prompt from the workflow template;
    3. start the app-server session;
    4. forward app-server events to the orchestrator;
    5. on any error, fail the worker attempt so the orchestrator can retry.

    Workspaces are intentionally preserved after successful runs (SPEC 10.7);
    the runner never removes one.
    """

    def __init__(
        self,
        *,
        config: ServiceConfig,
        workflow: WorkflowDefinition,
        workspace_manager: WorkspaceManagerLike,
        hooks: HookRunnerLike,
        tracker: TrackerLike,
        on_event: EventSink | None = None,
        app_server_factory: AppServerFactory | None = None,
        render_prompt: PromptRenderer | None = None,
        render_continuation_prompt: ContinuationRenderer | None = None,
        issue_routable: RoutableCheck | None = None,
        logger: LoggerLike | None = None,
    ) -> None:
        self._config = config
        self._workflow = workflow
        self._workspace_manager = workspace_manager
        self._hooks = hooks
        self._tracker = tracker
        self._on_event: EventSink = on_event if on_event is not None else _noop_event_sink
        self._app_server_factory: AppServerFactory = (
            app_server_factory if app_server_factory is not None else _default_app_server_factory
        )
        self._render_prompt: PromptRenderer = (
            render_prompt if render_prompt is not None else _default_render_prompt
        )
        self._render_continuation_prompt: ContinuationRenderer = (
            render_continuation_prompt
            if render_continuation_prompt is not None
            else _default_render_continuation_prompt
        )
        self._issue_routable: RoutableCheck = (
            issue_routable if issue_routable is not None else _default_issue_routable
        )
        self._logger: LoggerLike = logger if logger is not None else _default_logger()

    # ------------------------------------------------------------------
    # SPEC 16.5 — run_agent_attempt
    # ------------------------------------------------------------------

    async def run_attempt(self, issue: Issue, attempt: int | None = None) -> None:
        """Run one worker attempt to completion (SPEC 16.5).

        Returns on ``exit_normal()``. Raises the originating error on any
        ``fail_worker(...)`` path, after performing that path's cleanup.

        ``attempt`` is ``None`` on a first run and a 1-based integer on any
        retry or continuation run (SPEC 12.3).
        """
        log = self._logger.bind(issue_id=issue.id, issue_identifier=issue.identifier)
        log.info(
            "agent attempt starting",
            outcome="started",
            attempt=attempt,
            phase=RunPhase.PREPARING_WORKSPACE.value,
        )

        # SPEC 16.5 exit 1. No workspace exists yet, so there is nothing to
        # stop and no directory in which after_run could legally be executed.
        try:
            workspace = await self._workspace_manager.create_for_issue(issue.identifier)
        except Exception as exc:
            self._log_failure(log, FAIL_WORKSPACE, exc, phase=RunPhase.PREPARING_WORKSPACE)
            raise
        workspace_path = Path(workspace.path)
        log = log.bind(workspace_path=str(workspace_path))

        # SPEC 16.5 exit 2 / SPEC 9.4: before_run failure is fatal to the run
        # attempt. It aborts *before* any session exists, and SPEC 16.5 pairs
        # no after_run with it — the run never began.
        try:
            await self._hooks.run(_BEFORE_RUN, workspace_path, fatal=True)
        except Exception as exc:
            self._log_failure(log, FAIL_BEFORE_RUN, exc, phase=RunPhase.PREPARING_WORKSPACE)
            raise

        ctx = _AttemptContext(issue=issue)

        # SPEC 16.5 exit 3. From here on before_run has succeeded, so every
        # remaining exit path owes an after_run.
        try:
            # SPEC 15.3 / 10.5: the adapter declares which environment names
            # hold tracker credentials, and the launcher removes them from the
            # child environment. The child receives tool *results*, never a raw
            # token. This is the only place the two halves meet -- the adapter
            # cannot strip what it does not launch, and the client cannot know
            # which names matter without being told.
            client = self._app_server_factory(
                self._config.codex,
                workspace=workspace_path,
                tool_specs=list(self._tracker.agent_tool_specs()),
                tool_executor=self._make_tool_executor(ctx),
                on_event=self._make_event_sink(issue.id, log),
                secret_env_names=tuple(self._tracker.secret_environment_names()),
                approval_decider=_canonical_approval_decider,
            )
            session = await client.start_session()
        except BaseException as exc:
            self._log_failure(log, FAIL_SESSION_STARTUP, exc, phase=RunPhase.INITIALIZING_SESSION)
            await self._run_after_run(workspace_path, log)
            raise

        # SPEC 13.1 wants `session_id` on session-lifecycle logs, but SPEC 4.2
        # defines it as "<thread_id>-<turn_id>" and only the app-server client
        # sees `turn_id`. The runner logs the half it actually knows rather
        # than emitting a field that would be wrong.
        session_log = log.bind(thread_id=getattr(session, "thread_id", None))

        # SPEC 16.5 exits 4-6 plus the four normal breaks. Wrapping the whole
        # loop is deliberate: it makes it structurally impossible for a new
        # loop exit to skip the stop-session/after_run pair.
        try:
            reason = await self._run_turn_loop(
                client=client, session=session, ctx=ctx, attempt=attempt, log=session_log
            )
        except BaseException:
            await self._stop_session(session, session_log)
            await self._run_after_run(workspace_path, session_log)
            raise

        await self._stop_session(session, session_log)
        await self._run_after_run(workspace_path, session_log)
        session_log.info(
            "agent attempt completed",
            outcome="completed",
            reason=reason,
            turns=ctx.turn_number,
            phase=RunPhase.SUCCEEDED.value,
        )

    # ------------------------------------------------------------------
    # In-worker continuation turn loop (SPEC 7.1, 10.3, 16.5)
    # ------------------------------------------------------------------

    async def _run_turn_loop(
        self,
        *,
        client: AppServerClientLike,
        session: AppServerSessionLike,
        ctx: _AttemptContext,
        attempt: int | None,
        log: LoggerLike,
    ) -> str:
        """Drive back-to-back turns on one live thread; return the break reason.

        Raises on any of the three SPEC 16.5 in-loop ``fail_worker`` paths. The
        caller owns the stop-session/after_run pair for both outcomes.
        """
        # Read once: a mid-run config reload must not change the bound of a
        # loop that is already executing (SPEC 6.2 applies to later dispatch).
        max_turns = self._config.max_turns

        while True:
            turn_log = log.bind(turn=ctx.turn_number)

            # SPEC 16.5 exit 4 / SPEC 12.4: prompt failure fails the attempt.
            try:
                prompt = self._build_turn_prompt(ctx.issue, attempt, ctx.turn_number, max_turns)
            except Exception as exc:
                self._log_failure(turn_log, FAIL_PROMPT, exc, phase=RunPhase.BUILDING_PROMPT)
                raise

            # SPEC 16.5 exit 5. The subprocess stays alive across continuation
            # turns; only the worker's end stops it (SPEC 10.3).
            try:
                await client.run_turn(session, prompt, title=turn_title(ctx.issue))
            except Exception as exc:
                self._log_failure(turn_log, FAIL_TURN, exc, phase=RunPhase.STREAMING_TURN)
                raise

            # SPEC 16.5 exit 6. A *failed* refresh fails the attempt; an empty
            # refresh does not (see below).
            try:
                refreshed = await self._tracker.fetch_issues_by_ids([ctx.issue.id])
            except Exception as exc:
                self._log_failure(turn_log, FAIL_ISSUE_REFRESH, exc, phase=RunPhase.STREAMING_TURN)
                raise

            # Break 1. The issue merely became invisible to the adapter — that
            # is not a worker failure, so the attempt still exits normally.
            if not refreshed:
                return self._log_break(turn_log, BREAK_ISSUE_MISSING)

            ctx.issue = refreshed[0]

            # Break 2. SPEC 16.5 "issue.state is not active"; SPEC 8.2 defines
            # a schedulable state as active *and* not terminal, so a state that
            # is configured as both ends the loop.
            state = ctx.issue.state
            if not self._config.is_active(state) or self._config.is_terminal(state):
                return self._log_break(turn_log, BREAK_STATE_INACTIVE, state=state)

            # Break 3. SPEC 8.2: for continuation checks, routable means only
            # dispatchable plus required labels.
            if not self._issue_routable(ctx.issue, self._config):
                return self._log_break(turn_log, BREAK_NOT_ROUTABLE)

            # Break 4. Compared *after* a turn has run, so max_turns <= 1 still
            # yields exactly one turn (SPEC 16.5 ordering).
            if ctx.turn_number >= max_turns:
                return self._log_break(turn_log, BREAK_MAX_TURNS, max_turns=max_turns)

            ctx.turn_number += 1

    def _build_turn_prompt(
        self, issue: Issue, attempt: int | None, turn_number: int, max_turns: int
    ) -> str:
        """SPEC 16.5 ``build_turn_prompt``, split per CONTRACTS.md.

        The first turn sends the fully rendered task prompt; continuation turns
        send only continuation guidance, because the task prompt is already in
        the live thread's history (SPEC 7.1, 10.2).
        """
        if turn_number <= 1:
            return self._render_prompt(self._workflow.prompt_template, issue, attempt)
        return self._render_continuation_prompt(issue, turn_number, max_turns)

    # ------------------------------------------------------------------
    # Collaborator plumbing
    # ------------------------------------------------------------------

    def _make_tool_executor(self, ctx: _AttemptContext) -> ToolExecutor:
        """Bind host-side tool execution to the current issue (SPEC 10.5, 17.5)."""

        async def execute(name: str, arguments: dict[str, Any]) -> ToolResult:
            return await self._tracker.execute_agent_tool(
                name, arguments, ToolContext(issue=ctx.issue)
            )

        return execute

    def _make_event_sink(self, issue_id: str, log: LoggerLike) -> Callable[[AgentEvent], None]:
        """SPEC 16.5 ``on_message`` -> ``{codex_update, issue.id, msg}`` (SPEC 10.7.4).

        A failing observer must not take down the run (SPEC 14.2, 17.6).
        """

        def sink(event: AgentEvent) -> None:
            try:
                self._on_event(issue_id, event)
            except Exception as exc:  # observer isolation is the point
                log.warning(
                    "agent event forwarding failed",
                    outcome="ignored",
                    error=str(exc),
                    event=getattr(event, "event", None),
                )

        return sink

    async def _stop_session(self, session: AppServerSessionLike, log: LoggerLike) -> None:
        """SPEC 16.5 ``app_server.stop_session``; never masks the real failure."""
        try:
            await session.stop()
        except Exception as exc:  # cleanup must never replace the real cause
            log.warning("agent session stop failed", outcome="ignored", error=str(exc))

    async def _run_after_run(self, workspace_path: Path, log: LoggerLike) -> None:
        """SPEC 16.5 ``run_hook_best_effort("after_run")``.

        SPEC 9.4 already makes ``after_run`` non-fatal, so ``fatal=False`` is
        the contract-level guarantee and this ``except`` is the belt to its
        braces: a hook runner that raises anyway must not turn a normal exit
        into a worker failure.
        """
        try:
            await self._hooks.run(_AFTER_RUN, workspace_path, fatal=False)
        except Exception as exc:  # SPEC 9.4: logged and ignored
            log.warning("after_run hook failed", outcome="ignored", error=str(exc))

    # ------------------------------------------------------------------
    # Logging (SPEC 13.1: stable key=value, outcome, concise reason)
    # ------------------------------------------------------------------

    def _log_failure(
        self, log: LoggerLike, reason: str, exc: BaseException, *, phase: RunPhase, **fields: Any
    ) -> None:
        log.error(
            "agent attempt failed",
            outcome="failed",
            reason=reason,
            phase=phase.value,
            error_category=getattr(exc, "category", type(exc).__name__),
            error=str(exc),
            **fields,
        )

    def _log_break(self, log: LoggerLike, reason: str, **fields: Any) -> str:
        log.info("turn loop ended", outcome="completed", reason=reason, **fields)
        return reason
