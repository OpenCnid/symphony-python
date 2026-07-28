"""Structured logging — SPEC 13.1, 13.2.

Every log line is rendered as stable ``key=value`` phrasing (SPEC 13.1) so that
operators can grep it and a Recursive Language Model can parse it without a
custom reader::

    ts=2026-02-24T20:15:30.123Z level=info logger=symphony.orchestrator
    msg="agent attempt finished" issue_id=abc123 issue_identifier=MT-649
    outcome=completed

Issue context (``issue_id`` / ``issue_identifier``) and session context
(``session_id``) are attached once with :meth:`StructuredLogger.bind` rather
than threaded through every call site. Bound loggers are immutable and cheap:
:meth:`bind` copies a small tuple of pairs and shares the router, so a caller
can hold one logger per issue for the life of an attempt.

Sink failures never propagate (SPEC 13.2, 14.2, 17.6). A sink that raises is
reported *through the remaining sinks* — and, when no sink survives, through a
last-resort fallback stream — so a broken sink degrades visibility instead of
silently swallowing startup and dispatch failures (SPEC 13.2).

Field values are redacted by key and truncated by length: token values,
secrets, and large raw payloads never reach a sink (SPEC 13.1, 15.3).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from types import MappingProxyType
from typing import Any, Protocol, TextIO

__all__ = [
    "LogLevel",
    "LogRecord",
    "LogSink",
    "StreamSink",
    "ListSink",
    "LogRouter",
    "StructuredLogger",
    "REDACTED",
    "MAX_VALUE_CHARS",
    "get_logger",
    "get_router",
    "configure",
    "add_sink",
    "remove_sink",
    "set_level",
    "reset_logging",
    "render_record",
    "sink_name",
]


class LogLevel(IntEnum):
    """Severity ladder. Values follow the stdlib so they interoperate."""

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40


_LEVEL_NAMES = {
    LogLevel.DEBUG: "debug",
    LogLevel.INFO: "info",
    LogLevel.WARNING: "warning",
    LogLevel.ERROR: "error",
}

REDACTED = "[redacted]"

# SPEC 13.1: "Avoid logging large raw payloads unless necessary." A single
# field value is clipped at this many characters before rendering.
MAX_VALUE_CHARS = 512

_TRUNCATION_SUFFIX = "...[truncated]"

# SPEC 13.1 REQUIRED context fields lead every line so the phrasing stays
# stable regardless of the order a caller happened to bind them in.
_CONTEXT_ORDER = ("issue_id", "issue_identifier", "session_id")

# SPEC 15.3: never log API tokens or secret env values. Matching is by key,
# because a value-shaped heuristic would both miss real secrets and redact
# legitimate identifiers. Plural counters (``input_tokens``) are deliberately
# not matched by the ``_token`` suffix rule.
_SECRET_EXACT = frozenset(
    {
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "pat",
        "secret",
        "token",
    }
)
_SECRET_SUBSTRINGS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "passwd",
    "password",
    "private_key",
    "secret",
)
_SECRET_SUFFIXES = ("_token", "_key", "_pat")
_SECRET_SUFFIX_EXCEPTIONS = ("workspace_key", "idempotency_key", "sort_key", "cache_key")

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_./:@+-]+$")
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9_.]")


def is_secret_key(key: str) -> bool:
    """Report whether a field name must be redacted (SPEC 15.3).

    Exposed because sibling modules building their own field maps need the
    same answer this module uses.
    """
    lowered = key.strip().lower()
    if lowered in _SECRET_EXACT:
        return True
    if any(part in lowered for part in _SECRET_SUBSTRINGS):
        return True
    if lowered in _SECRET_SUFFIX_EXCEPTIONS:
        return False
    return lowered.endswith(_SECRET_SUFFIXES)


# --------------------------------------------------------------------------
# Records and rendering (SPEC 13.1)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogRecord:
    """One structured log event (SPEC 13.1)."""

    level: str
    logger: str
    message: str
    fields: Mapping[str, Any]
    timestamp: datetime

    def render(self) -> str:
        """Render the stable ``key=value`` line."""
        return render_record(self)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping, for sinks that ship structured records."""
        return {
            "ts": _format_timestamp(self.timestamp),
            "level": self.level,
            "logger": self.logger,
            "msg": self.message,
            "fields": {k: _redact(k, v) for k, v in _ordered_fields(self.fields)},
        }


def _format_timestamp(value: datetime) -> str:
    """RFC3339 with millisecond precision, always UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return f"{value.strftime('%Y-%m-%dT%H:%M:%S')}.{value.microsecond // 1000:03d}Z"


def _ordered_fields(fields: Mapping[str, Any]) -> Iterator[tuple[str, Any]]:
    """Yield REQUIRED context fields first, then the rest in insertion order."""
    for key in _CONTEXT_ORDER:
        if key in fields:
            yield key, fields[key]
    for key, value in fields.items():
        if key not in _CONTEXT_ORDER:
            yield key, value


def _redact(key: str, value: Any) -> Any:
    return REDACTED if is_secret_key(key) else value


def _truncate(text: str) -> str:
    if len(text) <= MAX_VALUE_CHARS:
        return text
    return text[:MAX_VALUE_CHARS] + _TRUNCATION_SUFFIX


def _quote(text: str) -> str:
    """Always-quoted rendering; ``json.dumps`` also escapes newlines."""
    return json.dumps(_truncate(text), ensure_ascii=False)


def _format_key(key: str) -> str:
    cleaned = _UNSAFE_KEY_CHARS.sub("_", str(key).strip())
    return cleaned or "field"


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        return _format_timestamp(value)
    if isinstance(value, str):
        text = _truncate(value)
        return text if text and _SAFE_TOKEN.match(text) else _quote(value)
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        # SPEC 13.1: raw payloads are summarized, never dumped wholesale.
        return _quote(_summarize_collection(value))
    inner = getattr(value, "value", None)
    if isinstance(inner, str | int | float):  # Enum and friends
        return _format_scalar(inner)
    return _quote(str(value))


def _summarize_collection(value: Any) -> str:
    if isinstance(value, Mapping):
        keys = ",".join(sorted(_format_key(str(k)) for k in value))
        return f"<map n={len(value)} keys={keys}>"
    return f"<seq n={len(value)}>"


def _format_value(key: str, value: Any) -> str:
    if is_secret_key(key):
        return REDACTED
    return _format_scalar(value)


def render_record(record: LogRecord) -> str:
    """Render a record as the SPEC 13.1 ``key=value`` line."""
    parts = [
        f"ts={_format_timestamp(record.timestamp)}",
        f"level={record.level}",
        f"logger={_format_scalar(record.logger)}",
        f"msg={_quote(record.message)}",
    ]
    parts.extend(
        f"{_format_key(key)}={_format_value(key, value)}"
        for key, value in _ordered_fields(record.fields)
    )
    return " ".join(parts)


# --------------------------------------------------------------------------
# Sinks (SPEC 13.2)
# --------------------------------------------------------------------------


class LogSink(Protocol):
    """Anything with ``emit``. Bare callables taking a record also work."""

    def emit(self, record: LogRecord) -> None: ...


def sink_name(sink: Any) -> str:
    """Best-effort operator-facing name for a sink."""
    for attr in ("name", "__name__"):
        value = getattr(sink, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(sink).__name__


class StreamSink:
    """Write rendered lines to a text stream (stderr by default).

    SPEC 13.2 requires startup/validation/dispatch failures to be visible
    without a debugger; an always-on stderr sink is the cheapest way to hold
    that guarantee. The stream is resolved at emit time so test harnesses that
    swap ``sys.stderr`` are honored.
    """

    def __init__(self, stream: TextIO | None = None, *, name: str = "stream") -> None:
        self._stream = stream
        self.name = name

    @property
    def stream(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stderr

    def emit(self, record: LogRecord) -> None:
        stream = self.stream
        stream.write(render_record(record) + "\n")
        stream.flush()


class ListSink:
    """Collect records in memory. Intended for tests and the RLM surface."""

    def __init__(self, *, name: str = "list") -> None:
        self.name = name
        self.records: list[LogRecord] = []

    def emit(self, record: LogRecord) -> None:
        self.records.append(record)

    @property
    def lines(self) -> list[str]:
        return [render_record(r) for r in self.records]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def clear(self) -> None:
        self.records.clear()


# --------------------------------------------------------------------------
# Router (SPEC 13.2, 14.2)
# --------------------------------------------------------------------------


DEFAULT_MAX_CONSECUTIVE_FAILURES = 5


class LogRouter:
    """Fan a record out to every sink, surviving sink failures (SPEC 13.2).

    Failure policy, in order:

    1. A raising sink never propagates its exception to the caller — logging
       must not be able to abort orchestration (SPEC 14.2, 17.6).
    2. The failure is reported as a ``warning`` record through every sink that
       did *not* fail on this record, so the loss is operator-visible rather
       than silent.
    3. When no sink survives, both the original line and the failure notice go
       to the fallback stream (``sys.__stderr__`` unless injected).
    4. After ``max_consecutive_failures`` back-to-back failures a sink is
       disabled, so a permanently broken sink cannot turn into a log storm. A
       single successful emit resets the counter.
    """

    def __init__(
        self,
        sinks: Iterable[Any] = (),
        *,
        level: LogLevel | str | int = LogLevel.INFO,
        clock: Callable[[], datetime] | None = None,
        fallback: TextIO | None = None,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self._lock = threading.RLock()
        self._sinks: list[Any] = list(sinks)
        self._disabled: list[Any] = []
        self._failures: dict[int, int] = {}
        self._level = parse_level(level)
        self._clock = clock
        self._fallback = fallback
        self._max_consecutive_failures = max(1, int(max_consecutive_failures))
        self._reporting = threading.local()

    # -- configuration -----------------------------------------------------

    @property
    def level(self) -> LogLevel:
        return self._level

    @level.setter
    def level(self, value: LogLevel | str | int) -> None:
        self._level = parse_level(value)

    @property
    def sinks(self) -> tuple[Any, ...]:
        with self._lock:
            return tuple(self._sinks)

    @property
    def disabled_sinks(self) -> tuple[Any, ...]:
        """Sinks removed after repeated failures (SPEC 13.2)."""
        with self._lock:
            return tuple(self._disabled)

    def add_sink(self, sink: Any) -> None:
        with self._lock:
            self._sinks.append(sink)
            self._failures.pop(id(sink), None)

    def remove_sink(self, sink: Any) -> bool:
        with self._lock:
            for bucket in (self._sinks, self._disabled):
                for index, existing in enumerate(bucket):
                    if existing is sink:
                        bucket.pop(index)
                        self._failures.pop(id(sink), None)
                        return True
        return False

    def set_sinks(self, sinks: Iterable[Any]) -> None:
        with self._lock:
            self._sinks = list(sinks)
            self._disabled.clear()
            self._failures.clear()

    def is_enabled(self, level: LogLevel) -> bool:
        return level >= self._level

    def now(self) -> datetime:
        """Current time, injectable for deterministic tests."""
        if self._clock is not None:
            return self._clock()
        return datetime.now(UTC)

    # -- emission ----------------------------------------------------------

    def emit(self, record: LogRecord) -> None:
        """Deliver *record* to every active sink; never raises."""
        with self._lock:
            targets = list(self._sinks)

        failed: list[tuple[Any, BaseException]] = []
        survivors: list[Any] = []
        for sink in targets:
            try:
                _deliver(sink, record)
            except BaseException as exc:  # a sink must never break the caller
                failed.append((sink, exc))
            else:
                survivors.append(sink)
                self._note_success(sink)

        if not failed:
            return

        if not survivors:
            # Nothing accepted the record; keep it operator-visible anyway.
            self._write_fallback(render_record(record))

        for sink, error in failed:
            self._report_failure(sink, error, survivors)

    def _note_success(self, sink: Any) -> None:
        with self._lock:
            self._failures.pop(id(sink), None)

    def _report_failure(self, sink: Any, exc: BaseException, survivors: list[Any]) -> None:
        with self._lock:
            count = self._failures.get(id(sink), 0) + 1
            self._failures[id(sink)] = count
            disable = count >= self._max_consecutive_failures
            if disable:
                for index, existing in enumerate(self._sinks):
                    if existing is sink:
                        self._disabled.append(self._sinks.pop(index))
                        break

        notice = LogRecord(
            level=_LEVEL_NAMES[LogLevel.WARNING],
            logger=__name__,
            message="log sink disabled" if disable else "log sink failed",
            fields=MappingProxyType(
                {
                    "sink": sink_name(sink),
                    "outcome": "disabled" if disable else "failed",
                    "reason": _describe_exception(exc),
                    "consecutive_failures": count,
                }
            ),
            timestamp=self.now(),
        )
        self._deliver_notice(notice, survivors)

    def _deliver_notice(self, notice: LogRecord, survivors: list[Any]) -> None:
        """Report through remaining sinks; fall back when none survive."""
        if getattr(self._reporting, "active", False) or not survivors:
            self._write_fallback(render_record(notice))
            return

        self._reporting.active = True
        try:
            delivered = False
            for sink in survivors:
                try:
                    _deliver(sink, notice)
                except BaseException:
                    continue
                else:
                    delivered = True
            if not delivered:
                self._write_fallback(render_record(notice))
        finally:
            self._reporting.active = False

    def _write_fallback(self, text: str) -> None:
        stream = self._fallback if self._fallback is not None else sys.__stderr__
        if stream is None:
            return
        try:
            stream.write(text + "\n")
            stream.flush()
        except BaseException:
            # There is nowhere left to report to; dropping is the only option
            # that still satisfies "do not crash orchestration" (SPEC 14.2).
            pass


def _deliver(sink: Any, record: LogRecord) -> None:
    emit = getattr(sink, "emit", None)
    if emit is not None:
        emit(record)
    else:
        sink(record)


def _describe_exception(exc: BaseException) -> str:
    """Short, secret-free description of a sink failure."""
    text = str(exc).strip()
    label = type(exc).__name__
    return f"{label}: {text}" if text else label


def parse_level(value: LogLevel | str | int) -> LogLevel:
    """Coerce a level name or number to :class:`LogLevel` (defaults to INFO)."""
    if isinstance(value, LogLevel):
        return value
    if isinstance(value, int):
        try:
            return LogLevel(value)
        except ValueError:
            return LogLevel.INFO
    name = str(value).strip().lower()
    aliases = {"warn": LogLevel.WARNING, "err": LogLevel.ERROR, "fatal": LogLevel.ERROR}
    if name in aliases:
        return aliases[name]
    for level, label in _LEVEL_NAMES.items():
        if name == label:
            return level
    return LogLevel.INFO


# --------------------------------------------------------------------------
# Logger (SPEC 13.1)
# --------------------------------------------------------------------------


class StructuredLogger:
    """Immutable logger carrying bound context (SPEC 13.1).

    Instances are cheap and safe to share: ``bind`` never mutates the receiver,
    so one logger per issue can be held for the length of an attempt and passed
    across tasks.
    """

    __slots__ = ("_name", "_pairs", "_router")

    def __init__(
        self,
        name: str,
        *,
        router: LogRouter | None = None,
        fields: tuple[tuple[str, Any], ...] = (),
    ) -> None:
        self._name = name
        self._router = router
        self._pairs = fields

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StructuredLogger(name={self._name!r}, fields={dict(self._pairs)!r})"

    @property
    def name(self) -> str:
        return self._name

    @property
    def fields(self) -> Mapping[str, Any]:
        """Read-only view of the bound context."""
        return MappingProxyType(dict(self._pairs))

    @property
    def router(self) -> LogRouter:
        return self._router if self._router is not None else get_router()

    def bind(self, **fields: Any) -> StructuredLogger:
        """Return a new logger with *fields* merged into the bound context.

        Rebinding an existing key keeps that key's original position so the
        rendered phrasing stays stable (SPEC 13.1). The receiver is untouched.
        """
        if not fields:
            return self
        merged: dict[str, Any] = dict(self._pairs)
        merged.update(fields)
        return StructuredLogger(self._name, router=self._router, fields=tuple(merged.items()))

    def unbind(self, *keys: str) -> StructuredLogger:
        """Return a new logger without *keys* bound."""
        drop = set(keys)
        if not drop:
            return self
        kept = tuple((k, v) for k, v in self._pairs if k not in drop)
        return StructuredLogger(self._name, router=self._router, fields=kept)

    def log(self, level: LogLevel | str | int, msg: str, **fields: Any) -> None:
        """Emit one record at *level*; never raises (SPEC 13.2, 14.2)."""
        resolved = parse_level(level)
        router = self.router
        if not router.is_enabled(resolved):
            return
        merged: dict[str, Any] = dict(self._pairs)
        merged.update(fields)
        record = LogRecord(
            level=_LEVEL_NAMES[resolved],
            logger=self._name,
            message=str(msg),
            fields=MappingProxyType(merged),
            timestamp=router.now(),
        )
        router.emit(record)

    def debug(self, msg: str, **fields: Any) -> None:
        self.log(LogLevel.DEBUG, msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self.log(LogLevel.INFO, msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self.log(LogLevel.WARNING, msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self.log(LogLevel.ERROR, msg, **fields)


# --------------------------------------------------------------------------
# Module-level default router
# --------------------------------------------------------------------------


_ROUTER_LOCK = threading.RLock()
_ROUTER: LogRouter | None = None
_LOGGERS: dict[str, StructuredLogger] = {}

LOG_LEVEL_ENV = "SYMPHONY_LOG_LEVEL"


def _env_level() -> LogLevel:
    """Read the level at call time, so the environment can change under tests."""
    return parse_level(os.environ.get(LOG_LEVEL_ENV, "info"))


def get_router() -> LogRouter:
    """Return the process-wide router, creating the stderr default on demand."""
    global _ROUTER
    with _ROUTER_LOCK:
        if _ROUTER is None:
            _ROUTER = LogRouter([StreamSink(name="stderr")], level=_env_level())
        return _ROUTER


def configure(
    *,
    sinks: Iterable[Any] | None = None,
    level: LogLevel | str | int | None = None,
    clock: Callable[[], datetime] | None = None,
    fallback: TextIO | None = None,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
) -> LogRouter:
    """Install a fresh default router. Returns it for direct inspection."""
    global _ROUTER
    with _ROUTER_LOCK:
        _ROUTER = LogRouter(
            sinks if sinks is not None else [StreamSink(name="stderr")],
            level=level if level is not None else _env_level(),
            clock=clock,
            fallback=fallback,
            max_consecutive_failures=max_consecutive_failures,
        )
        return _ROUTER


def reset_logging() -> None:
    """Drop the default router and the logger cache (test hygiene)."""
    global _ROUTER
    with _ROUTER_LOCK:
        _ROUTER = None
        _LOGGERS.clear()


def add_sink(sink: Any) -> None:
    get_router().add_sink(sink)


def remove_sink(sink: Any) -> bool:
    return get_router().remove_sink(sink)


def set_level(level: LogLevel | str | int) -> None:
    get_router().level = parse_level(level)


def get_logger(name: str) -> StructuredLogger:
    """Return the logger for *name* (SPEC 13.1).

    Unbound loggers are cached per name, so repeated module-level calls are
    free. The returned logger resolves the default router lazily, which means
    a later :func:`configure` still applies to loggers created before it.
    """
    key = str(name)
    with _ROUTER_LOCK:
        logger = _LOGGERS.get(key)
        if logger is None:
            logger = StructuredLogger(key)
            _LOGGERS[key] = logger
        return logger
