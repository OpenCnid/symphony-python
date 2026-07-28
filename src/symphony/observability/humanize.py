"""Humanized agent event summaries — SPEC 13.6.

Turns a raw coding-agent protocol event into one short operator-readable
sentence, e.g. ``turn_failed`` with a reason becomes
``Turn failed: response timeout``.

These strings are **observability-only** (SPEC 13.6). Nothing in the
orchestrator may branch on them: dispatch, retry, and reconciliation decisions
read the event name and typed payload fields, never the sentence produced
here. The functions are pure and total — an unknown, malformed, or empty event
yields a readable fallback instead of raising, because a summarizer that can
throw would turn an observability nicety into a correctness hazard.

Only well-known payload keys are read, and every interpolated value is
whitespace-collapsed and clipped, so raw payloads and secrets never reach a
summary (SPEC 13.1, 15.3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "KNOWN_EVENTS",
    "MAX_DETAIL_CHARS",
    "MAX_SUMMARY_CHARS",
    "event_name",
    "humanize_event",
    "humanize_events",
]

#: SPEC 10.4 emitted runtime events, verbatim.
KNOWN_EVENTS: tuple[str, ...] = (
    "session_started",
    "startup_failed",
    "turn_completed",
    "turn_failed",
    "turn_cancelled",
    "turn_ended_with_error",
    "turn_input_required",
    "approval_auto_approved",
    "unsupported_tool_call",
    "notification",
    "other_message",
    "malformed",
)

MAX_DETAIL_CHARS = 160
MAX_SUMMARY_CHARS = 240

_TRUNCATION_SUFFIX = "..."
_WHITESPACE = re.compile(r"\s+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Payload keys that are safe to surface, in preference order. Anything not
# listed is ignored rather than guessed at (SPEC 15.3).
_MESSAGE_KEYS = ("message", "text", "summary", "title", "content")
_REASON_KEYS = ("reason", "error", "error_message", "detail", "message", "status")
_NAME_KEYS = ("name", "tool", "tool_name", "method", "command", "type", "kind")
_SESSION_KEYS = ("session_id", "sessionId", "thread_id", "threadId")
_APPROVAL_KEYS = ("approval_type", "approvalType", "kind", "type", "name")
_USAGE_KEYS = ("usage", "total_token_usage", "totalTokenUsage", "token_usage", "tokenUsage")
_INPUT_TOKEN_KEYS = ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens")
_OUTPUT_TOKEN_KEYS = ("output_tokens", "outputTokens", "completion_tokens", "completionTokens")
_TOTAL_TOKEN_KEYS = ("total_tokens", "totalTokens")


# --------------------------------------------------------------------------
# Extraction helpers
# --------------------------------------------------------------------------


def _clean(value: Any) -> str | None:
    """Collapse whitespace and clip; ``None`` for anything unusable."""
    if value is None or isinstance(value, bool | Mapping | list | tuple | set):
        return None
    text = value if isinstance(value, str) else str(value)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return None
    if len(text) > MAX_DETAIL_CHARS:
        text = text[:MAX_DETAIL_CHARS] + _TRUNCATION_SUFFIX
    return text


def _pick(payload: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        if key in payload:
            cleaned = _clean(payload[key])
            if cleaned:
                return cleaned
    return None


def _pick_int(payload: Mapping[str, Any], keys: Iterable[str]) -> int | None:
    for key in keys:
        if key in payload:
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                continue
    return None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def event_name(event: Any) -> str | None:
    """Extract the event name from a string, mapping, or event-like object."""
    if isinstance(event, str):
        return event.strip() or None
    if isinstance(event, Mapping):
        raw = event.get("event", event.get("name", event.get("method")))
    else:
        raw = getattr(event, "event", None)
    if isinstance(raw, str):
        return raw.strip() or None
    inner = getattr(raw, "value", None)  # Enum member
    return inner.strip() if isinstance(inner, str) and inner.strip() else None


def _event_payload(event: Any, payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Merge the explicit payload with whatever the event object carries.

    An explicit ``payload`` argument wins; ``usage`` is folded in because
    SPEC 10.4 carries token counts beside the payload rather than inside it.
    """
    merged: dict[str, Any] = {}
    if not isinstance(event, str):
        if isinstance(event, Mapping):
            merged.update(_as_mapping(event.get("payload")))
            usage = event.get("usage")
            base: Mapping[str, Any] = event
        else:
            merged.update(_as_mapping(getattr(event, "payload", None)))
            usage = getattr(event, "usage", None)
            base = {}
        if isinstance(usage, Mapping):
            merged.setdefault("usage", usage)
        for key in ("message", "reason", "error", "name", "tool", "session_id"):
            if key in base and key not in merged:
                merged[key] = base[key]
    if payload:
        merged.update(payload)
    return merged


def _token_phrase(payload: Mapping[str, Any]) -> str:
    """``(tokens in=1200 out=800 total=2000)`` when counts are present."""
    usage: Mapping[str, Any] = payload
    for key in _USAGE_KEYS:
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            usage = candidate
            break
    inp = _pick_int(usage, _INPUT_TOKEN_KEYS)
    out = _pick_int(usage, _OUTPUT_TOKEN_KEYS)
    total = _pick_int(usage, _TOTAL_TOKEN_KEYS)
    parts = [
        f"{label}={value}"
        for label, value in (("in", inp), ("out", out), ("total", total))
        if value is not None
    ]
    return f" (tokens {' '.join(parts)})" if parts else ""


def _suffix(prefix: str, detail: str | None) -> str:
    return f"{prefix}{detail}" if detail else ""


# --------------------------------------------------------------------------
# Per-event summaries (SPEC 10.4 event names, SPEC 13.6 presentation)
# --------------------------------------------------------------------------


def _session_started(p: Mapping[str, Any]) -> str:
    return "Agent session started" + _suffix(" for session ", _pick(p, _SESSION_KEYS))


def _startup_failed(p: Mapping[str, Any]) -> str:
    return "Agent session failed to start" + _suffix(": ", _pick(p, _REASON_KEYS))


def _turn_completed(p: Mapping[str, Any]) -> str:
    turn = _pick_int(p, ("turn_number", "turnNumber", "turn_count", "turn"))
    label = f"Turn {turn} completed" if turn is not None else "Turn completed"
    return label + _token_phrase(p)


def _turn_failed(p: Mapping[str, Any]) -> str:
    return "Turn failed" + _suffix(": ", _pick(p, _REASON_KEYS))


def _turn_cancelled(p: Mapping[str, Any]) -> str:
    return "Turn cancelled" + _suffix(": ", _pick(p, _REASON_KEYS))


def _turn_ended_with_error(p: Mapping[str, Any]) -> str:
    return "Turn ended with an error" + _suffix(": ", _pick(p, _REASON_KEYS))


def _turn_input_required(p: Mapping[str, Any]) -> str:
    return "Turn requested user input" + _suffix(": ", _pick(p, _MESSAGE_KEYS))


def _approval_auto_approved(p: Mapping[str, Any]) -> str:
    kind = _pick(p, _APPROVAL_KEYS)
    return f"Auto-approved {kind} request" if kind else "Auto-approved an agent request"


def _unsupported_tool_call(p: Mapping[str, Any]) -> str:
    name = _pick(p, _NAME_KEYS)
    return f"Unsupported tool call: {name}" if name else "Unsupported tool call"


def _notification(p: Mapping[str, Any]) -> str:
    return "Notification" + _suffix(": ", _pick(p, _MESSAGE_KEYS))


def _other_message(p: Mapping[str, Any]) -> str:
    message = _pick(p, _MESSAGE_KEYS)
    if message:
        return f"Agent message: {message}"
    name = _pick(p, _NAME_KEYS)
    return f"Agent message ({name})" if name else "Agent message"


def _malformed(p: Mapping[str, Any]) -> str:
    return "Malformed agent output ignored" + _suffix(": ", _pick(p, _REASON_KEYS))


_SUMMARIES = {
    "session_started": _session_started,
    "startup_failed": _startup_failed,
    "turn_completed": _turn_completed,
    "turn_failed": _turn_failed,
    "turn_cancelled": _turn_cancelled,
    "turn_ended_with_error": _turn_ended_with_error,
    "turn_input_required": _turn_input_required,
    "approval_auto_approved": _approval_auto_approved,
    "unsupported_tool_call": _unsupported_tool_call,
    "notification": _notification,
    "other_message": _other_message,
    "malformed": _malformed,
}


def _prettify(name: str) -> str:
    """Readable text for wrapper/protocol names.

    ``thread/tokenUsage/updated`` -> ``Thread token usage updated``; that
    slash-and-camel family is the wrapper event class SPEC 13.5 names, and it
    arrives alongside the SPEC 10.4 events.
    """
    words: list[str] = []
    for part in re.split(r"[/._-]+", name.strip()):
        if not part:
            continue
        words.extend(w.lower() for w in _CAMEL_BOUNDARY.split(part) if w)
    if not words:
        return "Agent event"
    text = " ".join(words)
    return text[0].upper() + text[1:]


def humanize_event(event: Any, payload: Mapping[str, Any] | None = None) -> str:
    """Summarize one agent event in a single sentence (SPEC 13.6).

    Accepts an event name, a mapping (``{"event": ..., "payload": ...}``), or
    an event-like object exposing ``.event`` / ``.payload`` / ``.usage`` — the
    SPEC 10.4 shape — so this never has to import the agent module. Purely
    observability output: no caller may branch on the result.
    """
    name = event_name(event)
    merged = _event_payload(event, payload)

    if name is None:
        summary = "Unknown agent event"
    else:
        handler = _SUMMARIES.get(name)
        if handler is not None:
            summary = handler(merged)
        else:
            pretty = _prettify(name)
            detail = _pick(merged, _MESSAGE_KEYS) or _pick(merged, _REASON_KEYS)
            summary = pretty + _suffix(": ", detail) + _token_phrase(merged)

    summary = _WHITESPACE.sub(" ", summary).strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS] + _TRUNCATION_SUFFIX
    return summary


def humanize_events(events: Iterable[Any]) -> list[str]:
    """Summarize a sequence of events, preserving order (SPEC 13.6)."""
    return [humanize_event(event) for event in events]
