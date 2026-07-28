"""Runtime snapshot — SPEC 13.3, 13.5, 13.7.2.

:func:`build_snapshot` renders the orchestrator's in-memory state as a plain
JSON-safe ``dict``. Nothing in it is a live object: every value is a string,
number, bool, ``None``, list, or dict, so an HTTP handler can serialize it and
a Recursive Language Model can slice it out of a REPL without special
accessors. Top-level keys match the SPEC 13.7.2 ``GET /api/v1/state`` shape
exactly.

The snapshot is a *read*, not a tick. In particular ``codex_totals``
``seconds_running`` is recomputed on every call as the cumulative runtime of
ended sessions plus the elapsed time of each currently-running entry, derived
from its ``started_at`` (SPEC 13.5). No background ticking is required, and
building a snapshot never mutates :class:`~symphony.models.OrchestratorState`.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from symphony.models import ClaimState, OrchestratorState, RetryEntry, RunningEntry

__all__ = [
    "MAX_MESSAGE_CHARS",
    "MAX_RECENT_EVENTS",
    "SNAPSHOT_KEYS",
    "RUNNING_ROW_KEYS",
    "RETRY_ROW_KEYS",
    "build_snapshot",
    "build_issue_detail",
    "live_seconds_running",
    "elapsed_seconds",
    "to_rfc3339",
    "json_safe",
]

# SPEC 13.1 / 15.3: agent-authored text is summarized, never echoed wholesale.
MAX_MESSAGE_CHARS = 500

# Recent-event history is a debugging aid; keep the newest slice bounded so a
# long-running session cannot make one API response unbounded.
MAX_RECENT_EVENTS = 20

_TRUNCATION_SUFFIX = "..."

#: Exact top-level keys of :func:`build_snapshot` (SPEC 13.7.2).
SNAPSHOT_KEYS = (
    "generated_at",
    "counts",
    "running",
    "retrying",
    "codex_totals",
    "rate_limits",
)

#: Exact keys of each ``running`` row (SPEC 13.3, 13.7.2).
RUNNING_ROW_KEYS = (
    "issue_id",
    "issue_identifier",
    "issue_url",
    "state",
    "session_id",
    "turn_count",
    "last_event",
    "last_message",
    "started_at",
    "last_event_at",
    "tokens",
)

#: Exact keys of each ``retrying`` row (SPEC 13.3, 13.7.2).
RETRY_ROW_KEYS = (
    "issue_id",
    "issue_identifier",
    "issue_url",
    "attempt",
    "due_at",
    "error",
)


# --------------------------------------------------------------------------
# Time and JSON helpers
# --------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """Interpret a naive datetime as UTC rather than local time."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_rfc3339(value: datetime | None) -> str | None:
    """Render a timestamp as ``2026-02-24T20:15:30Z`` (SPEC 13.7.2)."""
    if value is None:
        return None
    return _as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def elapsed_seconds(started_at: datetime | None, now: datetime | None = None) -> float:
    """Seconds between *started_at* and *now*, never negative.

    A clock that steps backwards (NTP correction, a caller passing an earlier
    ``now``) must not subtract from aggregate runtime, so the floor is zero.
    """
    if started_at is None:
        return 0.0
    reference = _as_utc(now) if now is not None else datetime.now(UTC)
    return max(0.0, (reference - _as_utc(started_at)).total_seconds())


def json_safe(value: Any, *, _depth: int = 0) -> Any:
    """Coerce arbitrary values into JSON-serializable ones.

    Used for pass-through payloads (rate limits, orchestrator-authored recent
    events) whose exact shape this module does not own.
    """
    if _depth > 6:
        return str(value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime):
        return to_rfc3339(value)
    if isinstance(value, Mapping):
        return {str(k): json_safe(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [json_safe(v, _depth=_depth + 1) for v in value]
    inner = getattr(value, "value", None)
    if isinstance(inner, bool | int | float | str):  # Enum members
        return inner
    return str(value)


def _clip(text: Any, limit: int = MAX_MESSAGE_CHARS) -> str | None:
    if text is None:
        return None
    rendered = text if isinstance(text, str) else str(text)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + _TRUNCATION_SUFFIX


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# SPEC 13.5 — live runtime aggregate
# --------------------------------------------------------------------------


def live_seconds_running(state: OrchestratorState, now: datetime | None = None) -> float:
    """Aggregate runtime seconds as of *now* (SPEC 13.3, 13.5).

    Cumulative seconds already banked for ended sessions
    (``state.codex_totals.seconds_running``, credited by the orchestrator when
    a session ends) plus the elapsed time of every entry still in ``running``,
    derived from its ``started_at``. Computing this on read is what makes
    continuous background ticking unnecessary (SPEC 13.5).
    """
    reference = _as_utc(now) if now is not None else datetime.now(UTC)
    total = float(state.codex_totals.seconds_running)
    for entry in state.running.values():
        total += elapsed_seconds(entry.started_at, reference)
    return total


# --------------------------------------------------------------------------
# SPEC 13.7.2 — GET /api/v1/state
# --------------------------------------------------------------------------


def _tokens(entry: RunningEntry) -> dict[str, int]:
    session = entry.session
    return {
        "input_tokens": _int(getattr(session, "codex_input_tokens", 0)),
        "output_tokens": _int(getattr(session, "codex_output_tokens", 0)),
        "total_tokens": _int(getattr(session, "codex_total_tokens", 0)),
    }


def _running_row(issue_id: str, entry: RunningEntry) -> dict[str, Any]:
    session = entry.session
    issue = entry.issue
    return {
        "issue_id": issue_id,
        "issue_identifier": entry.identifier or getattr(issue, "identifier", None),
        # SPEC 13.3: include the tracker-provided issue URL when available.
        "issue_url": getattr(issue, "url", None),
        "state": getattr(issue, "state", None),
        "session_id": json_safe(getattr(session, "session_id", None)),
        "turn_count": _int(getattr(session, "turn_count", 0)),
        "last_event": json_safe(getattr(session, "last_codex_event", None)),
        "last_message": _clip(getattr(session, "last_codex_message", None)) or "",
        "started_at": to_rfc3339(entry.started_at),
        "last_event_at": to_rfc3339(getattr(session, "last_codex_timestamp", None)),
        "tokens": _tokens(entry),
    }


def _due_at(entry: RetryEntry, now: datetime, monotonic_ms: float) -> str | None:
    """Convert the monotonic ``due_at_ms`` (SPEC 4.1.7) into wall time.

    ``due_at_ms`` is a monotonic reading, so it is only meaningful as a delta
    against a monotonic reading taken at the same moment as *now*.
    """
    try:
        delta_ms = float(entry.due_at_ms) - float(monotonic_ms)
    except (TypeError, ValueError):
        return None
    return to_rfc3339(now + timedelta(milliseconds=delta_ms))


def _retry_row(entry: RetryEntry, now: datetime, monotonic_ms: float) -> dict[str, Any]:
    return {
        "issue_id": json_safe(entry.issue_id),
        "issue_identifier": json_safe(entry.identifier),
        # A retry entry carries no Issue, so the tracker URL is unavailable
        # here; SPEC 13.3 requires it only "when available".
        "issue_url": None,
        "attempt": _int(entry.attempt),
        "due_at": _due_at(entry, now, monotonic_ms),
        "error": _clip(entry.error),
    }


def build_snapshot(
    state: OrchestratorState,
    *,
    now: datetime | None = None,
    monotonic_ms: float | None = None,
) -> dict[str, Any]:
    """Render the SPEC 13.7.2 ``/api/v1/state`` document (SPEC 13.3).

    Pure with respect to *state*: no field is mutated and every nested value
    is copied, so a caller may edit the result freely. ``now`` and
    ``monotonic_ms`` are injectable to keep callers and tests deterministic.
    """
    reference = _as_utc(now) if now is not None else datetime.now(UTC)
    mono_ms = monotonic_ms if monotonic_ms is not None else time.monotonic() * 1000.0

    running = [_running_row(issue_id, entry) for issue_id, entry in state.running.items()]
    retrying = [_retry_row(entry, reference, mono_ms) for entry in state.retry_attempts.values()]

    totals = state.codex_totals
    return {
        "generated_at": to_rfc3339(reference),
        "counts": {"running": len(running), "retrying": len(retrying)},
        "running": running,
        "retrying": retrying,
        "codex_totals": {
            "input_tokens": _int(totals.input_tokens),
            "output_tokens": _int(totals.output_tokens),
            "total_tokens": _int(totals.total_tokens),
            # SPEC 13.5: a live aggregate at render time, not a stored counter.
            "seconds_running": round(live_seconds_running(state, reference), 1),
        },
        "rate_limits": json_safe(state.codex_rate_limits),
    }


# --------------------------------------------------------------------------
# SPEC 13.7.2 — GET /api/v1/<issue_identifier>
# --------------------------------------------------------------------------


def _matches(candidate: str | None, wanted: str) -> bool:
    return candidate is not None and candidate.strip().lower() == wanted


def _resolve(
    state: OrchestratorState, identifier: str
) -> tuple[str, RunningEntry | None, RetryEntry | None] | None:
    """Find an issue by identifier, falling back to issue id.

    The HTTP route is ``/api/v1/<issue_identifier>`` (SPEC 13.7.2), but the id
    is accepted too: both are stable keys and an operator debugging from logs
    may have either.
    """
    for issue_id, entry in state.running.items():
        if issue_id == identifier or entry.identifier == identifier:
            return issue_id, entry, state.retry_attempts.get(issue_id)
    for issue_id, retry in state.retry_attempts.items():
        if issue_id == identifier or retry.identifier == identifier:
            return issue_id, None, retry
    if identifier in state.claimed or identifier in state.completed:
        return identifier, None, None

    wanted = identifier.strip().lower()
    if not wanted:
        return None
    for issue_id, entry in state.running.items():
        if _matches(entry.identifier, wanted) or _matches(issue_id, wanted):
            return issue_id, entry, state.retry_attempts.get(issue_id)
    for issue_id, retry in state.retry_attempts.items():
        if _matches(retry.identifier, wanted) or _matches(issue_id, wanted):
            return issue_id, None, retry
    return None


def _status(state: OrchestratorState, issue_id: str) -> str:
    claim = state.claim_state(issue_id)
    if claim is ClaimState.RUNNING:
        return "running"
    if claim is ClaimState.RETRY_QUEUED:
        return "retrying"
    if claim is ClaimState.CLAIMED:
        return "claimed"
    if issue_id in state.completed:
        return "completed"
    return "unknown"


def _recent_events(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalize orchestrator-authored event rows into ``{at, event, message}``."""
    rows: list[dict[str, Any]] = []
    for raw in list(events)[-MAX_RECENT_EVENTS:]:
        if isinstance(raw, Mapping):
            at = raw.get("at", raw.get("timestamp"))
            event = raw.get("event")
            message = raw.get("message", raw.get("text"))
        else:
            at = getattr(raw, "timestamp", None)
            event = getattr(raw, "event", None)
            message = getattr(raw, "message", None)
        rows.append(
            {
                "at": to_rfc3339(at) if isinstance(at, datetime) else json_safe(at),
                "event": json_safe(event),
                "message": _clip(message) if message is not None else None,
            }
        )
    return rows


def _running_detail(entry: RunningEntry) -> dict[str, Any]:
    session = entry.session
    issue = entry.issue
    return {
        "session_id": json_safe(getattr(session, "session_id", None)),
        "turn_count": _int(getattr(session, "turn_count", 0)),
        "state": getattr(issue, "state", None),
        "started_at": to_rfc3339(entry.started_at),
        "last_event": json_safe(getattr(session, "last_codex_event", None)),
        "last_message": _clip(getattr(session, "last_codex_message", None)) or "",
        "last_event_at": to_rfc3339(getattr(session, "last_codex_timestamp", None)),
        "tokens": _tokens(entry),
        # SPEC 13.7.2 invites additional debugging fields on this endpoint.
        "phase": json_safe(entry.phase),
        "codex_app_server_pid": getattr(session, "codex_app_server_pid", None),
    }


def build_issue_detail(
    state: OrchestratorState,
    identifier: str,
    *,
    now: datetime | None = None,
    monotonic_ms: float | None = None,
) -> dict[str, Any] | None:
    """Render SPEC 13.7.2 ``/api/v1/<issue_identifier>``.

    Returns ``None`` when the issue is unknown to the current in-memory state;
    the HTTP layer turns that into ``404 issue_not_found``.
    """
    resolved = _resolve(state, identifier)
    if resolved is None:
        return None
    issue_id, entry, retry = resolved

    reference = _as_utc(now) if now is not None else datetime.now(UTC)
    mono_ms = monotonic_ms if monotonic_ms is not None else time.monotonic() * 1000.0

    issue = entry.issue if entry is not None else None
    resolved_identifier = None
    if entry is not None:
        resolved_identifier = entry.identifier or getattr(issue, "identifier", None)
    elif retry is not None:
        resolved_identifier = retry.identifier

    current_attempt = entry.retry_attempt if entry is not None else None
    if current_attempt is None and retry is not None:
        current_attempt = retry.attempt

    last_error = entry.last_error if entry is not None else None
    if last_error is None and retry is not None:
        last_error = retry.error

    return {
        "issue_identifier": resolved_identifier or identifier,
        "issue_id": issue_id,
        "status": _status(state, issue_id),
        "issue_url": getattr(issue, "url", None),
        "workspace": {"path": entry.workspace_path if entry is not None else None},
        "attempts": {
            # OrchestratorState (SPEC 4.1.8) carries no restart counter, so
            # this is derived best-effort: each queued retry attempt implies
            # one prior restart.
            "restart_count": max(0, _int(current_attempt)),
            "current_retry_attempt": current_attempt,
        },
        "running": _running_detail(entry) if entry is not None else None,
        "retry": _retry_row(retry, reference, mono_ms) if retry is not None else None,
        # This implementation streams agent output rather than writing per-issue
        # session log files, so there is nothing to link here.
        "logs": {"codex_session_logs": []},
        "recent_events": _recent_events(entry.recent_events) if entry is not None else [],
        "last_error": _clip(last_error),
        "tracked": {},
    }
