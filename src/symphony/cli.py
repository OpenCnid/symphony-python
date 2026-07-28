"""CLI entry point and host lifecycle (SPEC 16.1, 17.7).

This module owns the *process*: argument handling, the SPEC 16.1 startup
sequence, signal-driven shutdown, and the exit code. It owns no orchestration
policy — the poll/dispatch tick (SPEC 16.2-16.4, 16.6) belongs to
``symphony.orchestrator.core``.

Three properties shape the design:

**The 16.1 ordering is load-bearing.** Logging and the observability outputs come
up *before* dispatch preflight validation runs, so that a validation failure is
visible to an operator through the surfaces the spec requires (SPEC 13.2:
startup/validation failures MUST be visible without attaching a debugger). The
watch starts before validation for the same reason: an operator who boots with a
broken ``WORKFLOW.md`` should be able to fix it in place, and the process that
reports the failure should already have been watching the file.

**Startup validation is fatal; every later validation failure is not.** SPEC 6.3
splits these explicitly: startup validation failure MUST fail startup, while
per-tick validation failure only skips dispatch and keeps reconciliation alive
(SPEC 14.2). SPEC 6.2 pushes the same way for reloads — an invalid reload MUST
NOT crash the service and MUST keep the last known good config. So exactly one
validation call in this module is fatal (:meth:`ServiceHost.start`), and the
reload path (:meth:`ServiceHost.reload_workflow`) deliberately never validates
fatally at all.

**SPEC 16.1's tail belongs to the orchestrator.** CONTRACTS.md assigns 16.1-16.4
to ``orchestrator.core``, whose ``start()`` performs the preflight re-validation,
the SPEC 8.6 startup terminal workspace cleanup, and ``schedule_tick(0)``. The
host therefore owns steps 1-4 and the fatal gate, then delegates the tail. That
keeps SPEC 8.6's "log a warning and continue" next to the tracker and workspace
manager that implement it, instead of duplicating the rule here.

RLM note: ``main`` is a thin wrapper. The startup sequence is
:func:`start_service`, which returns a live :class:`ServiceHost`; a model driving
this system from a Python REPL can ``await start_service(...)``, inspect
``host.state``, ``host.config``, and ``host.orchestrator``, and
``await host.aclose()`` without ever spawning a subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from symphony import __version__
from symphony.errors import MissingWorkflowFile, SymphonyError

__all__ = [
    "EXIT_OK",
    "EXIT_RUNTIME_FAILURE",
    "EXIT_STARTUP_FAILURE",
    "EXIT_USAGE",
    "HostDeps",
    "ServiceHost",
    "SnapshotReader",
    "build_parser",
    "install_signal_handlers",
    "main",
    "resolve_workflow_argument",
    "run",
    "start_service",
]

# --------------------------------------------------------------------------
# Exit codes (SPEC 17.7)
# --------------------------------------------------------------------------

EXIT_OK = 0
"""Started and shut down normally — including an operator-requested signal stop."""

EXIT_STARTUP_FAILURE = 1
"""The SPEC 16.1 startup sequence did not complete."""

EXIT_USAGE = 2
"""Bad command line. Fixed at 2 because :mod:`argparse` hard-codes it."""

EXIT_RUNTIME_FAILURE = 3
"""Startup succeeded but the host exited abnormally (raised, or returned unasked)."""

SHUTDOWN_GRACE_SECONDS = 30.0
"""How long a graceful stop may take to unwind before the run task is cancelled."""

WINDOWS_SIGNAL_POLL_SECONDS = 0.25
"""Interpreter wakeup cadence used only on Windows; see :func:`install_signal_handlers`."""

PROGRAM_NAME = "symphony"


# --------------------------------------------------------------------------
# Collaborator protocols
#
# CONTRACTS.md section 3 fixes the signatures of workflow.loader,
# workflow.config, workflow.watcher, trackers.base and observability, and this
# module uses those verbatim. It does not fix one for orchestrator.core or
# http.server, so the minimum the host needs is stated here and matched to the
# shipped modules in the ``_default_*`` resolvers below.
# --------------------------------------------------------------------------


class Orchestrator(Protocol):
    """What the host requires of ``symphony.orchestrator.core.Orchestrator``."""

    state: Any

    async def start(self) -> None:
        """SPEC 16.1 tail: preflight, SPEC 8.6 cleanup, and ``schedule_tick(0)``."""

    async def run_forever(self) -> None:
        """Await the tick loop. Returns only after :meth:`stop`."""

    async def stop(self) -> None:
        """Stop accepting new dispatches and let in-flight work unwind."""

    async def apply_config(self, config: Any) -> None:
        """Re-apply a reloaded ``ServiceConfig`` to live behavior (SPEC 6.2)."""


class Watcher(Protocol):
    """``symphony.workflow.watcher.WorkflowWatcher`` (CONTRACTS.md section 3)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class ObservabilityHandle(Protocol):
    """A started observability output the host must later shut down."""

    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SnapshotReader:
    """The two late-bound reads handed to observability (SPEC 13.3, 13.7.2).

    Observability starts before the orchestrator exists (SPEC 16.1), so the
    dashboard cannot be handed a live ``OrchestratorState``. It gets these
    callables instead, which resolve against the host on every request.
    """

    snapshot: Callable[[], Any]
    issue_detail: Callable[[str], Any]


# --------------------------------------------------------------------------
# Dependency seams
# --------------------------------------------------------------------------


@dataclass(slots=True)
class HostDeps:
    """Injection points for every collaborator the host wires together.

    Each field defaults to ``None``, meaning "resolve the real sibling module on
    first use". Lazy resolution is not stylistic: it keeps ``import symphony.cli``
    free of import-time coupling to the whole system, so ``--version`` and
    ``--help`` work even when an optional extension is unimportable.

    Tests replace fields directly; nothing here reaches the filesystem, the
    network, or a subprocess by itself.
    """

    configure_logging: Callable[[], None] | None = None
    get_logger: Callable[[str], Any] | None = None
    load_workflow: Callable[[Path], Any] | None = None
    build_config: Callable[[Any], Any] | None = None
    validate_dispatch_config: Callable[[Any], None] | None = None
    build_adapter: Callable[..., Any] | None = None
    build_watcher: Callable[[Path, Callable[[], Awaitable[None]]], Watcher] | None = None
    start_observability: (
        Callable[[Any, int | None, SnapshotReader], Awaitable[ObservabilityHandle | None]] | None
    ) = None
    build_orchestrator: Callable[[Any, Any, Any], Orchestrator] | None = None
    build_snapshot: Callable[[Any], Any] | None = None
    build_issue_detail: Callable[[Any, str], Any] | None = None

    # -- resolution ------------------------------------------------------

    def resolved_configure_logging(self) -> Callable[[], None]:
        return self.configure_logging or _default_configure_logging

    def resolved_get_logger(self) -> Callable[[str], Any]:
        if self.get_logger is not None:
            return self.get_logger
        from symphony.observability.logging import get_logger

        return get_logger

    def resolved_load_workflow(self) -> Callable[[Path], Any]:
        if self.load_workflow is not None:
            return self.load_workflow
        from symphony.workflow.loader import load_workflow

        return load_workflow

    def resolved_build_config(self) -> Callable[[Any], Any]:
        if self.build_config is not None:
            return self.build_config
        from symphony.workflow.config import build_config

        return build_config

    def resolved_validate_dispatch_config(self) -> Callable[[Any], None]:
        if self.validate_dispatch_config is not None:
            return self.validate_dispatch_config
        from symphony.workflow.config import validate_dispatch_config

        return validate_dispatch_config

    def resolved_build_adapter(self) -> Callable[..., Any]:
        if self.build_adapter is not None:
            return self.build_adapter
        # Importing the package (not just ``base``) is what registers the
        # bundled adapters, so ``tracker.kind`` resolves for a bare WORKFLOW.md.
        from symphony.trackers import build_adapter

        return build_adapter

    def resolved_build_watcher(self) -> Callable[[Path, Callable[[], Awaitable[None]]], Watcher]:
        if self.build_watcher is not None:
            return self.build_watcher
        from symphony.workflow.watcher import WorkflowWatcher

        return WorkflowWatcher

    def resolved_build_orchestrator(self) -> Callable[[Any, Any, Any], Orchestrator]:
        return self.build_orchestrator or _default_build_orchestrator

    def resolved_start_observability(
        self,
    ) -> Callable[[Any, int | None, SnapshotReader], Awaitable[ObservabilityHandle | None]]:
        return self.start_observability or _default_start_observability

    def resolved_build_snapshot(self) -> Callable[[Any], Any]:
        if self.build_snapshot is not None:
            return self.build_snapshot
        from symphony.observability.snapshot import build_snapshot

        return build_snapshot

    def resolved_build_issue_detail(self) -> Callable[[Any, str], Any]:
        if self.build_issue_detail is not None:
            return self.build_issue_detail
        from symphony.observability.snapshot import build_issue_detail

        return build_issue_detail


def _default_configure_logging() -> None:
    """Bring up the log sink (SPEC 13.2).

    ``symphony.observability.logging.configure`` installs the structured router.
    The stderr fallback exists because SPEC 13.2 makes operator visibility of
    startup and validation failures a REQUIREMENT: a missing or unconfigurable
    sink must not be the reason nobody can see why the service refused to boot.
    """
    try:
        from symphony.observability import logging as observability_logging
    except ImportError:
        _basic_stderr_logging()
        return

    for name in ("configure", "configure_logging"):
        entry = getattr(observability_logging, name, None)
        if callable(entry):
            entry()
            return
    _basic_stderr_logging()


def _basic_stderr_logging() -> None:
    import logging

    if not logging.getLogger().handlers:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")


async def _default_start_observability(
    config: Any,
    port_override: int | None,
    reader: SnapshotReader,
) -> ObservabilityHandle | None:
    """Start observability outputs (SPEC 16.1, 13.7).

    For this implementation the only *startable* output is the OPTIONAL HTTP
    extension. Port precedence — CLI ``--port`` over ``server.port``, with ``0``
    a real request for an ephemeral port rather than an absence — is applied by
    ``symphony.http.server`` so that SPEC 13.7's rule has exactly one owner.
    ``None`` back from ``build_http_server`` means the extension is not enabled,
    which is the normal default and not an error.
    """
    from symphony.http.api import SnapshotSource
    from symphony.http.server import build_http_server

    server = build_http_server(
        SnapshotSource(snapshot=reader.snapshot, issue_detail=reader.issue_detail),
        cli_port=port_override,
        config_port=getattr(config, "server_port", None),
    )
    if server is None:
        return None
    await server.start()
    return server


def _default_build_orchestrator(config: Any, workflow: Any, tracker: Any) -> Orchestrator:
    """Assemble the orchestrator and the collaborators its constructor requires.

    The host is the only process-level assembler, so the workspace manager, hook
    runner, and agent runner are built here even though their behavior belongs
    to their own modules.

    The agent runner reports SPEC 10.4 events to the orchestrator, and the
    orchestrator needs the runner, so the event sink is bound late through a
    closure rather than by constructing either one twice.
    """
    from symphony.agent.runner import AgentRunner
    from symphony.orchestrator.core import Orchestrator as CoreOrchestrator
    from symphony.workspace.hooks import HookRunner
    from symphony.workspace.manager import WorkspaceManager

    built: dict[str, Any] = {}

    def on_event(issue_id: str, event: Any) -> None:
        orchestrator = built.get("orchestrator")
        if orchestrator is not None:
            orchestrator.report_agent_event(issue_id, event)

    hooks = HookRunner(config.hooks)
    workspaces = WorkspaceManager(config.workspace_root, hooks)
    runner = AgentRunner(
        config=config,
        workflow=workflow,
        workspace_manager=workspaces,
        hooks=hooks,
        tracker=tracker,
        on_event=on_event,
    )
    orchestrator = CoreOrchestrator(
        config=config, tracker=tracker, runner=runner, workspaces=workspaces
    )
    built["orchestrator"] = orchestrator
    return orchestrator


# --------------------------------------------------------------------------
# Workflow path resolution (SPEC 5.1, 17.7)
# --------------------------------------------------------------------------


def resolve_workflow_argument(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve and existence-check the workflow path (SPEC 5.1, 17.7).

    ``resolve_workflow_path`` is pure by design, so the "nonexistent explicit
    path or missing default ``./WORKFLOW.md``" check (SPEC 17.7) lands here. The
    check is not merely an early copy of what :func:`load_workflow` would raise:
    it is what lets the operator-facing message name *which* of the two SPEC 5.1
    precedence steps was taken, which the loader cannot know.
    """
    from symphony.workflow.loader import resolve_workflow_path

    path = resolve_workflow_path(explicit)
    if path.is_file():
        return path

    if explicit is not None and os.fspath(explicit).strip():
        raise MissingWorkflowFile(
            "workflow file does not exist at the path given on the command line",
            path=str(path),
            source="argument",
        )
    raise MissingWorkflowFile(
        "no WORKFLOW.md in the current working directory and no path given",
        path=str(path),
        source="default",
    )


# --------------------------------------------------------------------------
# Signals (SPEC 17.7 — clean termination)
# --------------------------------------------------------------------------


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    on_signal: Callable[[str], None],
) -> Callable[[], None]:
    """Route SIGINT/SIGTERM (and Windows SIGBREAK) to *on_signal*.

    Returns a callable that restores the previous handlers.

    POSIX and Windows genuinely differ here, so the difference is handled rather
    than assumed away:

    * ``loop.add_signal_handler`` is implemented only on Unix event loops. On
      Windows it raises ``NotImplementedError``, so this falls back to
      ``signal.signal`` and hops back onto the loop thread with
      ``call_soon_threadsafe`` — the C-level handler runs on whichever thread
      the interpreter chooses, and the shutdown flag must not be set from there.
    * ``signal.signal`` is legal only on the main thread; a non-main-thread host
      gets no handlers and is expected to call
      :meth:`ServiceHost.request_shutdown` itself.
    * The Windows ``ProactorEventLoop`` can park in a completion-port wait
      without executing Python bytecode, which is exactly when a pending
      C-level handler cannot run. :meth:`ServiceHost.serve` therefore also runs
      :func:`_windows_signal_pump` so the interpreter reliably regains control.
    * SIGBREAK (Ctrl+Break) exists only on Windows and is included there because
      it is the console signal that survives cases where SIGINT does not.
    """
    restores: list[Callable[[], None]] = []

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        restore = _install_one(loop, sig, name, on_signal)
        if restore is not None:
            restores.append(restore)

    def restore_all() -> None:
        for restore in reversed(restores):
            with contextlib.suppress(Exception):
                restore()

    return restore_all


def _install_one(
    loop: asyncio.AbstractEventLoop,
    sig: signal.Signals,
    name: str,
    on_signal: Callable[[str], None],
) -> Callable[[], None] | None:
    try:
        loop.add_signal_handler(sig, on_signal, name)
    except (NotImplementedError, RuntimeError, AttributeError, OSError, ValueError):
        pass
    else:
        return lambda: loop.remove_signal_handler(sig)

    def handler(_signum: int, _frame: Any) -> None:
        loop.call_soon_threadsafe(on_signal, name)

    try:
        previous = signal.signal(sig, handler)
    except (ValueError, OSError, RuntimeError):
        # Not the main thread, or the signal is unsupported on this platform.
        return None

    return lambda: signal.signal(sig, previous)


async def _windows_signal_pump(interval: float = WINDOWS_SIGNAL_POLL_SECONDS) -> None:
    """Yield to the interpreter periodically so pending signal handlers can run."""
    while True:
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------
# Host
# --------------------------------------------------------------------------


class ServiceHost:
    """A started Symphony service and everything the process must unwind.

    Constructed unstarted so that the late-bound :class:`SnapshotReader` handed
    to observability — before any orchestrator state exists — has a stable
    object to resolve against.
    """

    def __init__(
        self,
        *,
        workflow_path: Path,
        port_override: int | None = None,
        deps: HostDeps | None = None,
        grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self.workflow_path = workflow_path
        self.port_override = port_override
        self.deps = deps or HostDeps()
        self.grace_seconds = grace_seconds

        self.workflow: Any = None
        self.config: Any = None
        self.orchestrator: Orchestrator | None = None
        self.tracker: Any = None
        self.watcher: Watcher | None = None
        self.observability: ObservabilityHandle | None = None

        self.log: Any = None
        self.shutdown_reason: str | None = None

        self._shutdown = asyncio.Event()
        self._force = asyncio.Event()
        self._closed = False
        self._signal_count = 0

    @property
    def state(self) -> Any:
        """The SPEC 4.1.8 runtime state, or ``None`` before the orchestrator exists.

        Owned by the orchestrator (CONTRACTS.md gives it SPEC 16.1); the host
        reads it for observability and for an RLM driver.
        """
        return None if self.orchestrator is None else self.orchestrator.state

    # -- SPEC 16.1 -------------------------------------------------------

    async def start(self) -> None:
        """Run the SPEC 16.1 startup sequence, in order.

        1. ``configure_logging()``
        2. resolve effective config — load ``WORKFLOW.md`` and build the typed
           view. Inserted before step 3 because SPEC 13.7 makes the observability
           listener config-driven (``server.port``); SPEC 16.1 treats config
           access as ambient from its first line onward.
        3. ``start_observability_outputs()``
        4. ``start_workflow_watch(on_change=reload_and_reapply_workflow)``
        5. ``validate_dispatch_config()`` — **fatal on failure** (SPEC 6.3)
        6. build the tracker adapter and the orchestrator
        7. ``orchestrator.start()`` — the SPEC 16.1 tail: preflight re-check,
           SPEC 8.6 startup terminal workspace cleanup (a warning there, not a
           startup failure), and ``schedule_tick(0)``

        Steps 3 and 4 precede step 5 exactly as the spec orders them, so the
        validation error in step 5 is emitted through an already-running
        observability surface (SPEC 13.2).

        Any failure unwinds whatever already started and re-raises; the caller
        maps that to :data:`EXIT_STARTUP_FAILURE`.
        """
        self.deps.resolved_configure_logging()()
        self.log = self.deps.resolved_get_logger()("symphony.cli")
        self.log.info(
            "service starting",
            workflow_path=str(self.workflow_path),
            version=__version__,
        )

        try:
            self.config = self._load_effective_config()
            await self._start_observability()
            await self._start_workflow_watch()
            self._validate_for_startup()
            self.tracker = self.deps.resolved_build_adapter()(
                self.config.tracker_kind, self.config.tracker_provider
            )
            self.orchestrator = self.deps.resolved_build_orchestrator()(
                self.config, self.workflow, self.tracker
            )
            await self.orchestrator.start()
        except BaseException:
            await self.aclose()
            raise

        self.log.info("service started", outcome="completed")

    def _load_effective_config(self) -> Any:
        """SPEC 16.1 step 2. A load or build failure is fatal (SPEC 6.3, 14.1).

        The parsed :class:`~symphony.models.WorkflowDefinition` is retained, not
        just the typed config: the agent runner renders the SPEC 5.4 prompt
        template from it.
        """
        self.workflow = self.deps.resolved_load_workflow()(self.workflow_path)
        return self.deps.resolved_build_config()(self.workflow)

    async def _start_observability(self) -> None:
        """SPEC 16.1 step 3.

        A failure here is logged as an operator-visible error and startup
        continues: SPEC 13.7 forbids the dashboard from becoming REQUIRED for
        orchestrator correctness, and SPEC 14.2 says dashboard failures do not
        crash the orchestrator.
        """
        reader = SnapshotReader(snapshot=self.snapshot, issue_detail=self.issue_detail)
        try:
            self.observability = await self.deps.resolved_start_observability()(
                self.config, self.port_override, reader
            )
        except Exception as exc:
            self.log.error(
                "observability outputs failed to start",
                outcome="failed",
                reason=_reason(exc),
                category=_category(exc),
            )
            self.observability = None
            return

        if self.observability is not None:
            self.log.info(
                "observability outputs started",
                outcome="completed",
                port=getattr(self.observability, "port", None),
            )

    async def _start_workflow_watch(self) -> None:
        """SPEC 16.1 step 4 — ``start_workflow_watch(on_change=...)`` (SPEC 6.2)."""
        self.watcher = self.deps.resolved_build_watcher()(self.workflow_path, self.reload_workflow)
        await self.watcher.start()

    def _validate_for_startup(self) -> None:
        """SPEC 16.1 step 5 — the one fatal validation the host performs.

        SPEC 6.3 is explicit that a startup validation failure fails startup,
        while the identical check on a later tick only skips dispatch
        (SPEC 14.2). ``orchestrator.start()`` runs the same preflight again;
        the duplication is deliberate and cheap. Checking here means a bad
        config is rejected before four collaborators are constructed, and it
        keeps the exit-code decision — which only the host can make — from
        depending on orchestrator internals.
        """
        self.deps.resolved_validate_dispatch_config()(self.config)

    # -- SPEC 6.2 --------------------------------------------------------

    async def reload_workflow(self) -> None:
        """Re-read and re-apply ``WORKFLOW.md`` (SPEC 6.2).

        Deliberately non-fatal in every branch. SPEC 6.2 requires that an
        invalid reload not crash the service and that the last known good
        effective configuration stay in force, so a failed load leaves
        ``self.config`` untouched. No fatal ``validate_dispatch_config`` call
        belongs here — an unusable reloaded config is caught by the per-tick
        preflight, which skips dispatch and keeps reconciliation running
        (SPEC 6.3, 14.2).
        """
        previous_workflow = self.workflow
        try:
            config = self._load_effective_config()
        except Exception as exc:
            self.workflow = previous_workflow
            self.log.error(
                "workflow reload failed; keeping last known good config",
                outcome="failed",
                reason=_reason(exc),
                category=_category(exc),
            )
            return

        self.config = config
        if self.orchestrator is None:
            # The watch starts before the orchestrator exists (SPEC 16.1 order);
            # a change landing in that window is captured by config alone.
            return

        try:
            await self.orchestrator.apply_config(config)
        except Exception as exc:
            self.log.error(
                "workflow reload could not be applied to live behavior",
                outcome="failed",
                reason=_reason(exc),
                category=_category(exc),
            )
            return

        self.log.info("workflow reloaded", outcome="completed")

    # -- observability reads (SPEC 13.3, 13.7.2) -------------------------

    def snapshot(self) -> Any:
        """Current runtime snapshot, or ``None`` before the orchestrator exists."""
        return self._read(lambda state: self.deps.resolved_build_snapshot()(state))

    def issue_detail(self, identifier: str) -> Any:
        """Per-issue detail, or ``None`` before the orchestrator exists."""
        return self._read(lambda state: self.deps.resolved_build_issue_detail()(state, identifier))

    def _read(self, fn: Callable[[Any], Any]) -> Any:
        state = self.state
        if state is None:
            return None
        try:
            return fn(state)
        except Exception:
            # SPEC 14.2: observability failures never propagate into the host.
            return None

    # -- run / shutdown --------------------------------------------------

    def request_shutdown(self, reason: str) -> None:
        """Ask the host to stop. Idempotent; safe from a signal callback.

        A second request escalates: an operator pressing Ctrl-C twice must not
        be held hostage by a stop that will not unwind.
        """
        self._signal_count += 1
        if self._shutdown.is_set():
            if self._signal_count >= 2:
                self._force.set()
            return
        self.shutdown_reason = reason
        self._shutdown.set()

    async def serve(self) -> None:
        """Run until shutdown is requested (SPEC 16.1 ``event_loop``).

        Returns normally on an operator-requested stop. Raises if the
        orchestrator raised, or if it returned without being asked to stop —
        both are the "host process exits abnormally" case in SPEC 17.7.
        """
        if self.orchestrator is None:
            raise RuntimeError("serve() called before start()")

        loop = asyncio.get_running_loop()
        restore = install_signal_handlers(loop, self._on_signal)
        pump: asyncio.Task[None] | None = None
        if sys.platform == "win32":
            pump = asyncio.ensure_future(_windows_signal_pump())

        run_task: asyncio.Task[None] = asyncio.ensure_future(self.orchestrator.run_forever())
        stop_task: asyncio.Task[bool] = asyncio.ensure_future(self._shutdown.wait())

        try:
            await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

            if run_task.done():
                run_task.result()  # re-raise the orchestrator's exception, if any
                raise RuntimeError("orchestrator run loop exited without a shutdown request")

            self.log.info("shutdown requested", reason=self.shutdown_reason or "unknown")
            await self._graceful_stop(run_task)
        finally:
            stop_task.cancel()
            await _quiet_cancel(stop_task)
            if pump is not None:
                pump.cancel()
                await _quiet_cancel(pump)
            restore()

    async def _graceful_stop(self, run_task: asyncio.Task[None]) -> None:
        """Stop accepting new dispatches, then let the loop unwind."""
        try:
            await self.orchestrator.stop()  # type: ignore[union-attr]
        except Exception as exc:
            self.log.error("orchestrator stop failed", outcome="failed", reason=_reason(exc))

        force_task: asyncio.Task[bool] = asyncio.ensure_future(self._force.wait())
        try:
            await asyncio.wait(
                {run_task, force_task},
                timeout=self.grace_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            force_task.cancel()
            await _quiet_cancel(force_task)

        if run_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                # An exception raised *while* unwinding a requested shutdown is
                # reported, not promoted to an abnormal exit.
                exc = run_task.exception()
                if exc is not None:
                    self.log.error(
                        "orchestrator run loop failed during shutdown",
                        outcome="failed",
                        reason=_reason(exc),
                    )
            return

        self.log.warning(
            "orchestrator did not unwind in time; cancelling",
            outcome="failed",
            grace_seconds=self.grace_seconds,
            forced=self._force.is_set(),
        )
        run_task.cancel()
        await _quiet_cancel(run_task)

    def _on_signal(self, name: str) -> None:
        self.request_shutdown(name)

    async def aclose(self) -> None:
        """Tear down started components in reverse startup order. Idempotent.

        Every step is individually guarded: one collaborator refusing to close
        must not strand the others, and teardown noise must never turn a clean
        run into a nonzero exit.
        """
        if self._closed:
            return
        self._closed = True

        await self._close_quietly("orchestrator", getattr(self.orchestrator, "stop", None))
        await self._close_quietly("tracker adapter", getattr(self.tracker, "aclose", None))
        await self._close_quietly("workflow watch", getattr(self.watcher, "stop", None))
        await self._close_quietly(
            "observability outputs", getattr(self.observability, "stop", None)
        )

    async def _close_quietly(self, what: str, closer: Callable[[], Any] | None) -> None:
        if closer is None:
            return
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            if self.log is not None:
                self.log.warning(f"{what} failed to stop", outcome="failed", reason=_reason(exc))


async def start_service(
    workflow_path: str | os.PathLike[str] | None = None,
    *,
    port: int | None = None,
    deps: HostDeps | None = None,
    grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
) -> ServiceHost:
    """Run the SPEC 16.1 startup sequence and return the live host.

    This is the seam the module docstring promises an RLM: it performs every
    startup step, leaves the service ticking, and hands back an object whose
    ``config``, ``state``, and ``orchestrator`` are directly inspectable. The
    caller owns the returned host and must ``await host.aclose()``.

    Raises whatever the failing startup step raised — typically a
    :class:`~symphony.errors.SymphonyError` subclass.
    """
    host = ServiceHost(
        workflow_path=resolve_workflow_argument(workflow_path),
        port_override=port,
        deps=deps,
        grace_seconds=grace_seconds,
    )
    await host.start()
    return host


# --------------------------------------------------------------------------
# Argument handling (SPEC 17.7, 13.7)
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The command line (SPEC 17.7 positional path, SPEC 13.7 ``--port``)."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Run the Symphony orchestrator against a repository WORKFLOW.md.",
    )
    parser.add_argument(
        "workflow_path",
        nargs="?",
        default=None,
        metavar="path-to-WORKFLOW.md",
        help="Workflow file to run. Defaults to ./WORKFLOW.md (SPEC 5.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Enable the HTTP dashboard/API on this port, overriding server.port (SPEC 13.7).",
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM_NAME} {__version__}")
    return parser


async def _amain(
    workflow_path: str | os.PathLike[str] | None,
    *,
    port: int | None,
    deps: HostDeps | None,
    grace_seconds: float,
) -> int:
    try:
        host = await start_service(workflow_path, port=port, deps=deps, grace_seconds=grace_seconds)
    except Exception as exc:
        _report_startup_failure(exc)
        return EXIT_STARTUP_FAILURE

    try:
        await host.serve()
    except Exception as exc:
        _report(f"host exited abnormally: {_describe(exc)}")
        if host.log is not None:
            host.log.error("host exited abnormally", outcome="failed", reason=_reason(exc))
        return EXIT_RUNTIME_FAILURE
    finally:
        await host.aclose()

    if host.log is not None:
        host.log.info("service stopped", outcome="completed", reason=host.shutdown_reason)
    return EXIT_OK


def run(
    workflow_path: str | os.PathLike[str] | None = None,
    *,
    port: int | None = None,
    deps: HostDeps | None = None,
    grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
) -> int:
    """Start the service, serve until shutdown, and return the process exit code.

    Synchronous so it can be called from ``main`` and from a plain REPL; use
    :func:`start_service` when you already have a running event loop.
    """
    try:
        return asyncio.run(_amain(workflow_path, port=port, deps=deps, grace_seconds=grace_seconds))
    except KeyboardInterrupt:
        # A SIGINT that beat the handler into place, or one delivered between
        # loop iterations. Operator-requested stop, so still success.
        return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point. Thin by design (see the module docstring).

    Returns the exit code rather than calling :func:`sys.exit`, because
    ``console_scripts`` already treats the return value as the process status
    and an RLM can then call ``main`` in-process.
    """
    args = build_parser().parse_args(argv)
    return run(args.workflow_path, port=args.port)


# --------------------------------------------------------------------------
# Error reporting (SPEC 17.7 "surfaces startup failure cleanly")
# --------------------------------------------------------------------------


def _report_startup_failure(exc: BaseException) -> None:
    """Report a failed startup as one operator-readable line, never a traceback.

    "Cleanly" (SPEC 17.7) means the operator gets the category and message that
    SPEC 5.5/6.3/11.4 already defined for this failure, on stderr, without
    reading a Python stack.
    """
    _report(f"startup failed: {_describe(exc)}")


def _report(message: str) -> None:
    print(f"{PROGRAM_NAME}: {message}", file=sys.stderr)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, SymphonyError):
        path = exc.details.get("path")
        suffix = f" (path={path})" if path else ""
        return f"{exc.category}: {exc.message}{suffix}"
    return f"{type(exc).__name__}: {exc}"


def _reason(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _category(exc: BaseException) -> str:
    return exc.category if isinstance(exc, SymphonyError) else type(exc).__name__


async def _quiet_cancel(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    sys.exit(main())
