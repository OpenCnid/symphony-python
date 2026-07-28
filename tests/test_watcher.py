"""Tests for ``symphony.workflow.watcher`` (SPEC 6.2, 14.4, 17.1).

Sibling modules (``workflow.loader``, ``workflow.config``) are being written
concurrently and are never imported here: the watcher takes an injected loader,
and these tests supply a deterministic fake. What is under test is the watcher's
own contract — change detection, the last-known-good slot, and the
operator-visible error surface — not the config pipeline behind it.

Determinism: every assertion about reload behavior drives ``reload()`` /
``is_stale()`` directly, so no test depends on filesystem event timing. The one
genuinely event-driven test is bounded by ``asyncio.wait_for`` and fails loudly
at the timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from symphony.errors import (
    MissingWorkflowFile,
    SymphonyError,
    WorkflowError,
    WorkflowParseError,
)
from symphony.workflow.watcher import (
    FileStamp,
    ReloadStatus,
    WorkflowWatcher,
    _FallbackLogger,
)

# Bound for the single event-driven test. Generous enough that a slow CI box
# does not flake, short enough that a broken watcher fails rather than hangs.
EVENT_TIMEOUT_S = 15.0


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class FakeLoader:
    """Stand-in for ``build_config(load_workflow(path))``.

    The "effective configuration" is just the file's text, which keeps every
    assertion exact. ``error`` forces the next and all subsequent loads to fail;
    ``calls`` proves how often the loader was actually invoked.
    """

    calls: list[Path] = field(default_factory=list)
    error: Exception | None = None

    def __call__(self, path: Path) -> str:
        self.calls.append(Path(path))
        if self.error is not None:
            raise self.error
        target = Path(path)
        if not target.exists():
            # Mirrors the SPEC 5.1 loader contract without importing it.
            raise MissingWorkflowFile("workflow file not found", path=str(target))
        return target.read_text(encoding="utf-8")


class Recorder:
    """``on_change`` callback: counts invocations and signals an asyncio.Event."""

    def __init__(self) -> None:
        self.count = 0
        self.fired = asyncio.Event()
        self.raises: Exception | None = None

    async def __call__(self) -> None:
        self.count += 1
        self.fired.set()
        if self.raises is not None:
            raise self.raises


class FakeLogger:
    """Captures structured log calls so operator visibility is assertable
    without depending on ``observability.logging``."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str, msg: str, fields: dict[str, Any]) -> None:
        self.records.append((level, msg, fields))

    def debug(self, msg: str, **fields: Any) -> None:
        self._record("debug", msg, fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._record("info", msg, fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._record("warning", msg, fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._record("error", msg, fields)

    def errors(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [r for r in self.records if r[0] == "error"]


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture
def workflow_file(tmp_path: Path) -> Path:
    path = tmp_path / "WORKFLOW.md"
    path.write_text("version-one", encoding="utf-8")
    return path


@dataclass
class Harness:
    watcher: WorkflowWatcher[str]
    loader: FakeLoader
    recorder: Recorder
    logger: FakeLogger
    errors: list[SymphonyError]
    path: Path


def make(path: Path, *, debounce_ms: int = 20) -> Harness:
    loader = FakeLoader()
    recorder = Recorder()
    logger = FakeLogger()
    errors: list[SymphonyError] = []
    watcher: WorkflowWatcher[str] = WorkflowWatcher(
        path,
        recorder,
        loader=loader,
        on_error=errors.append,
        debounce_ms=debounce_ms,
        logger=logger,
    )
    return Harness(watcher, loader, recorder, logger, errors, path)


# --------------------------------------------------------------------------
# SPEC 17.1 — changes are detected and re-applied without restart
# --------------------------------------------------------------------------


async def test_prime_establishes_last_known_good_without_notifying(
    workflow_file: Path,
) -> None:
    h = make(workflow_file)

    outcome = await h.watcher.prime()

    assert outcome.status is ReloadStatus.APPLIED
    assert h.watcher.current() == "version-one"
    assert h.watcher.generation == 1
    assert h.watcher.primed is True
    assert h.watcher.healthy is True
    # Startup has no downstream state to refresh yet.
    assert h.recorder.count == 0


async def test_reload_is_a_noop_when_content_is_unchanged(workflow_file: Path) -> None:
    h = make(workflow_file)
    await h.watcher.prime()

    outcome = await h.watcher.reload()

    assert outcome.status is ReloadStatus.UNCHANGED
    assert h.watcher.generation == 1
    assert h.recorder.count == 0
    # The loader ran exactly once: for the prime, not for the no-op reload.
    assert len(h.loader.calls) == 1


async def test_reload_applies_changed_content_and_notifies(workflow_file: Path) -> None:
    h = make(workflow_file)
    await h.watcher.prime()

    workflow_file.write_text("version-two", encoding="utf-8")
    outcome = await h.watcher.reload()

    assert outcome.status is ReloadStatus.APPLIED
    assert outcome.value == "version-two"
    assert h.watcher.current() == "version-two"
    assert h.watcher.generation == 2
    assert h.recorder.count == 1


async def test_force_reload_reapplies_byte_identical_content(workflow_file: Path) -> None:
    h = make(workflow_file)
    await h.watcher.prime()

    outcome = await h.watcher.reload(force=True)

    assert outcome.status is ReloadStatus.APPLIED
    assert h.watcher.generation == 2
    assert h.recorder.count == 1


# --------------------------------------------------------------------------
# SPEC 6.2 / 17.1 — invalid reload keeps last known good, does not crash
# --------------------------------------------------------------------------


async def test_invalid_reload_keeps_last_known_good_and_does_not_raise(
    workflow_file: Path,
) -> None:
    h = make(workflow_file)
    await h.watcher.prime()
    assert h.watcher.current() == "version-one"

    workflow_file.write_text("::: not valid yaml :::", encoding="utf-8")
    h.loader.error = WorkflowParseError("front matter is not valid YAML")

    outcome = await h.watcher.reload()

    assert outcome.status is ReloadStatus.FAILED
    assert outcome.ok is False
    # The retained configuration travels with the failed outcome, not None.
    assert outcome.value == "version-one"
    assert h.watcher.current() == "version-one"
    assert h.watcher.generation == 1, "a failed reload must not advance the generation"
    assert h.watcher.healthy is False
    assert h.recorder.count == 0, "downstream must not be told to re-apply a failed load"


async def test_invalid_reload_emits_operator_visible_error(workflow_file: Path) -> None:
    h = make(workflow_file)
    await h.watcher.prime()

    workflow_file.write_text("broken", encoding="utf-8")
    h.loader.error = WorkflowParseError("front matter is not valid YAML")
    await h.watcher.reload()

    # Typed error preserved end to end: sink, accessor, and log.
    assert [e.category for e in h.errors] == ["workflow_parse_error"]
    assert h.watcher.last_error is not None
    assert h.watcher.last_error.category == "workflow_parse_error"

    logged = h.logger.errors()
    assert len(logged) == 1
    _level, message, fields = logged[0]
    assert "outcome=failed" in message
    assert fields["category"] == "workflow_parse_error"
    assert fields["path"] == str(workflow_file)


async def test_non_symphony_exception_is_wrapped_not_propagated(
    workflow_file: Path,
) -> None:
    h = make(workflow_file)
    await h.watcher.prime()

    workflow_file.write_text("boom", encoding="utf-8")
    h.loader.error = ZeroDivisionError("unexpected")

    outcome = await h.watcher.reload()

    assert outcome.status is ReloadStatus.FAILED
    assert isinstance(outcome.error, WorkflowError)
    assert outcome.error.details["exc_type"] == "ZeroDivisionError"
    assert h.watcher.current() == "version-one"


async def test_missing_file_reload_keeps_last_known_good(workflow_file: Path) -> None:
    h = make(workflow_file)
    await h.watcher.prime()

    workflow_file.unlink()
    outcome = await h.watcher.reload()

    assert outcome.status is ReloadStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.category == "missing_workflow_file"
    assert h.watcher.current() == "version-one"
    assert h.watcher.generation == 1


async def test_repeated_reload_of_identical_bad_content_reports_once(
    workflow_file: Path,
) -> None:
    h = make(workflow_file)
    await h.watcher.prime()

    workflow_file.write_text("broken", encoding="utf-8")
    h.loader.error = WorkflowParseError("bad")

    for _ in range(3):
        outcome = await h.watcher.reload()
        assert outcome.status is ReloadStatus.FAILED

    # One loader attempt and one operator notification for one broken revision:
    # a per-tick defensive reload must not spam the operator.
    assert len(h.loader.calls) == 2, "prime + exactly one failing attempt"
    assert len(h.errors) == 1
    assert len(h.logger.errors()) == 1


async def test_watcher_recovers_after_a_failed_reload(workflow_file: Path) -> None:
    h = make(workflow_file)
    await h.watcher.prime()

    workflow_file.write_text("broken", encoding="utf-8")
    h.loader.error = WorkflowParseError("bad")
    await h.watcher.reload()
    assert h.watcher.healthy is False

    workflow_file.write_text("version-three", encoding="utf-8")
    h.loader.error = None
    outcome = await h.watcher.reload()

    assert outcome.status is ReloadStatus.APPLIED
    assert h.watcher.current() == "version-three"
    assert h.watcher.generation == 2
    assert h.watcher.last_error is None
    assert h.watcher.healthy is True
    assert h.recorder.count == 1


async def test_on_change_failure_does_not_lose_the_applied_config(
    workflow_file: Path,
) -> None:
    h = make(workflow_file)
    await h.watcher.prime()
    h.recorder.raises = RuntimeError("consumer blew up")

    workflow_file.write_text("version-two", encoding="utf-8")
    outcome = await h.watcher.reload()

    # The load succeeded; only the consumer's callback failed. These are
    # tracked separately so a bad consumer does not look like a bad workflow.
    assert outcome.status is ReloadStatus.APPLIED
    assert h.watcher.current() == "version-two"
    assert h.watcher.last_error is None
    assert h.watcher.last_callback_error is not None
    assert h.watcher.last_callback_error.details["exc_type"] == "RuntimeError"
    assert len(h.errors) == 1


# --------------------------------------------------------------------------
# SPEC 6.2 — defensive staleness check, independent of the event stream
# --------------------------------------------------------------------------


async def test_is_stale_detects_edits_without_any_event_stream(
    workflow_file: Path,
) -> None:
    h = make(workflow_file)
    assert await h.watcher.is_stale() is True, "unprimed watcher is stale by definition"

    await h.watcher.prime()
    assert await h.watcher.is_stale() is False

    workflow_file.write_text("version-two", encoding="utf-8")
    assert await h.watcher.is_stale() is True
    assert h.watcher.watching is False, "no watch task was ever started"

    await h.watcher.reload()
    assert await h.watcher.is_stale() is False


async def test_change_detection_survives_an_unchanged_mtime(workflow_file: Path) -> None:
    """Coarse mtime granularity and write-rename saves both defeat mtime
    comparison; SPEC 6.2's defensive path has to hold anyway."""
    h = make(workflow_file)
    await h.watcher.prime()
    original = os.stat(workflow_file)

    workflow_file.write_text("version-two-same-length!", encoding="utf-8")
    os.utime(workflow_file, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert os.stat(workflow_file).st_mtime_ns == original.st_mtime_ns

    assert await h.watcher.is_stale() is True
    outcome = await h.watcher.reload()
    assert outcome.status is ReloadStatus.APPLIED
    assert h.watcher.current() == "version-two-same-length!"


def test_file_stamp_of_missing_path_is_non_existent(tmp_path: Path) -> None:
    stamp = FileStamp.of(tmp_path / "nope.md")

    assert stamp.exists is False
    assert stamp.digest == ""
    assert stamp.same_content_as(None) is False
    # A missing file must never read as "same content" as a present empty file.
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    assert stamp.same_content_as(FileStamp.of(empty)) is False


# --------------------------------------------------------------------------
# Lifecycle and contract surface
# --------------------------------------------------------------------------


def test_constructor_matches_the_contracts_signature(workflow_file: Path) -> None:
    """CONTRACTS.md pins ``(path, on_change)`` positionally; siblings construct
    the watcher with exactly those two arguments."""
    params = list(inspect.signature(WorkflowWatcher.__init__).parameters.values())

    assert [p.name for p in params[:3]] == ["self", "path", "on_change"]
    assert all(p.kind is p.POSITIONAL_OR_KEYWORD for p in params[1:3])
    assert all(p.kind is p.KEYWORD_ONLY for p in params[3:])
    assert all(p.default is not p.empty for p in params[3:])

    watcher: WorkflowWatcher[Any] = WorkflowWatcher(workflow_file, Recorder())
    assert inspect.iscoroutinefunction(watcher.start)
    assert inspect.iscoroutinefunction(watcher.stop)


async def test_stop_is_idempotent_and_start_is_reentrant(workflow_file: Path) -> None:
    h = make(workflow_file)

    await h.watcher.start()
    assert h.watcher.watching is True
    first_task_alive = h.watcher.watching

    await h.watcher.start()  # second start must not spawn a second watch task
    assert h.watcher.watching is first_task_alive

    await h.watcher.stop()
    assert h.watcher.watching is False
    await h.watcher.stop()  # idempotent
    assert h.watcher.watching is False


async def test_start_without_a_watch_directory_reports_instead_of_raising(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "gone" / "WORKFLOW.md"
    h = make(missing)

    await h.watcher.start()

    assert h.watcher.watching is False
    assert any("watch not started" in msg for _lvl, msg, _f in h.logger.errors())
    # The defensive poll path stays usable even with no event loop running.
    assert await h.watcher.is_stale() is True


async def test_reload_from_within_on_change_does_not_deadlock(
    workflow_file: Path,
) -> None:
    """The consumer's callback legitimately re-enters the watcher (for example a
    dispatch preflight). Notification therefore happens outside the lock."""
    seen: list[ReloadStatus] = []
    holder: dict[str, WorkflowWatcher[str]] = {}

    async def reentrant() -> None:
        seen.append((await holder["watcher"].reload()).status)

    watcher: WorkflowWatcher[str] = WorkflowWatcher(
        workflow_file, reentrant, loader=FakeLoader(), logger=FakeLogger()
    )
    holder["watcher"] = watcher
    await watcher.prime()

    workflow_file.write_text("version-two", encoding="utf-8")
    outcome = await asyncio.wait_for(watcher.reload(), timeout=5.0)

    assert outcome.status is ReloadStatus.APPLIED
    # The re-entrant call sees the already-swapped state, so it is a clean no-op.
    assert seen == [ReloadStatus.UNCHANGED]
    assert watcher.current() == "version-two"


def test_fallback_logger_renders_stable_key_value_pairs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SPEC 13.1/13.2: reload failures must reach an operator even before
    ``observability.logging`` is wired in."""
    log = _FallbackLogger("symphony.workflow.watcher.test")

    with caplog.at_level(logging.ERROR, logger="symphony.workflow.watcher.test"):
        log.error("workflow reload outcome=failed", category="workflow_parse_error")

    assert caplog.messages == ["workflow reload outcome=failed category=workflow_parse_error"]


# --------------------------------------------------------------------------
# The one genuinely event-driven test (SPEC 17.1: detected without restart)
# --------------------------------------------------------------------------


async def test_filesystem_event_triggers_reload(workflow_file: Path) -> None:
    """End-to-end through the real ``watchfiles`` loop.

    The edit is pulsed because watch registration happens asynchronously inside
    the task, so a single write can land before the backend is listening. The
    assertion is still the bounded wait: a watcher that never fires fails at the
    timeout rather than hanging the suite.
    """
    h = make(workflow_file, debounce_ms=10)
    await h.watcher.start()
    assert h.watcher.generation == 1, "start() primes before watching"

    async def pulse() -> None:
        counter = 0
        while True:
            counter += 1
            workflow_file.write_text(f"edited-{counter}", encoding="utf-8")
            await asyncio.sleep(0.05)

    pulser = asyncio.create_task(pulse())
    try:
        await asyncio.wait_for(h.recorder.fired.wait(), timeout=EVENT_TIMEOUT_S)
    finally:
        pulser.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pulser
        await h.watcher.stop()

    assert h.recorder.count >= 1
    assert h.watcher.generation >= 2
    current = h.watcher.current()
    assert current is not None and current.startswith("edited-")
    assert h.watcher.watching is False
