"""Dynamic reload of ``WORKFLOW.md`` without restart (SPEC 6.2).

SPEC 6.2 makes dynamic reload REQUIRED, and pins two behaviors that this module
exists to guarantee:

1. *Invalid reloads MUST NOT crash the service.* The watcher owns a
   "last known good" slot. A load that raises never displaces it, never bumps
   the generation counter, and never fires ``on_change``; it emits an
   operator-visible error instead (SPEC 13.2) and leaves the service running on
   the previously applied effective configuration.
2. *Implementations SHOULD also re-validate defensively during runtime
   operations in case filesystem watch events are missed.* Editors that save by
   write-rename, and network filesystems generally, drop inotify-class events
   routinely. So change detection here is a content digest that any caller can
   poll on demand (:meth:`WorkflowWatcher.is_stale`,
   :meth:`WorkflowWatcher.reload`) with no dependency on the event stream. The
   event loop is an optimization layered on top of the poll, not the only path.

Boundary: this module detects change and re-derives the effective workflow. It
does not re-plumb the result. Applying reloaded config to future dispatch,
retry scheduling, reconciliation, hooks, and agent launches is the
orchestrator's job, which reads :meth:`WorkflowWatcher.current` from its
``on_change`` callback. In-flight agent sessions are deliberately not restarted
(SPEC 6.2 does not require it), and listener-owning extensions such as the HTTP
server port (SPEC 13.7) are not live-rebound here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from symphony.errors import SymphonyError, WorkflowError
from symphony.models import WorkflowDefinition

if TYPE_CHECKING:
    from symphony.workflow.config import ServiceConfig

__all__ = [
    "DEFAULT_DEBOUNCE_MS",
    "EffectiveWorkflow",
    "FileStamp",
    "ReloadOutcome",
    "ReloadStatus",
    "WorkflowWatcher",
    "load_effective_workflow",
]

DEFAULT_DEBOUNCE_MS = 200
"""Coalescing window for filesystem events.

Deliberately far below the ``watchfiles`` default of 1600 ms: a digest compare
already makes redundant events free, so the window only needs to be wide enough
to skip the truncate-then-write intermediate state of a non-atomic editor save.
"""

_LOGGER_NAME = "symphony.workflow.watcher"

T = TypeVar("T")


# --------------------------------------------------------------------------
# Operator-visible error surface (SPEC 13.1, 13.2)
# --------------------------------------------------------------------------


class _FallbackLogger:
    """Stable ``key=value`` rendering (SPEC 13.1) over the stdlib logger.

    ``symphony.observability.logging`` owns the real structured logger. This
    stands in when that module is unavailable, because a reload failure that
    cannot be reported is worse than one reported through a plainer sink
    (SPEC 13.2).
    """

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    @staticmethod
    def _render(msg: str, fields: dict[str, Any]) -> str:
        if not fields:
            return msg
        return msg + " " + " ".join(f"{k}={v}" for k, v in fields.items())

    def debug(self, msg: str, **fields: Any) -> None:
        self._log.debug(self._render(msg, fields))

    def info(self, msg: str, **fields: Any) -> None:
        self._log.info(self._render(msg, fields))

    def warning(self, msg: str, **fields: Any) -> None:
        self._log.warning(self._render(msg, fields))

    def error(self, msg: str, **fields: Any) -> None:
        self._log.error(self._render(msg, fields))


def _default_logger() -> Any:
    try:
        from symphony.observability.logging import get_logger

        return get_logger(_LOGGER_NAME)
    except Exception:
        return _FallbackLogger(_LOGGER_NAME)


# --------------------------------------------------------------------------
# Change detection
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileStamp:
    """Content fingerprint of the workflow file at one instant.

    Change is decided by ``digest``, not by ``mtime_ns``. Coarse mtime
    granularity (1 s on some filesystems) and write-rename saves both produce
    edits that an mtime comparison misses, and SPEC 6.2 requires that missed
    filesystem signals stay recoverable. ``mtime_ns`` and ``size`` are carried
    for operator diagnostics only.
    """

    exists: bool
    mtime_ns: int
    size: int
    digest: str

    @classmethod
    def of(cls, path: Path | str) -> FileStamp:
        """Stamp ``path``. An unreadable or absent file stamps as non-existent."""
        try:
            data = Path(path).read_bytes()
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError:
            return cls(exists=False, mtime_ns=0, size=0, digest="")
        return cls(
            exists=True,
            mtime_ns=mtime_ns,
            size=len(data),
            digest=hashlib.sha256(data).hexdigest(),
        )

    def same_content_as(self, other: FileStamp | None) -> bool:
        """True when ``other`` describes byte-identical content."""
        if other is None:
            return False
        return self.exists == other.exists and self.digest == other.digest


class ReloadStatus(StrEnum):
    """Outcome of one reload attempt."""

    UNCHANGED = "unchanged"
    APPLIED = "applied"
    FAILED = "failed"


@dataclass(frozen=True)
class ReloadOutcome(Generic[T]):
    """Result of :meth:`WorkflowWatcher.reload`.

    ``value`` is always the *effective* configuration after the attempt, so on
    :attr:`ReloadStatus.FAILED` it is the retained last known good rather than
    ``None`` (SPEC 6.2).
    """

    status: ReloadStatus
    generation: int
    value: T | None
    error: SymphonyError | None = None
    stamp: FileStamp | None = None

    @property
    def ok(self) -> bool:
        return self.status is not ReloadStatus.FAILED

    @property
    def applied(self) -> bool:
        return self.status is ReloadStatus.APPLIED


# --------------------------------------------------------------------------
# Default effective-configuration loader
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectiveWorkflow:
    """One applied unit of workflow state.

    SPEC 6.2 requires re-applying workflow config *and* prompt template
    together, so they travel as a pair and are swapped atomically.
    """

    definition: WorkflowDefinition
    config: ServiceConfig

    @property
    def prompt_template(self) -> str:
        return self.definition.prompt_template


def load_effective_workflow(path: Path) -> EffectiveWorkflow:
    """Re-run the SPEC 6.1 resolution pipeline end to end for ``path``.

    Imported lazily so that this module stays importable — and the defensive
    poll path stays usable — regardless of sibling module availability.
    """
    from symphony.workflow.config import build_config
    from symphony.workflow.loader import load_workflow

    definition = load_workflow(path)
    return EffectiveWorkflow(definition=definition, config=build_config(definition))


# --------------------------------------------------------------------------
# Watcher
# --------------------------------------------------------------------------


class WorkflowWatcher(Generic[T]):
    """Detects ``WORKFLOW.md`` changes and re-applies them without restart (SPEC 6.2).

    Two independent change-detection paths, by design:

    * ``start()`` runs a ``watchfiles`` loop over the *parent directory*
      (filtered to the target filename, so write-rename and delete-recreate
      saves are still seen).
    * ``reload()`` / ``is_stale()`` compare the on-disk digest on demand, with
      no event stream involved. SPEC 6.2 asks for this defensive path
      explicitly; call ``reload()`` before each dispatch tick, which also
      satisfies the SPEC 6.3 per-tick re-validation requirement.

    Both funnel through the same guarded apply, so the last-known-good
    invariant holds no matter which one fires.
    """

    def __init__(
        self,
        path: Path,
        on_change: Callable[[], Awaitable[None]],
        *,
        loader: Callable[[Path], T] = load_effective_workflow,  # type: ignore[assignment]
        on_error: Callable[[SymphonyError], None] | None = None,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        logger: Any | None = None,
    ) -> None:
        """``path`` and ``on_change`` are the CONTRACTS.md surface; the rest are
        injection seams with working defaults.

        ``on_change`` takes no arguments: the consumer pulls the new effective
        configuration from :meth:`current`, which keeps the callback signature
        stable as the effective type evolves.
        """
        self.path = Path(path)
        self._on_change = on_change
        self._loader = loader
        self._on_error = on_error
        self._debounce_ms = max(0, int(debounce_ms))
        self._log = logger if logger is not None else _default_logger()

        self._lock = asyncio.Lock()
        self._generation = 0
        self._value: T | None = None
        self._applied_stamp: FileStamp | None = None
        self._failed_stamp: FileStamp | None = None
        self._last_error: SymphonyError | None = None
        self._last_callback_error: SymphonyError | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    # -- introspection (kept flat and named for REPL/RLM drive) -------------

    def current(self) -> T | None:
        """Last known good effective configuration, or ``None`` before priming."""
        return self._value

    @property
    def generation(self) -> int:
        """Count of successful applies. Never advances on a failed reload."""
        return self._generation

    @property
    def primed(self) -> bool:
        return self._generation > 0

    @property
    def last_error(self) -> SymphonyError | None:
        """Error from the most recent failed *reload*, cleared by a success."""
        return self._last_error

    @property
    def last_callback_error(self) -> SymphonyError | None:
        """Error raised by ``on_change``. Tracked apart from :attr:`last_error`
        because the configuration itself did load and was applied."""
        return self._last_callback_error

    @property
    def healthy(self) -> bool:
        """True when the on-disk workflow last loaded cleanly."""
        return self._last_error is None

    @property
    def applied_stamp(self) -> FileStamp | None:
        """Stamp of the content behind :meth:`current`."""
        return self._applied_stamp

    @property
    def watching(self) -> bool:
        """True while the filesystem event loop is live."""
        return self._task is not None and not self._task.done()

    # -- change detection --------------------------------------------------

    async def is_stale(self) -> bool:
        """True when on-disk content differs from what is applied.

        The SPEC 6.2 defensive check. Independent of the event stream and safe
        to call on every tick; it costs one small read plus a digest.
        """
        stamp = await asyncio.to_thread(FileStamp.of, self.path)
        return not stamp.same_content_as(self._applied_stamp)

    async def prime(self) -> ReloadOutcome[T]:
        """Establish the initial last-known-good slot without firing ``on_change``.

        Startup has nothing to notify yet; SPEC 6.3 startup validation is the
        caller's gate on the result.
        """
        return await self._apply(force=True, notify=False)

    async def reload(self, *, force: bool = False) -> ReloadOutcome[T]:
        """Re-read and re-apply if the file changed; otherwise a no-op.

        Never raises for workflow content problems — that is the SPEC 6.2
        no-crash requirement. Failure is reported through the returned outcome,
        :attr:`last_error`, ``on_error``, and the log.
        """
        return await self._apply(force=force, notify=True)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Begin watching. Idempotent; primes the last-known-good slot first."""
        if self.watching:
            return
        if not self.primed:
            await self.prime()

        watch_dir = self.path.parent
        if not await asyncio.to_thread(watch_dir.is_dir):
            # Not fatal: the defensive reload() path still works, and SPEC 6.3
            # startup validation is what should fail the service here.
            self._log.error(
                "workflow watch not started outcome=failed",
                reason="watch_directory_missing",
                path=str(self.path),
            )
            return

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._watch_loop(self._stop_event), name="symphony-workflow-watch"
        )

    async def stop(self) -> None:
        """Stop watching and join the task. Idempotent."""
        stop_event, self._stop_event = self._stop_event, None
        task, self._task = self._task, None
        if stop_event is not None:
            stop_event.set()
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # -- internals ---------------------------------------------------------

    async def _apply(self, *, force: bool, notify: bool) -> ReloadOutcome[T]:
        async with self._lock:
            stamp = await asyncio.to_thread(FileStamp.of, self.path)

            if not force:
                if stamp.same_content_as(self._applied_stamp):
                    return ReloadOutcome(
                        ReloadStatus.UNCHANGED, self._generation, self._value, None, stamp
                    )
                if stamp.same_content_as(self._failed_stamp):
                    # Same broken bytes already reported. Re-running the loader
                    # and re-emitting would spam operators once per tick.
                    return ReloadOutcome(
                        ReloadStatus.FAILED,
                        self._generation,
                        self._value,
                        self._last_error,
                        stamp,
                    )

            try:
                value = await asyncio.to_thread(self._loader, self.path)
            except Exception as exc:
                error = _as_symphony_error(exc, "workflow reload failed")
                self._failed_stamp = stamp
                self._last_error = error
                self._report(error, "workflow reload outcome=failed")
                return ReloadOutcome(
                    ReloadStatus.FAILED, self._generation, self._value, error, stamp
                )

            self._value = value
            self._applied_stamp = stamp
            self._failed_stamp = None
            self._last_error = None
            self._generation += 1
            outcome: ReloadOutcome[T] = ReloadOutcome(
                ReloadStatus.APPLIED, self._generation, value, None, stamp
            )
            self._log.info(
                "workflow reload outcome=completed",
                path=str(self.path),
                generation=self._generation,
                digest=stamp.digest[:12],
            )

        # Notify outside the lock: a consumer is allowed to call back into
        # reload()/is_stale() from on_change without deadlocking.
        if notify:
            await self._notify()
        return outcome

    async def _notify(self) -> None:
        try:
            await self._on_change()
        except Exception as exc:
            error = _as_symphony_error(exc, "workflow reload callback failed")
            self._last_callback_error = error
            self._report(error, "workflow reload callback outcome=failed")

    def _report(self, error: SymphonyError, message: str) -> None:
        self._log.error(
            message,
            path=str(self.path),
            generation=self._generation,
            category=error.category,
            reason=error.message,
        )
        if self._on_error is None:
            return
        try:
            self._on_error(error)
        except Exception:
            self._log.error(
                "workflow reload error sink outcome=failed",
                path=str(self.path),
                category=error.category,
            )

    async def _watch_loop(self, stop_event: asyncio.Event) -> None:
        try:
            from watchfiles import awatch

            target = self.path.name

            def _matches(_change: Any, changed_path: str) -> bool:
                return Path(changed_path).name == target

            # Watch the containing directory, not the file: an editor that saves
            # by write-rename replaces the inode, and a watch bound to the old
            # file would go permanently deaf.
            async for _changes in awatch(
                self.path.parent,
                watch_filter=_matches,
                debounce=self._debounce_ms,
                stop_event=stop_event,
                recursive=False,
            ):
                await self._apply(force=False, notify=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The event stream died; the service keeps running on the last known
            # good config and the defensive reload() path still detects edits.
            error = _as_symphony_error(exc, "workflow watch stopped")
            self._report(error, "workflow watch outcome=failed")


def _as_symphony_error(exc: Exception, message: str) -> SymphonyError:
    """Normalize any loader/callback failure into the typed error surface.

    ``SymphonyError`` subclasses pass through so spec categories such as
    ``missing_workflow_file`` and ``workflow_parse_error`` (SPEC 5.5) reach the
    operator intact. Anything else is wrapped rather than propagated, because
    SPEC 6.2 forbids a reload failure from crashing the service.
    """
    if isinstance(exc, SymphonyError):
        return exc
    return WorkflowError(f"{message}: {exc}", exc_type=type(exc).__name__)
