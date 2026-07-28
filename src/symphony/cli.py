"""CLI entry point and host lifecycle (SPEC 16.1, 17.7).

This module owns the *process*: argument handling, the SPEC 16.1 startup
sequence, signal-driven shutdown, and the exit code. It owns no orchestration
policy — the poll/dispatch tick (SPEC 16.2-16.4, 16.6) belongs to
``symphony.orchestrator.core``.

Two properties shape the design:

**The 16.1 ordering is load-bearing.** Logging and the observability outputs come
up *before* dispatch preflight validation runs, so that a validation failure is
visible to an operator through the surfaces the spec requires (SPEC 13.2:
startup/validation failures MUST be visible without attaching a debugger). The
watch starts before validation for the same reason: an operator who boots with a
broken ``WORKFLOW.md`` should be able to fix the file, and the process that
reports the failure should already have been watching it.

**Startup validation is fatal; every later validation failure is not.** SPEC 6.3
splits these explicitly: startup validation failure MUST fail startup, while
per-tick validation failure only skips dispatch and keeps reconciliation alive
(SPEC 14.2). SPEC 6.2 pushes the same way for reloads — an invalid reload MUST
NOT crash the service and MUST keep the last known good config. So exactly one
validation call in this module is fatal (:meth:`ServiceHost.start`), and the
reload path (:meth:`ServiceHost.reload_workflow`) deliberately does not validate
fatally at all.

Startup terminal workspace cleanup (SPEC 8.6) runs after validation and before
the first tick, and its failure is a logged warning rather than a startup
failure.

RLM note: ``main`` is a thin wrapper. The startup sequence is
:func:`start_service`, which returns a live :class:`ServiceHost`; a model driving
this system from a Python REPL can ``await start_service(...)``, inspect
``host.state`` and ``host.config``, and ``await host.aclose()`` without ever
spawning a subprocess.
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
from symphony.models import OrchestratorState

__all__ = [
    "EXIT_OK",
    "EXIT_RUNTIME_FAILURE",
    "EXIT_STARTUP_FAILURE",
    "EXIT_USAGE",
    "HostDeps",
    "ServiceHost",
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
# CONTRACTS.md fixes the signatures of workflow.loader, workflow.config,
# workflow.watcher, trackers.base and observability.logging, and this module
# uses those verbatim. It does *not* fix a surface for orchestrator.core,
# http.server, or an observability "start outputs" entry point, so the minimum
# this host needs from each is stated here and resolved lazily in HostDeps.
# --------------------------------------------------------------------------


class Orchestrator(Protocol):
    """What the host requires of ``symphony.orchestrator.core`` (SPEC 16.2-16.4)."""

    async def run(self) -> None:
        """Run the tick loop (SPEC 16.1 ``schedule_tick(0)`` + ``event_loop``).

        Returns only after :meth:`stop`. Returning unasked is an abnormal exit.
        """

    async def stop(self) -> None:
        """Stop accepting new dispatches and let in-flight work unwind."""

    async def reload(self, config: Any) -> None:
        """Re-apply a newly loaded :class:`ServiceConfig` to live behavior (SPEC 6.2)."""

    async def startup_terminal_workspace_cleanup(self) -> None:
        """Remove workspaces for terminal issues (SPEC 8.6). Failure is non-fatal."""


class Watcher(Protocol):
    """``symphony.workflow.watcher.WorkflowWatcher`` (CONTRACTS.md section 3)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class ObservabilityHandle(Protocol):
    """A started observability output that the host must later shut down."""

    async def stop(self) -> None: ...


# --------------------------------------------------------------------------
# Dependency seams
# --------------------------------------------------------------------------


@dataclass(slots=True)
class HostDeps:
    """Injection points for every collaborator the host wires together.

    Each field defaults to ``None``, meaning "resolve the real sibling module on
    first use". Lazy resolution is not stylistic: it keeps ``import symphony.cli``
    free of import-time coupling to modules that may be absent, half-written, or
    (for the HTTP server) an optional extension the operator never enabled.

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
        Callable[[Any, int | None, Callable[[], Any]], Awaitable[ObservabilityHandle | None]] | None
    ) = None
    build_orchestrator: Callable[[Any, OrchestratorState, Any], Orchestrator] | None = None
    build_snapshot: Callable[[OrchestratorState], Any] | None = None

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
        from symphony.trackers.base import build_adapter

        return build_adapter

    def resolved_build_watcher(self) -> Callable[[Path, Callable[[], Awaitable[None]]], Watcher]:
        if self.build_watcher is not None:
            return self.build_watcher
        from symphony.workflow.watcher import WorkflowWatcher

        return WorkflowWatcher

    def resolved_build_orchestrator(self) -> Callable[[Any, OrchestratorState, Any], Orchestrator]:
        if self.build_orchestrator is not None:
            return self.build_orchestrator
        from symphony.orchestrator.core import Orchestrator as _Orchestrator

        def factory(config: Any, state: OrchestratorState, tracker: Any) -> Orchestrator:
            return _Orchestrator(config=config, state=state, tracker=tracker)

        return factory

    def resolved_start_observability(
        self,
    ) -> Callable[[Any, int | None, Callable[[], Any]], Awaitable[ObservabilityHandle | None]]:
        return self.start_observability or _default_start_observability

    def resolved_build_snapshot(self) -> Callable[[OrchestratorState], Any]:
        if self.build_snapshot is not None:
            return self.build_snapshot
        from symphony.observability.snapshot import build_snapshot

        return build_snapshot


def _default_configure_logging() -> None:
    """Bring up the log sink (SPEC 13.2).

    CONTRACTS.md names ``get_logger``/``StructuredLogger`` but no configuration
    entry point, so ``configure_logging`` is called only if the observability
    module actually defines it. The stderr fallback exists because SPEC 13.2
    makes operator visibility of startup and validation failures a REQUIREMENT:
    a missing or unconfigured sink must not be the reason nobody sees why the
    service refused to boot.
    """
    try:
        from symphony.observability import logging as observability_logging
    except ImportError:
        _basic_stderr_logging()
        return

    configure = getattr(observability_logging, "configure_logging", None)
    if callable(configure):
        configure()
    else:
        _basic_stderr_logging()


def _basic_stderr_logging() -> None:
    import logging

    if not logging.getLogger().handlers:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")


async def _default_start_observability(
    config: Any,
    port_override: int | None,
    snapshot: Callable[[], Any],
) -> ObservabilityHandle | None:
    """Start observability outputs (SPEC 16.1, 13.7).

    For this implementation the only *startable* output is the OPTIONAL HTTP
    extension. It is enabled by a CLI ``--port`` argument or by ``server.port``
    in the front matter, and the CLI value wins when both are present
    (SPEC 13.7). Returning ``None`` means the extension is disabled, which is
    the conformant default.
    """
    port = port_override if port_override is not None else getattr(config, "server_port", None)
    if port is None:
        return None

    from symphony.http.server import start_server

    return await start_server(port=port, snapshot=snapshot)


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
    * ``signal.signal`` is only legal on the main thread; a non-main-thread host
      gets no handlers and is expected to call
      :meth:`ServiceHost.request_shutdown` itself.
    * The Windows ``ProactorEventLoop`` can park in a completion-port wait
      without executing Python bytecode, which is exactly when a pending
      C-level SIGINT handler cannot run. The caller therefore also runs
      :func:`_windows_signal_pump` to guarantee the interpreter regains control.
    * SIGBREAK (Ctrl+Break) exists only on Windows and is included there because
      it is the console signal that survives cases where SIGINT does not.
    """
    names: list[str] = ["SIGINT", "SIGTERM", "SIGBREAK"]
    restores: list[Callable[[], None]] = []

    for name in names:
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

    Constructed unstarted so that late-bound collaborators (the snapshot
    callable handed to observability before ``state`` exists) have a stable
    object to read from.
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

        self.config: Any = None
        self.state: OrchestratorState | None = None
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
        5. build the SPEC 4.1.8 initial state
        6. ``validate_dispatch_config()`` — **fatal on failure** (SPEC 6.3)
        7. ``startup_terminal_workspace_cleanup()`` — warn on failure (SPEC 8.6)

        Steps 3 and 4 precede step 6 exactly as the spec orders them, so the
        validation error in step 6 is emitted through an already-running
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
            self.state = self._build_initial_state(self.config)
            self._validate_for_startup()
            self.orchestrator = self.deps.resolved_build_orchestrator()(
                self.config, self.state, self.tracker
            )
            await self._startup_terminal_workspace_cleanup()
        except BaseException:
            await self.aclose()
            raise

        self.log.info("service started", outcome="completed")

    def _load_effective_config(self) -> Any:
        """SPEC 16.1 step 2. A load or build failure is fatal (SPEC 6.3, 14.1)."""
        definition = self.deps.resolved_load_workflow()(self.workflow_path)
        return self.deps.resolved_build_config()(definition)

    async def _start_observability(self) -> None:
        """SPEC 16.1 step 3.

        A failure here is logged as an operator-visible error and startup
        continues: SPEC 13.7 forbids the dashboard from becoming REQUIRED for
        orchestrator correctness, and SPEC 14.2 says dashboard failures do not
        crash the orchestrator.
        """
        try:
            self.observability = await self.deps.resolved_start_observability()(
                self.config, self.port_override, self.snapshot
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
        self.watcher = self.deps.resolved_build_watcher()(
            self.workflow_path, self.reload_workflow
        )
        await self.watcher.start()

    @staticmethod
    def _build_initial_state(config: Any) -> OrchestratorState:
        """SPEC 16.1 step 5 / SPEC 4.1.8. Remaining fields use their model defaults."""
        return OrchestratorState(
            poll_interval_ms=config.poll_interval_ms,
            max_concurrent_agents=config.max_concurrent_agents,
        )

    def _validate_for_startup(self) -> None:
        """SPEC 16.1 step 6 — the one fatal validation in the whole service.

        SPEC 6.3 is explicit that a startup validation failure fails startup,
        while the identical check on a later tick only skips dispatch
        (SPEC 14.2). Building the tracker adapter is part of the same check:
        SPEC 6.3 requires that the selected adapter *accept*
        ``tracker.provider`` after defaults and ``$VAR`` resolution, and the
        only honest way to assert that is to construct it.
        """
        self.deps.resolved_validate_dispatch_config()(self.config)
        self.tracker = self.deps.resolved_build_adapter()(
            self.config.tracker_kind, self.config.tracker_provider
        )

    async def _startup_terminal_workspace_cleanup(self) -> None:
        """SPEC 16.1 step 7 / SPEC 8.6 — runs before the first tick; warns on failure.

        SPEC 8.6 step 3 makes the terminal-issue fetch failure a warning that
        continues startup. Stale workspaces are an accumulation problem, not a
        correctness one, so no failure mode here is worth refusing to boot over.
        """
        cleanup = getattr(self.orchestrator, "startup_terminal_workspace_cleanup", None)
        if cleanup is None:
            return
        try:
            await cleanup()
        except Exception as exc:
            self.log.warning(
                "startup terminal workspace cleanup failed",
                outcome="failed",
                reason=_reason(exc),
                category=_category(exc),
            )

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
        try:
            config = self._load_effective_config()
        except Exception as exc:
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
            await self.orchestrator.reload(config)
        except Exception as exc:
            self.log.error(
                "workflow reload could not be applied to live behavior",
                outcome="failed",
                reason=_reason(exc),
                category=_category(exc),
            )
            return

        self.log.info("workflow reloaded", outcome="completed")

    # -- run / shutdown --------------------------------------------------

    def snapshot(self) -> Any:
        """Current runtime snapshot (SPEC 13.3), or ``None`` before state exists."""
        if self.state is None:
            return None
        try:
            return self.deps.resolved_build_snapshot()(self.state)
        except Exception:
            # SPEC 14.2: observability failures never propagate into the host.
            return None

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

        run_task: asyncio.Task[None] = asyncio.ensure_future(self.orchestrator.run())
        stop_task: asyncio.Task[bool] = asyncio.ensure_future(self._shutdown.wait())

        try:
            await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

            if run_task.done():
                stop_task.cancel()
                # Re-raise the orchestrator's exception, if any.
                run_task.result()
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
    workflow_path: str | Path | None = None,
    *,
    port: int | None = None,
    deps: HostDeps | None = None,
    grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
) -> ServiceHost:
    """Run the SPEC 16.1 startup sequence and return the live host.

    This is the seam the module docstring promises an RLM: it performs every
    startup step, leaves the service running, and hands back an object whose
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
        host = await start_service(
            workflow_path, port=port, deps=deps, grace_seconds=grace_seconds
        )
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
        return asyncio.run(
            _amain(workflow_path, port=port, deps=deps, grace_seconds=grace_seconds)
        )
    except KeyboardInterrupt:
        # A SIGINT that beat the handler into place, or one raised on Windows
        # between loop iterations. Operator-requested stop, so still success.
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
