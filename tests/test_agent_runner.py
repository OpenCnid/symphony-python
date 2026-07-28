"""Tests for ``symphony.agent.runner`` — SPEC 10.7 and the SPEC 16.5 algorithm.

Every collaborator is faked: the workspace manager, the hook runner, the
workflow template renderers, the app-server client, ``issue_routable``, and the
tracker adapter. None of the real sibling modules exist yet, and the point of
these tests is the runner's own ordering, not theirs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from symphony.agent.runner import (
    BREAK_ISSUE_MISSING,
    BREAK_MAX_TURNS,
    BREAK_NOT_ROUTABLE,
    BREAK_STATE_INACTIVE,
    EXIT_PATHS,
    FAIL_BEFORE_RUN,
    FAIL_ISSUE_REFRESH,
    FAIL_PROMPT,
    FAIL_SESSION_STARTUP,
    FAIL_TURN,
    FAIL_WORKSPACE,
    AgentRunner,
    turn_title,
)
from symphony.errors import (
    CodexNotFound,
    HookTimeout,
    TemplateRenderError,
    TrackerRequestError,
    TurnFailed,
    WorkspaceCreationError,
)
from symphony.models import Issue, WorkflowDefinition, Workspace, normalize_state, workspace_key
from symphony.trackers.base import ToolResult, ToolSpec

TEMPLATE = "Work on {{ issue.identifier }}."
TOOL_SPECS = [
    ToolSpec(
        name="add_comment", description="c", input_schema={"type": "object"}, mutates_tracker=True
    )
]


def make_issue(
    *,
    id: str = "issue-1",
    identifier: str = "ENG-42",
    title: str = "Original",
    state: str = "In Progress",
    dispatchable: bool = True,
    labels: tuple[str, ...] = ("agent",),
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title=title,
        state=state,
        dispatchable=dispatchable,
        labels=labels,
    )


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class FakeConfig:
    """Stand-in for ``symphony.workflow.config.ServiceConfig``."""

    max_turns: int = 1
    active_states: frozenset[str] = frozenset({"in progress"})
    terminal_states: frozenset[str] = frozenset({"done"})
    codex: Any = field(default_factory=lambda: SimpleNamespace(command="codex app-server"))

    def is_active(self, state: str) -> bool:
        return normalize_state(state) in self.active_states

    def is_terminal(self, state: str) -> bool:
        return normalize_state(state) in self.terminal_states


class FakeWorkspaceManager:
    def __init__(self, trace: list[str], path: Path, error: BaseException | None = None) -> None:
        self.trace = trace
        self.path = path
        self.error = error
        self.calls: list[str] = []

    async def create_for_issue(self, identifier: str) -> Workspace:
        self.calls.append(identifier)
        self.trace.append("workspace")
        if self.error is not None:
            raise self.error
        return Workspace(
            path=str(self.path), workspace_key=workspace_key(identifier), created_now=True
        )


class FakeHooks:
    def __init__(self, trace: list[str], failures: dict[str, BaseException] | None = None) -> None:
        self.trace = trace
        self.failures = failures or {}
        self.calls: list[tuple[str, Path, bool]] = []

    async def run(self, name: str, cwd: Path, *, fatal: bool) -> None:
        self.calls.append((name, Path(cwd), fatal))
        self.trace.append(f"hook:{name}")
        exc = self.failures.get(name)
        if exc is not None:
            raise exc

    @property
    def names(self) -> list[str]:
        return [name for name, _cwd, _fatal in self.calls]


class FakeSession:
    def __init__(self, trace: list[str], stop_error: BaseException | None = None) -> None:
        self.thread_id = "thread-1"
        self.trace = trace
        self.stop_error = stop_error
        self.stops = 0

    async def stop(self) -> None:
        self.stops += 1
        self.trace.append("stop_session")
        if self.stop_error is not None:
            raise self.stop_error


class FakeAppServerClient:
    def __init__(self, harness: Harness, cfg: Any, **kwargs: Any) -> None:
        self.harness = harness
        self.cfg = cfg
        self.workspace: Path = kwargs["workspace"]
        self.tool_specs: list[ToolSpec] = kwargs["tool_specs"]
        self.tool_executor = kwargs["tool_executor"]
        self.on_event = kwargs["on_event"]
        self.turns: list[tuple[str, str | None, Any]] = []
        self.session: FakeSession | None = None

    async def start_session(self) -> FakeSession:
        self.harness.trace.append("start_session")
        if self.harness.start_error is not None:
            raise self.harness.start_error
        self.session = FakeSession(self.harness.trace, self.harness.stop_error)
        self.harness.session = self.session
        return self.session

    async def run_turn(self, session: Any, prompt: str, *, title: str | None = None) -> None:
        n = len(self.turns) + 1
        self.turns.append((prompt, title, session))
        self.harness.trace.append(f"turn:{n}")
        for event in self.harness.events_per_turn.get(n, ()):
            self.on_event(event)
        hook = self.harness.on_turn.get(n)
        if hook is not None:
            await hook(self)
        exc = self.harness.turn_errors.get(n)
        if exc is not None:
            raise exc


class FakeTracker:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.refresh_calls: list[list[str]] = []
        self.tool_calls: list[tuple[str, dict[str, Any], Any]] = []

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        self.refresh_calls.append(list(issue_ids))
        index = len(self.refresh_calls) - 1
        scripted = self.harness.refresh
        result = scripted[index] if index < len(scripted) else self.harness.refresh_default
        if isinstance(result, BaseException):
            raise result
        return list(result)

    def agent_tool_specs(self) -> list[ToolSpec]:
        return list(self.harness.tool_specs)

    async def execute_agent_tool(
        self, name: str, arguments: dict[str, Any], context: Any
    ) -> ToolResult:
        self.tool_calls.append((name, arguments, context))
        return ToolResult.success({"ok": True})


class RecordingLogger:
    def __init__(self, records: list[dict[str, Any]] | None = None, **bound: Any) -> None:
        self.records = records if records is not None else []
        self.bound = bound

    def _emit(self, level: str, msg: str, fields: dict[str, Any]) -> None:
        self.records.append({"level": level, "msg": msg, **self.bound, **fields})

    def debug(self, msg: str, **fields: Any) -> None:
        self._emit("debug", msg, fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._emit("info", msg, fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._emit("warning", msg, fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._emit("error", msg, fields)

    def bind(self, **fields: Any) -> RecordingLogger:
        return RecordingLogger(self.records, **{**self.bound, **fields})


@dataclass
class Harness:
    """Assembles an :class:`AgentRunner` whose every collaborator is a fake."""

    workspace_path: Path
    issue: Issue = field(default_factory=make_issue)
    config: FakeConfig = field(default_factory=FakeConfig)
    workspace_error: BaseException | None = None
    hook_failures: dict[str, BaseException] | None = None
    start_error: BaseException | None = None
    stop_error: BaseException | None = None
    turn_errors: dict[int, BaseException] = field(default_factory=dict)
    prompt_errors: dict[int, BaseException] = field(default_factory=dict)
    refresh: list[list[Issue] | BaseException] = field(default_factory=list)
    refresh_default: list[Issue] | BaseException | None = None
    routable: bool = True
    on_turn: dict[int, Any] = field(default_factory=dict)
    events_per_turn: dict[int, tuple[Any, ...]] = field(default_factory=dict)
    tool_specs: list[ToolSpec] = field(default_factory=lambda: list(TOOL_SPECS))
    on_event: Any = None

    def __post_init__(self) -> None:
        self.trace: list[str] = []
        self.forwarded: list[tuple[str, Any]] = []
        self.session: FakeSession | None = None
        self.clients: list[FakeAppServerClient] = []
        self.prompt_calls: list[tuple[str, Issue, int | None]] = []
        self.continuation_calls: list[tuple[Issue, int, int]] = []
        self.routable_calls: list[tuple[Issue, Any]] = []
        if self.refresh_default is None:
            self.refresh_default = [self.issue]
        self.workspace_manager = FakeWorkspaceManager(
            self.trace, self.workspace_path, self.workspace_error
        )
        self.hooks = FakeHooks(self.trace, self.hook_failures)
        self.tracker = FakeTracker(self)
        self.logger = RecordingLogger()
        self.runner = AgentRunner(
            config=self.config,
            workflow=WorkflowDefinition(config={}, prompt_template=TEMPLATE, source_path=None),
            workspace_manager=self.workspace_manager,
            hooks=self.hooks,
            tracker=self.tracker,
            on_event=self.on_event if self.on_event is not None else self._record_event,
            app_server_factory=self._factory,
            render_prompt=self._render_prompt,
            render_continuation_prompt=self._render_continuation_prompt,
            issue_routable=self._issue_routable,
            logger=self.logger,
        )

    # -- injected collaborators -------------------------------------------

    def _factory(self, cfg: Any, **kwargs: Any) -> FakeAppServerClient:
        client = FakeAppServerClient(self, cfg, **kwargs)
        self.clients.append(client)
        return client

    def _render_prompt(self, template: str, issue: Issue, attempt: int | None) -> str:
        self.prompt_calls.append((template, issue, attempt))
        exc = self.prompt_errors.get(1)
        if exc is not None:
            raise exc
        return f"TASK|{template}|{issue.identifier}|{issue.title}|attempt={attempt}"

    def _render_continuation_prompt(self, issue: Issue, turn_number: int, max_turns: int) -> str:
        self.continuation_calls.append((issue, turn_number, max_turns))
        exc = self.prompt_errors.get(turn_number)
        if exc is not None:
            raise exc
        return f"CONT|{issue.identifier}|{issue.title}|turn={turn_number}|max={max_turns}"

    def _issue_routable(self, issue: Issue, cfg: Any) -> bool:
        self.routable_calls.append((issue, cfg))
        return self.routable

    def _record_event(self, issue_id: str, event: Any) -> None:
        self.forwarded.append((issue_id, event))

    # -- assertions helpers ------------------------------------------------

    async def run(self, attempt: int | None = None) -> None:
        await self.runner.run_attempt(self.issue, attempt)

    @property
    def client(self) -> FakeAppServerClient:
        return self.clients[0]

    @property
    def prompts(self) -> list[str]:
        return [p for p, _t, _s in self.client.turns]

    @property
    def stops(self) -> int:
        return self.session.stops if self.session is not None else 0

    def reasons(self) -> list[str]:
        return [r["reason"] for r in self.logger.records if "reason" in r]

    @property
    def break_reason(self) -> str | None:
        ended = [r for r in self.logger.records if r["msg"] == "turn loop ended"]
        assert len(ended) <= 1, "the turn loop must end exactly once"
        return ended[0]["reason"] if ended else None

    @property
    def completion_reason(self) -> str | None:
        done = [r for r in self.logger.records if r["msg"] == "agent attempt completed"]
        return done[0]["reason"] if done else None


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    path = tmp_path / "ENG-42"
    path.mkdir()
    return path


# --------------------------------------------------------------------------
# Happy path and ordering (SPEC 16.5)
# --------------------------------------------------------------------------


async def test_single_turn_runs_the_full_ordered_pipeline(ws: Path) -> None:
    h = Harness(workspace_path=ws)

    await h.run()

    assert h.trace == [
        "workspace",
        "hook:before_run",
        "start_session",
        "turn:1",
        "stop_session",
        "hook:after_run",
    ]


async def test_before_run_is_fatal_and_after_run_is_not(ws: Path) -> None:
    h = Harness(workspace_path=ws)

    await h.run()

    assert h.hooks.calls == [("before_run", ws, True), ("after_run", ws, False)]


async def test_first_turn_sends_the_rendered_task_prompt(ws: Path) -> None:
    h = Harness(workspace_path=ws)

    await h.run(attempt=3)

    assert h.prompt_calls == [(TEMPLATE, h.issue, 3)]
    assert h.continuation_calls == []
    assert h.prompts == ["TASK|Work on {{ issue.identifier }}.|ENG-42|Original|attempt=3"]


async def test_turn_title_carries_issue_identity(ws: Path) -> None:
    h = Harness(workspace_path=ws)

    await h.run()

    assert h.client.turns[0][1] == "ENG-42: Original"
    assert turn_title(h.issue) == "ENG-42: Original"


async def test_app_server_client_is_built_with_workspace_and_tracker_tools(ws: Path) -> None:
    h = Harness(workspace_path=ws)

    await h.run()

    assert h.client.cfg is h.config.codex
    assert h.client.workspace == ws
    assert h.client.tool_specs == TOOL_SPECS


# --------------------------------------------------------------------------
# Exit paths (SPEC 16.5 cleanup ordering)
# --------------------------------------------------------------------------


def _harness_for_reason(reason: str, ws: Path) -> tuple[Harness, type[Exception] | None]:
    """One harness per SPEC 16.5 exit point, keyed by its reason string.

    Returns the harness plus the error the attempt is expected to raise, or
    ``None`` for the four exits that end the loop normally.
    """
    if reason == FAIL_WORKSPACE:
        h = Harness(workspace_path=ws, workspace_error=WorkspaceCreationError("mkdir failed"))
        return h, WorkspaceCreationError
    if reason == FAIL_BEFORE_RUN:
        h = Harness(workspace_path=ws, hook_failures={"before_run": HookTimeout("slow")})
        return h, HookTimeout
    if reason == FAIL_SESSION_STARTUP:
        return Harness(workspace_path=ws, start_error=CodexNotFound("no codex")), CodexNotFound
    if reason == FAIL_PROMPT:
        h = Harness(workspace_path=ws, prompt_errors={1: TemplateRenderError("unknown var")})
        return h, TemplateRenderError
    if reason == FAIL_TURN:
        return Harness(workspace_path=ws, turn_errors={1: TurnFailed("boom")}), TurnFailed
    if reason == FAIL_ISSUE_REFRESH:
        h = Harness(workspace_path=ws, refresh=[TrackerRequestError("connection reset")])
        return h, TrackerRequestError
    if reason == BREAK_ISSUE_MISSING:
        return Harness(workspace_path=ws, refresh=[[]]), None
    if reason == BREAK_STATE_INACTIVE:
        h = Harness(
            workspace_path=ws,
            config=FakeConfig(max_turns=5),
            refresh=[[make_issue(state="Backlog")]],
        )
        return h, None
    if reason == BREAK_NOT_ROUTABLE:
        return Harness(workspace_path=ws, config=FakeConfig(max_turns=5), routable=False), None
    if reason == BREAK_MAX_TURNS:
        return Harness(workspace_path=ws), None
    raise AssertionError(f"unmapped exit reason: {reason}")


@pytest.mark.parametrize("reason", list(EXIT_PATHS))
async def test_every_exit_path_matches_the_declared_cleanup_pair(reason: str, ws: Path) -> None:
    """SPEC 16.5: each exit stops the session and/or runs after_run, or neither."""
    expect_stop, expect_after_run = EXIT_PATHS[reason]
    h, expected_error = _harness_for_reason(reason, ws)

    if expected_error is not None:
        with pytest.raises(expected_error):
            await h.run()
    else:
        await h.run()

    assert h.stops == (1 if expect_stop else 0)
    assert ("after_run" in h.hooks.names) is expect_after_run
    assert reason in h.reasons()


async def test_workspace_failure_never_starts_a_session_or_runs_any_hook(ws: Path) -> None:
    h = Harness(workspace_path=ws, workspace_error=WorkspaceCreationError("mkdir failed"))

    with pytest.raises(WorkspaceCreationError):
        await h.run()

    assert h.trace == ["workspace"]
    assert h.hooks.calls == []
    assert h.clients == []


async def test_before_run_failure_aborts_before_any_session_exists(ws: Path) -> None:
    h = Harness(workspace_path=ws, hook_failures={"before_run": HookTimeout("slow")})

    with pytest.raises(HookTimeout):
        await h.run()

    assert h.trace == ["workspace", "hook:before_run"]
    assert h.clients == []
    assert h.session is None
    assert "after_run" not in h.hooks.names


async def test_session_startup_failure_runs_after_run_without_stopping(ws: Path) -> None:
    h = Harness(workspace_path=ws, start_error=CodexNotFound("no codex"))

    with pytest.raises(CodexNotFound):
        await h.run()

    assert h.trace == ["workspace", "hook:before_run", "start_session", "hook:after_run"]
    assert h.session is None


async def test_continuation_prompt_failure_still_stops_and_runs_after_run(ws: Path) -> None:
    h = Harness(
        workspace_path=ws,
        config=FakeConfig(max_turns=5),
        prompt_errors={2: TemplateRenderError("bad continuation")},
    )

    with pytest.raises(TemplateRenderError):
        await h.run()

    assert h.trace == [
        "workspace",
        "hook:before_run",
        "start_session",
        "turn:1",
        "stop_session",
        "hook:after_run",
    ]


async def test_failure_on_a_later_turn_stops_and_runs_after_run(ws: Path) -> None:
    h = Harness(
        workspace_path=ws, config=FakeConfig(max_turns=5), turn_errors={3: TurnFailed("boom")}
    )

    with pytest.raises(TurnFailed):
        await h.run()

    assert len(h.client.turns) == 3
    assert h.stops == 1
    assert h.hooks.names == ["before_run", "after_run"]


async def test_failure_reraises_the_original_error_with_its_category(ws: Path) -> None:
    original = TurnFailed("agent gave up", turn=1)
    h = Harness(workspace_path=ws, turn_errors={1: original})

    with pytest.raises(TurnFailed) as caught:
        await h.run()

    assert caught.value is original
    failure = next(r for r in h.logger.records if r["level"] == "error")
    assert failure["reason"] == FAIL_TURN
    assert failure["error_category"] == "turn_failed"
    assert failure["issue_id"] == "issue-1"
    assert failure["issue_identifier"] == "ENG-42"


async def test_session_lifecycle_logs_carry_issue_and_thread_context(ws: Path) -> None:
    """SPEC 13.1 context fields; SPEC 4.2 keeps full session_id off this layer."""
    h = Harness(workspace_path=ws)

    await h.run()

    done = next(r for r in h.logger.records if r["msg"] == "agent attempt completed")
    assert done["issue_id"] == "issue-1"
    assert done["issue_identifier"] == "ENG-42"
    assert done["thread_id"] == "thread-1"
    assert done["outcome"] == "completed"
    assert "session_id" not in done


async def test_cancellation_still_stops_the_session_and_runs_after_run(ws: Path) -> None:
    """A reconciliation cancel (SPEC 8.5) must not leak the subprocess."""
    started = asyncio.Event()

    async def block(_client: FakeAppServerClient) -> None:
        started.set()
        await asyncio.Event().wait()

    h = Harness(workspace_path=ws, on_turn={1: block})
    task = asyncio.create_task(h.runner.run_attempt(h.issue, None))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert h.stops == 1
    assert h.hooks.names == ["before_run", "after_run"]


# --------------------------------------------------------------------------
# Continuation turn loop (SPEC 7.1, 10.3, 16.5)
# --------------------------------------------------------------------------


async def test_continuation_turns_send_only_continuation_guidance(ws: Path) -> None:
    h = Harness(workspace_path=ws, config=FakeConfig(max_turns=3))

    await h.run()

    assert h.prompts == [
        "TASK|Work on {{ issue.identifier }}.|ENG-42|Original|attempt=None",
        "CONT|ENG-42|Original|turn=2|max=3",
        "CONT|ENG-42|Original|turn=3|max=3",
    ]
    assert len(h.prompt_calls) == 1


async def test_continuation_turns_reuse_the_same_live_session(ws: Path) -> None:
    h = Harness(workspace_path=ws, config=FakeConfig(max_turns=3))

    await h.run()

    sessions = {id(session) for _p, _t, session in h.client.turns}
    assert len(sessions) == 1
    assert len(h.clients) == 1
    assert h.stops == 1
    assert h.trace.count("stop_session") == 1


async def test_loop_stops_at_max_turns(ws: Path) -> None:
    h = Harness(workspace_path=ws, config=FakeConfig(max_turns=4))

    await h.run()

    assert len(h.client.turns) == 4
    assert h.break_reason == BREAK_MAX_TURNS
    assert h.completion_reason == BREAK_MAX_TURNS


async def test_max_turns_below_one_still_runs_exactly_one_turn(ws: Path) -> None:
    """SPEC 16.5 compares turn_number *after* a turn has already run."""
    h = Harness(workspace_path=ws, config=FakeConfig(max_turns=0))

    await h.run()

    assert len(h.client.turns) == 1
    assert h.break_reason == BREAK_MAX_TURNS


async def test_empty_refresh_breaks_without_failing_the_attempt(ws: Path) -> None:
    h = Harness(workspace_path=ws, config=FakeConfig(max_turns=5), refresh=[[]])

    await h.run()

    assert len(h.client.turns) == 1
    assert h.break_reason == BREAK_ISSUE_MISSING
    assert h.tracker.refresh_calls == [["issue-1"]]


async def test_failed_refresh_fails_the_attempt(ws: Path) -> None:
    boom = TrackerRequestError("connection reset")
    h = Harness(workspace_path=ws, config=FakeConfig(max_turns=5), refresh=[boom])

    with pytest.raises(TrackerRequestError) as caught:
        await h.run()

    assert caught.value is boom
    assert FAIL_ISSUE_REFRESH in h.reasons()


async def test_inactive_state_breaks_the_loop(ws: Path) -> None:
    h = Harness(
        workspace_path=ws,
        config=FakeConfig(max_turns=5),
        refresh=[[make_issue(state="Backlog")]],
    )

    await h.run()

    assert len(h.client.turns) == 1
    assert h.break_reason == BREAK_STATE_INACTIVE


async def test_terminal_state_breaks_even_when_also_listed_active(ws: Path) -> None:
    """SPEC 8.2 defines schedulable as active *and* not terminal."""
    config = FakeConfig(
        max_turns=5,
        active_states=frozenset({"in progress", "done"}),
        terminal_states=frozenset({"done"}),
    )
    h = Harness(workspace_path=ws, config=config, refresh=[[make_issue(state="Done")]])

    await h.run()

    assert len(h.client.turns) == 1
    assert h.break_reason == BREAK_STATE_INACTIVE


async def test_non_routable_issue_breaks_the_loop(ws: Path) -> None:
    h = Harness(workspace_path=ws, config=FakeConfig(max_turns=5), routable=False)

    await h.run()

    assert len(h.client.turns) == 1
    assert h.break_reason == BREAK_NOT_ROUTABLE
    assert h.routable_calls[0][1] is h.config


async def test_refreshed_issue_drives_later_turns(ws: Path) -> None:
    renamed = make_issue(title="Refreshed")
    h = Harness(workspace_path=ws, config=FakeConfig(max_turns=2), refresh=[[renamed]])

    await h.run()

    assert h.continuation_calls == [(renamed, 2, 2)]
    assert h.client.turns[1][0] == "CONT|ENG-42|Refreshed|turn=2|max=2"
    assert h.client.turns[1][1] == "ENG-42: Refreshed"


# --------------------------------------------------------------------------
# Event forwarding and tool context (SPEC 10.5, 10.7, 17.5)
# --------------------------------------------------------------------------


async def test_agent_events_are_forwarded_tagged_with_the_issue_id(ws: Path) -> None:
    event = SimpleNamespace(event="turn_completed")
    h = Harness(workspace_path=ws, events_per_turn={1: (event,)})

    await h.run()

    assert h.forwarded == [("issue-1", event)]


async def test_a_failing_event_sink_does_not_break_the_run(ws: Path) -> None:
    def explode(_issue_id: str, _event: Any) -> None:
        raise RuntimeError("observer down")

    h = Harness(
        workspace_path=ws,
        on_event=explode,
        events_per_turn={1: (SimpleNamespace(event="notification"),)},
    )

    await h.run()

    assert h.trace[-1] == "hook:after_run"
    assert any(r["msg"] == "agent event forwarding failed" for r in h.logger.records)


async def test_tool_execution_carries_the_current_issue_as_context(ws: Path) -> None:
    async def call_tool(client: FakeAppServerClient) -> None:
        await client.tool_executor("add_comment", {"body": "hi"})

    renamed = make_issue(title="Refreshed")
    h = Harness(
        workspace_path=ws,
        config=FakeConfig(max_turns=2),
        refresh=[[renamed]],
        on_turn={1: call_tool, 2: call_tool},
    )

    await h.run()

    assert [name for name, _args, _ctx in h.tracker.tool_calls] == ["add_comment", "add_comment"]
    assert h.tracker.tool_calls[0][2].issue.title == "Original"
    assert h.tracker.tool_calls[1][2].issue.title == "Refreshed"
    assert h.tracker.tool_calls[0][1] == {"body": "hi"}


# --------------------------------------------------------------------------
# Best-effort cleanup never masks the outcome (SPEC 9.4, 14.2)
# --------------------------------------------------------------------------


async def test_after_run_failure_does_not_fail_a_successful_attempt(ws: Path) -> None:
    h = Harness(workspace_path=ws, hook_failures={"after_run": HookTimeout("hung")})

    await h.run()

    assert any(r["msg"] == "after_run hook failed" for r in h.logger.records)


async def test_after_run_failure_does_not_mask_the_original_error(ws: Path) -> None:
    h = Harness(
        workspace_path=ws,
        turn_errors={1: TurnFailed("real cause")},
        hook_failures={"after_run": HookTimeout("hung")},
    )

    with pytest.raises(TurnFailed, match="real cause"):
        await h.run()


async def test_session_stop_failure_does_not_mask_the_original_error(ws: Path) -> None:
    h = Harness(
        workspace_path=ws,
        turn_errors={1: TurnFailed("real cause")},
        stop_error=RuntimeError("stop broke"),
    )

    with pytest.raises(TurnFailed, match="real cause"):
        await h.run()

    assert h.hooks.names == ["before_run", "after_run"]


async def test_session_stop_failure_does_not_fail_a_successful_attempt(ws: Path) -> None:
    h = Harness(workspace_path=ws, stop_error=RuntimeError("stop broke"))

    await h.run()

    assert any(r["msg"] == "agent session stop failed" for r in h.logger.records)
    assert h.hooks.names == ["before_run", "after_run"]


async def test_runner_never_removes_the_workspace(ws: Path) -> None:
    """SPEC 10.7: workspaces are intentionally preserved after successful runs."""
    marker = ws / "artifact.txt"
    marker.write_text("kept", encoding="utf-8")

    h = Harness(workspace_path=ws)
    await h.run()

    assert marker.read_text(encoding="utf-8") == "kept"
    assert "before_remove" not in h.hooks.names
