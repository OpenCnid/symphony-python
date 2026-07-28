"""Conformance tests for ``symphony.cli`` (SPEC 16.1, 17.7, and 5.1/6.2/6.3/8.6/13.7).

Every collaborator the host wires together — ``workflow.loader``,
``workflow.config``, ``workflow.watcher``, ``orchestrator.core``,
``trackers.base.build_adapter``, ``observability``, ``http.server`` — is written
by a different author and is faked here through :class:`symphony.cli.HostDeps`.
Nothing in this file touches the network, a subprocess (except the one explicit
entry-point smoke test), or a wall-clock sleep.

The suite is organized around the six SPEC 17.7 bullets, then the SPEC 16.1
ordering and the fatal-vs-warned asymmetry that ordering exists to express.
"""

from __future__ import annotations

import asyncio
import importlib
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from symphony import cli
from symphony.errors import ConfigValidationError, MissingWorkflowFile, TrackerRequestError
from symphony.models import OrchestratorState

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

WORKFLOW_TEXT = """---
tracker:
  kind: memory
codex:
  command: codex app-server
---

Fix {{ issue.identifier }}.
"""


@dataclass
class FakeConfig:
    """Stand-in for ``workflow.config.ServiceConfig`` (only the fields the host reads)."""

    poll_interval_ms: int = 5_000
    max_concurrent_agents: int = 3
    tracker_kind: str = "memory"
    tracker_provider: dict[str, Any] = field(default_factory=dict)
    server_port: int | None = None
    tag: str = "initial"


class FakeLogger:
    """Records structured calls so tests can assert on operator-visible output."""

    def __init__(self, records: list[tuple[str, str, dict[str, Any]]]) -> None:
        self.records = records

    def _emit(self, level: str, msg: str, fields: dict[str, Any]) -> None:
        self.records.append((level, msg, fields))

    def info(self, msg: str, **fields: Any) -> None:
        self._emit("info", msg, fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._emit("warning", msg, fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._emit("error", msg, fields)

    def debug(self, msg: str, **fields: Any) -> None:
        self._emit("debug", msg, fields)

    def bind(self, **_fields: Any) -> FakeLogger:
        return self


class FakeWatcher:
    """Stand-in for ``workflow.watcher.WorkflowWatcher``."""

    def __init__(self, rec: Recorder, path: Path, on_change: Any) -> None:
        self.rec = rec
        self.path = path
        self.on_change = on_change
        self.started = False
        self.stopped = False
        self.stop_error: Exception | None = None

    async def start(self) -> None:
        self.started = True
        self.rec.steps.append("start_watch")

    async def stop(self) -> None:
        self.stopped = True
        self.rec.steps.append("stop_watch")
        if self.stop_error is not None:
            raise self.stop_error


class FakeObservability:
    def __init__(self, rec: Recorder, port: int | None) -> None:
        self.rec = rec
        self.port = port
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        self.rec.steps.append("stop_observability")


class FakeTracker:
    def __init__(self, rec: Recorder, kind: str, provider: dict[str, Any]) -> None:
        self.rec = rec
        self.kind = kind
        self.provider = provider
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        self.rec.steps.append("close_tracker")


class FakeOrchestrator:
    """Stand-in for ``orchestrator.core.Orchestrator`` — the lifecycle the host uses.

    Mirrors the real module: ``start()`` performs the SPEC 16.1 tail (preflight,
    SPEC 8.6 terminal cleanup, first tick), ``run_forever()`` awaits the loop,
    ``stop()`` unwinds it, and ``apply_config()`` re-applies a reload.
    """

    def __init__(self, rec: Recorder, config: Any, workflow: Any, tracker: Any) -> None:
        self.rec = rec
        self.config = config
        self.workflow = workflow
        self.tracker = tracker
        self.state = OrchestratorState(
            poll_interval_ms=config.poll_interval_ms,
            max_concurrent_agents=config.max_concurrent_agents,
        )

        self.start_error: BaseException | None = None
        self.run_error: BaseException | None = None
        self.run_returns_unasked = False
        self.stop_error: BaseException | None = None
        self.ignore_stop = False
        self.on_run: Any = None

        self.started = False
        self.stop_called = False
        self.reloaded: list[Any] = []
        self.run_started = asyncio.Event()
        self._release = asyncio.Event()

    async def start(self) -> None:
        self.rec.steps.append("orchestrator_start")
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    async def run_forever(self) -> None:
        self.rec.steps.append("run")
        self.run_started.set()
        if self.run_error is not None:
            raise self.run_error
        if self.run_returns_unasked:
            return
        if self.on_run is not None:
            await self.on_run()
        await self._release.wait()

    async def stop(self) -> None:
        self.rec.steps.append("stop")
        self.stop_called = True
        if self.stop_error is not None:
            raise self.stop_error
        if not self.ignore_stop:
            self._release.set()

    async def apply_config(self, config: Any) -> None:
        self.rec.steps.append("apply_config")
        self.reloaded.append(config)


class Recorder:
    """Collects the ordered startup steps, the log records, and the built fakes."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.logs: list[tuple[str, str, dict[str, Any]]] = []
        self.watcher: FakeWatcher | None = None
        self.observability: FakeObservability | None = None
        self.tracker: FakeTracker | None = None
        self.orchestrator: FakeOrchestrator | None = None
        self.reader: cli.SnapshotReader | None = None
        self.snapshot_calls = 0

    def messages(self, level: str | None = None) -> list[str]:
        return [m for lvl, m, _ in self.logs if level is None or lvl == level]

    def fields_for(self, message: str) -> dict[str, Any]:
        for _lvl, msg, fields in self.logs:
            if msg == message:
                return fields
        raise AssertionError(f"no log record with message {message!r}; got {self.messages()}")


def make_deps(
    rec: Recorder,
    *,
    config: FakeConfig | None = None,
    load_error: BaseException | None = None,
    build_config_error: BaseException | None = None,
    validate_error: BaseException | None = None,
    adapter_error: BaseException | None = None,
    observability_error: BaseException | None = None,
    watcher_start_error: BaseException | None = None,
    run_blocks: bool = True,
) -> cli.HostDeps:
    """Build a fully-faked :class:`~symphony.cli.HostDeps`.

    ``run_blocks=False`` makes the orchestrator's run loop return immediately.
    Tests that assert a *startup* failure through the synchronous
    :func:`symphony.cli.run` use it so that a regression which wrongly swallows
    the failure reports the wrong exit code instead of serving forever — a
    hanging suite diagnoses nothing.
    """
    cfg = config if config is not None else FakeConfig()

    def configure_logging() -> None:
        rec.steps.append("configure_logging")

    def get_logger(_name: str) -> FakeLogger:
        return FakeLogger(rec.logs)

    def load_workflow(path: Path) -> Any:
        rec.steps.append("load_workflow")
        if load_error is not None:
            raise load_error
        return {"source_path": str(path)}

    def build_config(_definition: Any) -> FakeConfig:
        rec.steps.append("build_config")
        if build_config_error is not None:
            raise build_config_error
        return cfg

    def validate_dispatch_config(_config: Any) -> None:
        rec.steps.append("validate")
        if validate_error is not None:
            raise validate_error

    def build_adapter(kind: str, provider: dict[str, Any], **_kw: Any) -> FakeTracker:
        rec.steps.append("build_adapter")
        if adapter_error is not None:
            raise adapter_error
        rec.tracker = FakeTracker(rec, kind, provider)
        return rec.tracker

    def build_watcher(path: Path, on_change: Any) -> FakeWatcher:
        watcher = FakeWatcher(rec, path, on_change)
        if watcher_start_error is not None:

            async def failing_start() -> None:
                raise watcher_start_error

            watcher.start = failing_start  # type: ignore[method-assign]
        rec.watcher = watcher
        return watcher

    async def start_observability(
        config: Any, port_override: int | None, reader: cli.SnapshotReader
    ) -> FakeObservability | None:
        rec.steps.append("start_observability")
        if observability_error is not None:
            raise observability_error
        port = port_override if port_override is not None else config.server_port
        if port is None:
            return None
        rec.reader = reader
        # Prove the host handed over working late-bound observability reads.
        reader.snapshot()
        reader.issue_detail("ENG-1")
        rec.observability = FakeObservability(rec, port)
        return rec.observability

    def build_orchestrator(config: Any, workflow: Any, tracker: Any) -> FakeOrchestrator:
        rec.steps.append("build_orchestrator")
        rec.orchestrator = FakeOrchestrator(rec, config, workflow, tracker)
        rec.orchestrator.run_returns_unasked = not run_blocks
        return rec.orchestrator

    def build_snapshot(state: Any) -> dict[str, Any]:
        rec.snapshot_calls += 1
        return {"running": len(state.running)}

    def build_issue_detail(state: Any, identifier: str) -> dict[str, Any]:
        return {"identifier": identifier, "running": len(state.running)}

    return cli.HostDeps(
        configure_logging=configure_logging,
        get_logger=get_logger,
        load_workflow=load_workflow,
        build_config=build_config,
        validate_dispatch_config=validate_dispatch_config,
        build_adapter=build_adapter,
        build_watcher=build_watcher,
        start_observability=start_observability,
        build_orchestrator=build_orchestrator,
        build_snapshot=build_snapshot,
        build_issue_detail=build_issue_detail,
    )


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An existing ``WORKFLOW.md`` in a cwd that is the tmp dir."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "WORKFLOW.md"
    path.write_text(WORKFLOW_TEXT, encoding="utf-8")
    return path


# ==========================================================================
# SPEC 17.7 bullet 1 — "CLI accepts a positional workflow path argument"
# ==========================================================================


def test_parser_accepts_positional_workflow_path() -> None:
    args = cli.build_parser().parse_args(["repo/WORKFLOW.md"])
    assert args.workflow_path == "repo/WORKFLOW.md"


async def test_explicit_path_is_used_verbatim(tmp_path: Path, rec: Recorder) -> None:
    explicit = tmp_path / "custom-workflow.md"
    explicit.write_text(WORKFLOW_TEXT, encoding="utf-8")

    host = await cli.start_service(str(explicit), deps=make_deps(rec))
    try:
        assert host.workflow_path == explicit
        assert rec.watcher is not None and rec.watcher.path == explicit
    finally:
        await host.aclose()


# ==========================================================================
# SPEC 17.7 bullet 2 — "uses ./WORKFLOW.md when no path argument is provided"
# ==========================================================================


def test_parser_defaults_positional_to_none() -> None:
    assert cli.build_parser().parse_args([]).workflow_path is None


def test_default_resolves_to_workflow_md_in_cwd(workflow: Path) -> None:
    assert cli.resolve_workflow_argument(None) == workflow


async def test_start_service_defaults_to_cwd_workflow(workflow: Path, rec: Recorder) -> None:
    host = await cli.start_service(deps=make_deps(rec))
    try:
        assert host.workflow_path == workflow
    finally:
        await host.aclose()


# ==========================================================================
# SPEC 17.7 bullet 3 — "errors on nonexistent explicit path or missing default"
# ==========================================================================


def test_nonexistent_explicit_path_raises_missing_workflow_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "WORKFLOW.md"
    with pytest.raises(MissingWorkflowFile) as excinfo:
        cli.resolve_workflow_argument(str(missing))
    assert excinfo.value.details["source"] == "argument"
    assert excinfo.value.details["path"] == str(missing)


def test_missing_default_workflow_raises_missing_workflow_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(MissingWorkflowFile) as excinfo:
        cli.resolve_workflow_argument(None)
    assert excinfo.value.details["source"] == "default"


def test_directory_at_workflow_path_is_not_accepted(tmp_path: Path) -> None:
    """A directory is not a readable workflow file; `is_file` must gate, not `exists`."""
    (tmp_path / "WORKFLOW.md").mkdir()
    with pytest.raises(MissingWorkflowFile):
        cli.resolve_workflow_argument(str(tmp_path / "WORKFLOW.md"))


def test_run_exits_nonzero_on_nonexistent_explicit_path(
    tmp_path: Path, rec: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.run(str(tmp_path / "absent.md"), deps=make_deps(rec, run_blocks=False))
    assert code == cli.EXIT_STARTUP_FAILURE
    assert "missing_workflow_file" in capsys.readouterr().err


def test_run_exits_nonzero_on_missing_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rec: Recorder
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.run(deps=make_deps(rec, run_blocks=False)) == cli.EXIT_STARTUP_FAILURE


def test_path_check_happens_before_any_startup_step(tmp_path: Path, rec: Recorder) -> None:
    """A bad path must not bring collaborators up just to tear them down."""
    cli.run(str(tmp_path / "absent.md"), deps=make_deps(rec, run_blocks=False))
    assert rec.steps == []


# ==========================================================================
# SPEC 17.7 bullet 4 — "CLI surfaces startup failure cleanly"
# ==========================================================================


def test_startup_failure_prints_one_clean_line_without_traceback(
    workflow: Path, rec: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = make_deps(
        rec,
        validate_error=ConfigValidationError("codex.command is required"),
        run_blocks=False,
    )
    code = cli.run(str(workflow), deps=deps)
    err = capsys.readouterr().err

    assert code == cli.EXIT_STARTUP_FAILURE
    assert "Traceback" not in err
    assert err.strip() == (
        "symphony: startup failed: config_validation_error: codex.command is required"
    )


def test_startup_failure_surfaces_the_spec_error_category(
    workflow: Path, rec: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = make_deps(
        rec,
        adapter_error=TrackerRequestError("cannot reach provider"),
        run_blocks=False,
    )
    cli.run(str(workflow), deps=deps)
    assert "tracker_request" in capsys.readouterr().err


def test_non_symphony_startup_failure_is_still_surfaced_cleanly(
    workflow: Path, rec: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sibling raising something untyped must not produce a traceback either."""
    deps = make_deps(rec, build_config_error=ValueError("boom"), run_blocks=False)
    code = cli.run(str(workflow), deps=deps)
    err = capsys.readouterr().err

    assert code == cli.EXIT_STARTUP_FAILURE
    assert "Traceback" not in err
    assert "ValueError: boom" in err


# ==========================================================================
# SPEC 17.7 bullet 5 — "exits with success when it starts and shuts down normally"
# ==========================================================================


async def test_serve_returns_on_requested_shutdown(workflow: Path, rec: Recorder) -> None:
    host = await cli.start_service(str(workflow), deps=make_deps(rec), grace_seconds=5.0)
    try:
        serving = asyncio.ensure_future(host.serve())
        assert rec.orchestrator is not None
        await rec.orchestrator.run_started.wait()

        host.request_shutdown("test")
        await serving  # must return, not raise

        assert rec.orchestrator.stop_called is True
        assert host.shutdown_reason == "test"
    finally:
        await host.aclose()


def test_run_exits_zero_on_sigint(workflow: Path, rec: Recorder) -> None:
    """End-to-end: a real in-process SIGINT must produce a graceful, zero exit.

    Asserting on the logged reason matters: ``run`` also maps a stray
    ``KeyboardInterrupt`` to ``EXIT_OK``, so a bare ``== 0`` would pass even if
    the signal handler were never installed.
    """
    deps = make_deps(rec)
    outer_build = deps.build_orchestrator
    assert outer_build is not None

    def build(config: Any, workflow_defn: Any, tracker: Any) -> FakeOrchestrator:
        orch = outer_build(config, workflow_defn, tracker)

        async def raise_sigint() -> None:
            signal.raise_signal(signal.SIGINT)

        orch.on_run = raise_sigint
        return orch

    deps.build_orchestrator = build

    code = cli.run(str(workflow), deps=deps, grace_seconds=5.0)

    assert code == cli.EXIT_OK
    assert rec.fields_for("shutdown requested")["reason"] == "SIGINT"
    assert rec.orchestrator is not None and rec.orchestrator.stop_called is True


async def test_graceful_shutdown_stops_dispatch_before_unwinding(
    workflow: Path, rec: Recorder
) -> None:
    """``stop`` (stop accepting dispatches) must precede the loop's return."""
    host = await cli.start_service(str(workflow), deps=make_deps(rec), grace_seconds=5.0)
    try:
        serving = asyncio.ensure_future(host.serve())
        assert rec.orchestrator is not None
        await rec.orchestrator.run_started.wait()
        host.request_shutdown("SIGTERM")
        await serving
    finally:
        await host.aclose()

    assert rec.steps.index("stop") > rec.steps.index("run")


async def test_a_wedged_loop_is_cancelled_after_the_grace_period(
    workflow: Path, rec: Recorder
) -> None:
    """Shutdown must terminate even when the orchestrator refuses to unwind."""
    host = await cli.start_service(str(workflow), deps=make_deps(rec), grace_seconds=0.0)
    try:
        assert rec.orchestrator is not None
        rec.orchestrator.ignore_stop = True

        serving = asyncio.ensure_future(host.serve())
        await rec.orchestrator.run_started.wait()
        host.request_shutdown("SIGTERM")
        await serving  # returns rather than hanging
    finally:
        await host.aclose()

    assert "orchestrator did not unwind in time; cancelling" in rec.messages("warning")


# ==========================================================================
# SPEC 17.7 bullet 6 — "exits nonzero when startup fails or the host exits abnormally"
# ==========================================================================


def test_startup_validation_failure_exits_nonzero(workflow: Path, rec: Recorder) -> None:
    deps = make_deps(
        rec,
        validate_error=ConfigValidationError("unsupported tracker kind"),
        run_blocks=False,
    )
    assert cli.run(str(workflow), deps=deps) == cli.EXIT_STARTUP_FAILURE


def test_workflow_load_failure_exits_nonzero(workflow: Path, rec: Recorder) -> None:
    deps = make_deps(rec, load_error=MissingWorkflowFile("unreadable"), run_blocks=False)
    assert cli.run(str(workflow), deps=deps) == cli.EXIT_STARTUP_FAILURE


def test_watcher_start_failure_exits_nonzero(workflow: Path, rec: Recorder) -> None:
    deps = make_deps(rec, watcher_start_error=OSError("inotify limit reached"), run_blocks=False)
    assert cli.run(str(workflow), deps=deps) == cli.EXIT_STARTUP_FAILURE


def test_abnormal_host_exit_exits_nonzero(workflow: Path, rec: Recorder) -> None:
    """The orchestrator raising after a successful startup is an abnormal exit."""
    deps = make_deps(rec)
    outer_build = deps.build_orchestrator
    assert outer_build is not None

    def build(config: Any, workflow_defn: Any, tracker: Any) -> FakeOrchestrator:
        orch = outer_build(config, workflow_defn, tracker)
        orch.run_error = RuntimeError("tick loop crashed")
        return orch

    deps.build_orchestrator = build
    assert cli.run(str(workflow), deps=deps) == cli.EXIT_RUNTIME_FAILURE


def test_loop_returning_without_a_shutdown_request_exits_nonzero(
    workflow: Path, rec: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run loop that simply stops looping is abnormal even though it did not raise."""
    deps = make_deps(rec)
    outer_build = deps.build_orchestrator
    assert outer_build is not None

    def build(config: Any, workflow_defn: Any, tracker: Any) -> FakeOrchestrator:
        orch = outer_build(config, workflow_defn, tracker)
        orch.run_returns_unasked = True
        return orch

    deps.build_orchestrator = build

    assert cli.run(str(workflow), deps=deps) == cli.EXIT_RUNTIME_FAILURE
    assert "host exited abnormally" in capsys.readouterr().err


def test_usage_error_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--not-a-flag"])
    assert excinfo.value.code == cli.EXIT_USAGE


def test_exit_codes_are_distinct_and_only_success_is_zero() -> None:
    failures = [cli.EXIT_STARTUP_FAILURE, cli.EXIT_USAGE, cli.EXIT_RUNTIME_FAILURE]
    assert cli.EXIT_OK == 0
    assert all(code != 0 for code in failures)
    assert len(set(failures)) == len(failures)


# ==========================================================================
# SPEC 16.1 — startup sequence order
# ==========================================================================


async def test_startup_runs_spec_16_1_steps_in_order(workflow: Path, rec: Recorder) -> None:
    host = await cli.start_service(str(workflow), deps=make_deps(rec))
    try:
        assert rec.steps == [
            "configure_logging",
            "load_workflow",
            "build_config",
            "start_observability",
            "start_watch",
            "validate",
            "build_adapter",
            "build_orchestrator",
            "orchestrator_start",
        ]
    finally:
        await host.aclose()


async def test_observability_and_watch_start_before_validation(
    workflow: Path, rec: Recorder
) -> None:
    """SPEC 16.1 order + SPEC 13.2: a validation failure must be operator-visible.

    If validation ran first, the surfaces that report it would not be up yet.
    """
    deps = make_deps(rec, validate_error=ConfigValidationError("bad"))
    with pytest.raises(ConfigValidationError):
        await cli.start_service(str(workflow), deps=deps)

    assert rec.steps.index("start_observability") < rec.steps.index("validate")
    assert rec.steps.index("start_watch") < rec.steps.index("validate")


async def test_initial_state_is_built_from_config(workflow: Path, rec: Recorder) -> None:
    config = FakeConfig(poll_interval_ms=1_234, max_concurrent_agents=7)
    host = await cli.start_service(str(workflow), deps=make_deps(rec, config=config))
    try:
        assert host.state is not None
        assert host.state.poll_interval_ms == 1_234
        assert host.state.max_concurrent_agents == 7
        assert host.state.running == {}
        assert host.state.claimed == set()
        assert host.state.codex_rate_limits is None
    finally:
        await host.aclose()


async def test_state_is_built_before_validation(workflow: Path, rec: Recorder) -> None:
    """SPEC 16.1 builds state, then validates; the failure branch is the last step."""
    captured: dict[str, Any] = {}

    deps = make_deps(rec)

    def validate(config: Any) -> None:
        rec.steps.append("validate")
        captured["state_exists"] = True
        raise ConfigValidationError("bad")

    deps.validate_dispatch_config = validate
    with pytest.raises(ConfigValidationError):
        await cli.start_service(str(workflow), deps=deps)
    assert captured["state_exists"] is True


async def test_failed_startup_unwinds_what_it_already_started(
    workflow: Path, rec: Recorder
) -> None:
    config = FakeConfig(server_port=0)
    deps = make_deps(rec, config=config, validate_error=ConfigValidationError("bad"))
    with pytest.raises(ConfigValidationError):
        await cli.start_service(str(workflow), deps=deps)

    assert rec.watcher is not None and rec.watcher.stopped is True
    assert rec.observability is not None and rec.observability.stopped is True


# ==========================================================================
# SPEC 6.3 / 6.2 / 14.2 — the fatal-vs-skip asymmetry
# ==========================================================================


async def test_startup_validation_failure_is_fatal(workflow: Path, rec: Recorder) -> None:
    """SPEC 6.3: 'If startup validation fails, fail startup.'"""
    deps = make_deps(rec, validate_error=ConfigValidationError("codex.command missing"))
    with pytest.raises(ConfigValidationError):
        await cli.start_service(str(workflow), deps=deps)
    assert "cleanup" not in rec.steps
    assert "build_orchestrator" not in rec.steps


async def test_reload_failure_is_not_fatal_and_keeps_last_known_good(
    workflow: Path, rec: Recorder
) -> None:
    """SPEC 6.2: an invalid reload MUST NOT crash and MUST keep the last good config.

    This is the mirror image of the test above: the same validation-shaped
    failure that kills startup must be survivable once the service is running.
    """
    deps = make_deps(rec)
    host = await cli.start_service(str(workflow), deps=deps)
    try:
        good = host.config

        def failing_build_config(_definition: Any) -> FakeConfig:
            raise ConfigValidationError("front matter went bad")

        deps.build_config = failing_build_config
        await host.reload_workflow()  # must not raise

        assert host.config is good
        assert rec.orchestrator is not None and rec.orchestrator.reloaded == []
        assert "workflow reload failed; keeping last known good config" in rec.messages("error")
    finally:
        await host.aclose()


async def test_reload_applies_new_config_to_live_behavior(workflow: Path, rec: Recorder) -> None:
    """SPEC 6.2: re-read and re-apply without restart."""
    deps = make_deps(rec)
    host = await cli.start_service(str(workflow), deps=deps)
    try:
        reloaded_config = FakeConfig(poll_interval_ms=99, tag="reloaded")
        deps.build_config = lambda _definition: reloaded_config

        assert rec.watcher is not None
        await rec.watcher.on_change()  # exactly what the watcher will call

        assert host.config is reloaded_config
        assert rec.orchestrator is not None
        assert rec.orchestrator.reloaded == [reloaded_config]
    finally:
        await host.aclose()


async def test_reload_never_revalidates_fatally(workflow: Path, rec: Recorder) -> None:
    """The reload path must not call the fatal SPEC 6.3 startup validation again."""
    deps = make_deps(rec)
    host = await cli.start_service(str(workflow), deps=deps)
    try:
        rec.steps.clear()
        await host.reload_workflow()
        assert "validate" not in rec.steps
    finally:
        await host.aclose()


async def test_reload_apply_failure_does_not_crash_the_host(
    workflow: Path, rec: Recorder
) -> None:
    deps = make_deps(rec)
    host = await cli.start_service(str(workflow), deps=deps)
    try:
        assert rec.orchestrator is not None

        async def failing_apply(_config: Any) -> None:
            raise RuntimeError("cannot rebind")

        rec.orchestrator.apply_config = failing_apply  # type: ignore[method-assign]
        await host.reload_workflow()

        assert "workflow reload could not be applied to live behavior" in rec.messages("error")
    finally:
        await host.aclose()


async def test_change_arriving_before_the_orchestrator_exists_only_updates_config(
    workflow: Path, rec: Recorder
) -> None:
    """SPEC 16.1 starts the watch before the orchestrator; that window must be safe."""
    host = cli.ServiceHost(workflow_path=workflow, deps=make_deps(rec))
    host.log = FakeLogger(rec.logs)
    assert host.orchestrator is None

    await host.reload_workflow()

    assert isinstance(host.config, FakeConfig)
    assert rec.messages("error") == []


# ==========================================================================
# SPEC 8.6 — startup terminal workspace cleanup
# ==========================================================================


async def test_spec_16_1_tail_is_delegated_before_the_first_tick(
    workflow: Path, rec: Recorder
) -> None:
    """``orchestrator.start()`` carries SPEC 16.1's tail, including SPEC 8.6 cleanup.

    The host's obligation is ordering: the fatal preflight first, then the
    delegated tail, and only then the run loop.
    """
    host = await cli.start_service(str(workflow), deps=make_deps(rec), grace_seconds=5.0)
    try:
        serving = asyncio.ensure_future(host.serve())
        assert rec.orchestrator is not None
        await rec.orchestrator.run_started.wait()
        host.request_shutdown("test")
        await serving
    finally:
        await host.aclose()

    assert (
        rec.steps.index("validate")
        < rec.steps.index("orchestrator_start")
        < rec.steps.index("run")
    )
    assert rec.orchestrator.started is True


def test_orchestrator_start_failure_fails_startup(workflow: Path, rec: Recorder) -> None:
    """The delegated tail re-runs the SPEC 6.3 preflight, so its failure is fatal.

    SPEC 8.6 cleanup failures are swallowed inside ``orchestrator.start()``
    (they are warnings there); anything that still escapes is a startup failure
    and must not be downgraded to a running-but-broken service.
    """
    deps = make_deps(rec, run_blocks=False)
    outer_build = deps.build_orchestrator
    assert outer_build is not None

    def build(config: Any, workflow_defn: Any, tracker: Any) -> FakeOrchestrator:
        orch = outer_build(config, workflow_defn, tracker)
        orch.start_error = ConfigValidationError("preflight failed on the second look")
        return orch

    deps.build_orchestrator = build

    assert cli.run(str(workflow), deps=deps) == cli.EXIT_STARTUP_FAILURE
    assert rec.orchestrator is not None and rec.orchestrator.started is False


# ==========================================================================
# SPEC 13.7 — the OPTIONAL HTTP extension
# ==========================================================================


def test_parser_accepts_port() -> None:
    assert cli.build_parser().parse_args(["--port", "8787"]).port == 8787


async def test_cli_port_overrides_server_port(workflow: Path, rec: Recorder) -> None:
    """SPEC 13.7: 'CLI --port overrides server.port when both are present.'"""
    deps = make_deps(rec, config=FakeConfig(server_port=1234))
    host = await cli.start_service(str(workflow), port=8787, deps=deps)
    try:
        assert rec.observability is not None and rec.observability.port == 8787
    finally:
        await host.aclose()


async def test_port_zero_enables_the_extension(workflow: Path, rec: Recorder) -> None:
    """SPEC 13.7: '0 requests an ephemeral port.' Falsy, but not absent."""
    deps = make_deps(rec, config=FakeConfig(server_port=4321))
    host = await cli.start_service(str(workflow), port=0, deps=deps)
    try:
        assert rec.observability is not None and rec.observability.port == 0
    finally:
        await host.aclose()


async def test_server_port_enables_the_extension_without_a_cli_flag(
    workflow: Path, rec: Recorder
) -> None:
    deps = make_deps(rec, config=FakeConfig(server_port=1234))
    host = await cli.start_service(str(workflow), deps=deps)
    try:
        assert rec.observability is not None and rec.observability.port == 1234
    finally:
        await host.aclose()


async def test_extension_stays_disabled_when_no_port_is_configured(
    workflow: Path, rec: Recorder
) -> None:
    host = await cli.start_service(str(workflow), deps=make_deps(rec))
    try:
        assert host.observability is None
        assert rec.observability is None
    finally:
        await host.aclose()


async def test_observability_failure_does_not_fail_startup(workflow: Path, rec: Recorder) -> None:
    """SPEC 13.7 + 14.2: the dashboard is never REQUIRED for orchestrator correctness."""
    deps = make_deps(rec, observability_error=OSError("address already in use"))
    host = await cli.start_service(str(workflow), port=8787, deps=deps)
    try:
        assert host.observability is None
        assert host.orchestrator is not None
        assert "observability outputs failed to start" in rec.messages("error")
    finally:
        await host.aclose()


async def test_the_dashboard_gets_the_hosts_own_live_reads(
    workflow: Path, rec: Recorder
) -> None:
    """Both SPEC 13.7.2 reads must resolve against this host, not a stub.

    Handing observability a placeholder for either one yields a dashboard that
    renders but always reports nothing, which no smoke test would notice.
    """
    deps = make_deps(rec, config=FakeConfig(server_port=0))
    host = await cli.start_service(str(workflow), deps=deps)
    try:
        assert rec.reader is not None
        assert rec.reader.snapshot() == host.snapshot()
        assert rec.reader.issue_detail("ENG-7") == {"identifier": "ENG-7", "running": 0}
    finally:
        await host.aclose()


async def test_snapshot_is_late_bound_and_survives_a_failing_builder(
    workflow: Path, rec: Recorder
) -> None:
    """Observability starts before state exists (SPEC 16.1), so snapshot must tolerate it."""
    deps = make_deps(rec, config=FakeConfig(server_port=0))
    host = await cli.start_service(str(workflow), deps=deps)
    try:
        assert rec.snapshot_calls == 0  # called before state existed -> returned None
        assert isinstance(host.snapshot(), dict)

        def boom(_state: Any) -> dict[str, Any]:
            raise RuntimeError("snapshot timeout")

        deps.build_snapshot = boom
        assert host.snapshot() is None  # SPEC 14.2: never propagates
    finally:
        await host.aclose()


# ==========================================================================
# The real default resolutions
#
# Everything above fakes ``start_observability`` and ``configure_logging``
# wholesale, which would leave the shipped implementations of the SPEC 13.7
# port precedence and the SPEC 13.2 log-sink fallback untested. These exercise
# them directly, with the not-yet-written sibling modules injected so the result
# does not depend on how far a parallel author has got.
# ==========================================================================


def _install_fake_module(
    monkeypatch: pytest.MonkeyPatch, dotted: str, module: ModuleType
) -> ModuleType:
    parent_name, _, child = dotted.rpartition(".")
    monkeypatch.setitem(sys.modules, dotted, module)
    monkeypatch.setattr(importlib.import_module(parent_name), child, module, raising=False)
    return module


class _FakeHttpServer:
    def __init__(self, port: int) -> None:
        self.port = port
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def _capture_build_http_server(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]], *, enabled: bool = True
) -> None:
    """Intercept the real ``build_http_server`` to record what the CLI passes it."""
    import symphony.http.server as http_server

    def fake_build(source: Any, **kwargs: Any) -> Any:
        calls.append({"source": source, **kwargs})
        return _FakeHttpServer(kwargs.get("cli_port") or 0) if enabled else None

    monkeypatch.setattr(http_server, "build_http_server", fake_build)


async def test_default_observability_delegates_port_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC 13.7's precedence rule has one owner: ``http.server.resolve_port``.

    The CLI's obligation is to hand both sources over unmodified — in
    particular not to collapse a ``--port 0`` into "absent" on the way.
    """
    calls: list[dict[str, Any]] = []
    _capture_build_http_server(monkeypatch, calls)
    reader = cli.SnapshotReader(snapshot=dict, issue_detail=lambda _i: None)

    await cli._default_start_observability(FakeConfig(server_port=1234), 0, reader)

    assert calls[0]["cli_port"] == 0
    assert calls[0]["config_port"] == 1234


async def test_default_observability_passes_a_none_cli_port_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _capture_build_http_server(monkeypatch, calls)
    reader = cli.SnapshotReader(snapshot=dict, issue_detail=lambda _i: None)

    await cli._default_start_observability(FakeConfig(server_port=8080), None, reader)

    assert calls[0]["cli_port"] is None
    assert calls[0]["config_port"] == 8080


async def test_default_observability_starts_the_server_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _capture_build_http_server(monkeypatch, calls)
    reader = cli.SnapshotReader(snapshot=dict, issue_detail=lambda _i: None)

    handle = await cli._default_start_observability(FakeConfig(), 8787, reader)

    assert isinstance(handle, _FakeHttpServer)
    assert handle.started is True


async def test_default_observability_returns_none_when_not_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_http_server`` returning ``None`` is the normal disabled result."""
    calls: list[dict[str, Any]] = []
    _capture_build_http_server(monkeypatch, calls, enabled=False)
    reader = cli.SnapshotReader(snapshot=dict, issue_detail=lambda _i: None)

    assert await cli._default_start_observability(FakeConfig(), None, reader) is None


async def test_default_observability_hands_over_both_live_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard needs SPEC 13.7.2's snapshot *and* per-issue detail."""
    calls: list[dict[str, Any]] = []
    _capture_build_http_server(monkeypatch, calls)
    reader = cli.SnapshotReader(
        snapshot=lambda: {"ok": True},
        issue_detail=lambda identifier: {"identifier": identifier},
    )

    await cli._default_start_observability(FakeConfig(), 0, reader)

    source = calls[0]["source"]
    assert source.snapshot() == {"ok": True}
    assert source.issue_detail("ENG-7") == {"identifier": "ENG-7"}


async def test_default_observability_composes_with_the_real_http_extension(
    workflow: Path, rec: Recorder
) -> None:
    """Integration: the real ``build_http_server`` binds an ephemeral loopback port.

    Everything above stubs the extension out, which would let a signature drift
    between this module and ``symphony.http`` go unnoticed. Port ``0`` keeps the
    bind local and free (SPEC 13.7).
    """
    deps = make_deps(rec)
    deps.start_observability = None  # use the real resolver

    host = await cli.start_service(str(workflow), port=0, deps=deps)
    try:
        assert host.observability is not None
        assert host.observability.port > 0  # an ephemeral port was actually bound
    finally:
        await host.aclose()


def test_default_logging_prefers_the_shipped_configure_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``observability.logging`` names it ``configure``; that must win."""
    module = ModuleType("symphony.observability.logging")
    order: list[str] = []
    module.configure = lambda: order.append("configure")  # type: ignore[attr-defined]
    module.configure_logging = lambda: order.append("configure_logging")  # type: ignore[attr-defined]
    _install_fake_module(monkeypatch, "symphony.observability.logging", module)

    monkeypatch.setattr(cli, "_basic_stderr_logging", lambda: order.append("fallback"))
    cli._default_configure_logging()

    assert order == ["configure"]


def test_default_logging_uses_the_observability_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("symphony.observability.logging")
    called: list[bool] = []
    module.configure_logging = lambda: called.append(True)  # type: ignore[attr-defined]
    _install_fake_module(monkeypatch, "symphony.observability.logging", module)

    fallback: list[bool] = []
    monkeypatch.setattr(cli, "_basic_stderr_logging", lambda: fallback.append(True))

    cli._default_configure_logging()

    assert called == [True]
    assert fallback == []


def test_default_logging_falls_back_when_the_sink_is_unconfigurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC 13.2: operators MUST see startup failures, so a sink must always exist."""
    module = ModuleType("symphony.observability.logging")  # no configure_logging
    _install_fake_module(monkeypatch, "symphony.observability.logging", module)

    fallback: list[bool] = []
    monkeypatch.setattr(cli, "_basic_stderr_logging", lambda: fallback.append(True))

    cli._default_configure_logging()

    assert fallback == [True]


def test_default_logging_falls_back_when_the_module_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "symphony.observability.logging", None)
    monkeypatch.setitem(sys.modules, "symphony.observability", None)

    fallback: list[bool] = []
    monkeypatch.setattr(cli, "_basic_stderr_logging", lambda: fallback.append(True))

    cli._default_configure_logging()

    assert fallback == [True]


# ==========================================================================
# Teardown
# ==========================================================================


async def test_aclose_unwinds_in_reverse_startup_order(workflow: Path, rec: Recorder) -> None:
    deps = make_deps(rec, config=FakeConfig(server_port=0))
    host = await cli.start_service(str(workflow), deps=deps)
    rec.steps.clear()
    await host.aclose()
    assert rec.steps == ["stop", "close_tracker", "stop_watch", "stop_observability"]


async def test_aclose_is_idempotent(workflow: Path, rec: Recorder) -> None:
    host = await cli.start_service(str(workflow), deps=make_deps(rec))
    await host.aclose()
    rec.steps.clear()
    await host.aclose()
    assert rec.steps == []


async def test_aclose_continues_past_a_failing_closer(workflow: Path, rec: Recorder) -> None:
    deps = make_deps(rec, config=FakeConfig(server_port=0))
    host = await cli.start_service(str(workflow), deps=deps)
    assert rec.watcher is not None
    rec.watcher.stop_error = OSError("watch already gone")

    await host.aclose()  # must not raise

    assert rec.observability is not None and rec.observability.stopped is True
    assert "workflow watch failed to stop" in rec.messages("warning")


async def test_serve_before_start_is_rejected(workflow: Path, rec: Recorder) -> None:
    host = cli.ServiceHost(workflow_path=workflow, deps=make_deps(rec))
    with pytest.raises(RuntimeError, match="before start"):
        await host.serve()


# ==========================================================================
# Signals — the POSIX/Windows difference is handled, not assumed
# ==========================================================================


def test_shutdown_request_is_idempotent_and_records_the_first_reason() -> None:
    host = cli.ServiceHost(workflow_path=Path("WORKFLOW.md"))
    host.request_shutdown("SIGTERM")
    host.request_shutdown("SIGINT")
    assert host.shutdown_reason == "SIGTERM"


def test_second_shutdown_request_escalates_to_a_forced_stop() -> None:
    host = cli.ServiceHost(workflow_path=Path("WORKFLOW.md"))
    host.request_shutdown("SIGINT")
    assert host._force.is_set() is False
    host.request_shutdown("SIGINT")
    assert host._force.is_set() is True


def test_signal_handlers_fall_back_to_signal_signal_without_loop_support() -> None:
    """Windows event loops raise NotImplementedError from ``add_signal_handler``.

    Simulated with a fake loop so the fallback is exercised on every platform,
    not only the one CI happens to run on.
    """

    class NoSignalLoop:
        def __init__(self) -> None:
            self.scheduled: list[tuple[Any, tuple[Any, ...]]] = []

        def add_signal_handler(self, *_a: Any, **_k: Any) -> None:
            raise NotImplementedError

        def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
            self.scheduled.append((callback, args))

        def remove_signal_handler(self, _sig: Any) -> None:
            raise AssertionError("loop-level removal must not be used on the fallback path")

    loop = NoSignalLoop()
    seen: list[str] = []
    original = signal.getsignal(signal.SIGINT)

    restore = cli.install_signal_handlers(loop, seen.append)  # type: ignore[arg-type]
    try:
        assert signal.getsignal(signal.SIGINT) is not original
        signal.raise_signal(signal.SIGINT)
        assert loop.scheduled, "the C-level handler did not reach the loop"

        callback, args = loop.scheduled[0]
        callback(*args)
        assert seen == ["SIGINT"]
    finally:
        restore()

    assert signal.getsignal(signal.SIGINT) is original


def test_signal_handlers_restore_the_previous_handlers() -> None:
    """Installation must be reversible on whichever platform is running."""
    loop = asyncio.new_event_loop()
    before = {
        name: signal.getsignal(getattr(signal, name))
        for name in ("SIGINT", "SIGTERM", "SIGBREAK")
        if hasattr(signal, name)
    }
    try:
        restore = cli.install_signal_handlers(loop, lambda _name: None)
        restore()
    finally:
        loop.close()

    for name, handler in before.items():
        assert signal.getsignal(getattr(signal, name)) is handler


def test_only_windows_needs_the_interpreter_wakeup_pump() -> None:
    """The pump exists solely for the Proactor loop's completion-port wait."""
    assert cli.WINDOWS_SIGNAL_POLL_SECONDS > 0
    assert ("_windows_signal_pump" in cli.ServiceHost.serve.__code__.co_names) is True


# ==========================================================================
# Entry points
# ==========================================================================


def test_main_is_thin_and_forwards_parsed_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(workflow_path: Any = None, **kwargs: Any) -> int:
        seen["path"] = workflow_path
        seen.update(kwargs)
        return 7

    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main(["repo/WORKFLOW.md", "--port", "9000"]) == 7
    assert seen == {"path": "repo/WORKFLOW.md", "port": 9000}


def test_main_returns_the_code_instead_of_exiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """``console_scripts`` uses the return value, and an RLM can call this in-process."""
    monkeypatch.setattr(cli, "run", lambda *_a, **_k: cli.EXIT_RUNTIME_FAILURE)
    assert cli.main([]) == cli.EXIT_RUNTIME_FAILURE


def test_module_entry_point_is_runnable() -> None:
    """``python -m symphony`` must resolve without importing unwritten siblings."""
    result = subprocess.run(
        [sys.executable, "-m", "symphony", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert "symphony" in result.stdout
