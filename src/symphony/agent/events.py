"""Agent runtime events, token accounting, and rate-limit tracking.

Implements SPEC 10.4 (emitted runtime events) and SPEC 13.5 (session metrics
and token accounting).

The hard part of SPEC 13.5 is *payload selection*, not field parsing. Agent
updates carry token counts in several shapes, and only **absolute thread
totals** may feed the dashboard aggregate:

* accepted — ``thread/tokenUsage/updated`` payloads, and ``total_token_usage``
  inside a token-count wrapper event;
* ignored — delta-style payloads such as ``last_token_usage``, and generic
  ``usage`` maps on event types that do not define them as cumulative.

Selection is therefore strict, while field-name reading *inside* the selected
payload is lenient (providers spell the same counter several ways). That
asymmetry is deliberate: a lenient selector would silently double-count, and a
strict reader would silently drop real usage.

Because the accepted payloads are absolute, accumulating them naively
double-counts. :func:`apply_token_totals` instead credits the delta against the
last reported absolute (SPEC 13.5: "track deltas relative to last reported
totals to avoid double-counting"), which is exactly why
:class:`~symphony.models.LiveSession` carries both ``codex_*_tokens`` and
``last_reported_*_tokens``.
"""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from symphony.models import CodexTotals, LiveSession

__all__ = [
    "AGENT_EVENT_NAMES",
    "EVENT_APPROVAL_AUTO_APPROVED",
    "EVENT_MALFORMED",
    "EVENT_NOTIFICATION",
    "EVENT_OTHER_MESSAGE",
    "EVENT_SESSION_STARTED",
    "EVENT_STARTUP_FAILED",
    "EVENT_TURN_CANCELLED",
    "EVENT_TURN_COMPLETED",
    "EVENT_TURN_ENDED_WITH_ERROR",
    "EVENT_TURN_FAILED",
    "EVENT_TURN_INPUT_REQUIRED",
    "EVENT_UNSUPPORTED_TOOL_CALL",
    "TOKEN_USAGE_EVENT_NAMES",
    "AgentEvent",
    "apply_event_tokens",
    "apply_token_totals",
    "extract_rate_limits",
    "extract_token_totals",
    "read_token_counts",
    "select_absolute_usage",
    "token_delta",
]


# --------------------------------------------------------------------------
# SPEC 10.4 — emitted runtime event names (verbatim spec strings)
# --------------------------------------------------------------------------

EVENT_SESSION_STARTED = "session_started"
EVENT_STARTUP_FAILED = "startup_failed"
EVENT_TURN_COMPLETED = "turn_completed"
EVENT_TURN_FAILED = "turn_failed"
EVENT_TURN_CANCELLED = "turn_cancelled"
EVENT_TURN_ENDED_WITH_ERROR = "turn_ended_with_error"
EVENT_TURN_INPUT_REQUIRED = "turn_input_required"
EVENT_APPROVAL_AUTO_APPROVED = "approval_auto_approved"
EVENT_UNSUPPORTED_TOOL_CALL = "unsupported_tool_call"
EVENT_NOTIFICATION = "notification"
EVENT_OTHER_MESSAGE = "other_message"
EVENT_MALFORMED = "malformed"

#: SPEC 10.4 lists these as examples, so the set is open: an unrecognized name
#: is legal and MUST NOT be rejected. :attr:`AgentEvent.is_known` reports
#: membership without gating anything.
AGENT_EVENT_NAMES: tuple[str, ...] = (
    EVENT_SESSION_STARTED,
    EVENT_STARTUP_FAILED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_TURN_CANCELLED,
    EVENT_TURN_ENDED_WITH_ERROR,
    EVENT_TURN_INPUT_REQUIRED,
    EVENT_APPROVAL_AUTO_APPROVED,
    EVENT_UNSUPPORTED_TOOL_CALL,
    EVENT_NOTIFICATION,
    EVENT_OTHER_MESSAGE,
    EVENT_MALFORMED,
)


# --------------------------------------------------------------------------
# SPEC 13.5 — payload selection vocabulary
# --------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _norm(text: str) -> str:
    """Fold a key or type name so spelling variants compare equal.

    ``total_token_usage``, ``totalTokenUsage`` and ``Total Token Usage`` all
    fold to ``totaltokenusage``. Only *spelling* is folded — distinct concepts
    such as ``cached_input_tokens`` never collide with ``input_tokens``.
    """
    return _NON_ALNUM.sub("", text.lower())


#: Sub-objects that ARE absolute thread totals (SPEC 13.5, accepted).
_ABSOLUTE_TOTAL_KEYS = frozenset({"totaltokenusage", "totalusage", "cumulativetokenusage"})

#: Sub-objects that are per-turn/incremental (SPEC 13.5, ignored). The walk
#: never descends into these, so an absolute-looking key nested *inside* a
#: delta payload cannot be harvested either.
_DELTA_USAGE_KEYS = frozenset({"lasttokenusage", "lastusage", "deltatokenusage", "tokenusagedelta"})

#: Event type names whose payload is, by definition, an absolute thread total.
TOKEN_USAGE_EVENT_NAMES: tuple[str, ...] = (
    "thread/tokenUsage/updated",
    "tokenUsage/updated",
)
_TOKEN_USAGE_EVENT_KEYS = frozenset(_norm(name) for name in TOKEN_USAGE_EVENT_NAMES)

#: Keys whose value may name the event type.
_TYPE_KEYS = frozenset({"method", "type", "event", "eventtype", "name"})

#: Transport envelopes that wrap the interesting object one level down.
_ENVELOPE_KEYS = frozenset({"params", "payload", "data", "result", "info", "msg", "update"})

#: Usage containers to look for *inside* an accepted token-usage event.
_USAGE_CONTAINER_KEYS: tuple[str, ...] = (
    "totaltokenusage",
    "usage",
    "tokenusage",
    "tokens",
    "totals",
)

# Lenient field names, tried in order, within the *selected* payload.
# ``cached_input_tokens`` and ``reasoning_output_tokens`` are deliberately
# absent: providers report them as subsets of input/output, so counting them
# would inflate the aggregate.
_INPUT_FIELDS: tuple[str, ...] = (
    "inputtokens",
    "prompttokens",
    "inputtokencount",
    "tokensin",
    "input",
    "prompt",
)
_OUTPUT_FIELDS: tuple[str, ...] = (
    "outputtokens",
    "completiontokens",
    "outputtokencount",
    "tokensout",
    "output",
    "completion",
)
_TOTAL_FIELDS: tuple[str, ...] = (
    "totaltokens",
    "totaltokencount",
    "tokenstotal",
    "total",
)
_COUNT_FIELDS = frozenset(_INPUT_FIELDS + _OUTPUT_FIELDS + _TOTAL_FIELDS)

#: Rate-limit payload keys (SPEC 13.5: "track the latest rate-limit payload").
_RATE_LIMIT_KEYS = frozenset({"ratelimits", "ratelimit", "ratelimitsnapshot"})

# Bounds so a hostile or merely huge agent payload cannot turn extraction into
# an unbounded traversal.
_MAX_WALK_DEPTH = 8
_MAX_WALK_NODES = 512
_MAX_JSON_DEPTH = 12

#: Agent payloads are decoded from another process's JSON. The declared type is
#: what a well-behaved app-server sends; the alias is what the parser actually
#: assumes, so the runtime guards below are real checks rather than dead code.
_Payload = Mapping[Any, Any]


def _as_mapping(value: object) -> _Payload | None:
    return value if isinstance(value, Mapping) else None


# --------------------------------------------------------------------------
# SPEC 10.4 — AgentEvent
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One structured event emitted upstream to the orchestrator (SPEC 10.4).

    Fields follow SPEC 10.4 exactly: ``event`` name, UTC ``timestamp``, the
    app-server pid when available, an OPTIONAL generic ``usage`` map, and free
    ``payload`` fields.

    ``usage`` is *not* treated as a cumulative total by default (SPEC 13.5);
    it is re-tested with the same strict selector as ``payload`` and only
    contributes when it carries an absolute-total wrapper of its own.
    """

    event: str
    timestamp: datetime
    codex_app_server_pid: str | None = None
    usage: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def is_known(self) -> bool:
        """Whether ``event`` is one of the SPEC 10.4 example names."""
        return self.event in AGENT_EVENT_NAMES

    def token_totals(self) -> tuple[int, int, int] | None:
        """Absolute ``(input, output, total)`` for this event, or ``None``.

        SPEC 13.5. ``payload`` is consulted first; ``usage`` only afterwards
        and only under the same strict selection rules.
        """
        totals = extract_token_totals(self.payload)
        if totals is None and self.usage is not None:
            totals = extract_token_totals(self.usage)
        return totals

    def rate_limits(self) -> dict[str, Any] | None:
        """Latest rate-limit payload carried by this event, if any (SPEC 13.5)."""
        return extract_rate_limits(self.payload)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe rendering for logs, the HTTP API, and RLM inspection."""
        return {
            "event": self.event,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "codex_app_server_pid": self.codex_app_server_pid,
            "usage": _json_safe(self.usage),
            "payload": _json_safe(self.payload),
        }


# --------------------------------------------------------------------------
# SPEC 13.5 — payload selection (strict)
# --------------------------------------------------------------------------


def _iter_nodes(root: _Payload) -> Iterator[_Payload]:
    """Breadth-first walk of the mapping tree, pruning delta subtrees.

    Breadth-first so the outermost (most authoritative) wrapper wins when a
    payload nests several candidates.
    """
    queue: deque[tuple[_Payload, int]] = deque([(root, 0)])
    seen = 0
    while queue:
        node, depth = queue.popleft()
        seen += 1
        if seen > _MAX_WALK_NODES:
            return
        yield node
        if depth >= _MAX_WALK_DEPTH:
            continue
        for key, value in node.items():
            if not isinstance(key, str) or _norm(key) in _DELTA_USAGE_KEYS:
                continue
            if isinstance(value, Mapping):
                queue.append((value, depth + 1))


def _is_token_usage_event(node: _Payload) -> bool:
    """Whether this node identifies itself as a token-usage update event."""
    for key, value in node.items():
        if not isinstance(key, str) or _norm(key) not in _TYPE_KEYS:
            continue
        if isinstance(value, str) and _norm(value) in _TOKEN_USAGE_EVENT_KEYS:
            return True
    return False


def _has_count_fields(node: _Payload) -> bool:
    return any(isinstance(k, str) and _norm(k) in _COUNT_FIELDS for k in node)


def _usage_container(node: _Payload) -> _Payload | None:
    """Find the counts object inside an accepted token-usage event node."""
    envelopes = [
        value
        for key, value in node.items()
        if isinstance(key, str) and _norm(key) in _ENVELOPE_KEYS and isinstance(value, Mapping)
    ]
    for candidate in (node, *envelopes):
        folded = {_norm(k): v for k, v in candidate.items() if isinstance(k, str)}
        for name in _USAGE_CONTAINER_KEYS:
            value = folded.get(name)
            if isinstance(value, Mapping):
                return value
        if _has_count_fields(candidate):
            return candidate
    return None


def select_absolute_usage(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Pick the sub-payload that carries **absolute thread totals** (SPEC 13.5).

    Two acceptance rules, in order:

    1. an explicit absolute-total wrapper (``total_token_usage`` and spelling
       variants) anywhere outside a delta subtree;
    2. a node identifying itself as a ``thread/tokenUsage/updated`` event, in
       which case its ``usage`` map *is* defined as cumulative by the event
       type.

    Everything else — bare ``usage`` maps, ``last_token_usage``, per-turn
    counts — returns ``None``.
    """
    root = _as_mapping(payload)
    if root is None:
        return None

    nodes = list(_iter_nodes(root))

    for node in nodes:
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            if _norm(key) in _ABSOLUTE_TOTAL_KEYS and isinstance(value, Mapping):
                return value

    for node in nodes:
        if _is_token_usage_event(node):
            container = _usage_container(node)
            if container is not None:
                return container

    return None


# --------------------------------------------------------------------------
# SPEC 13.5 — field reading (lenient) and public extraction
# --------------------------------------------------------------------------


def _coerce_count(value: Any) -> int | None:
    """Coerce a provider-supplied token count to a non-negative int.

    Booleans are rejected (``True`` is not a count). Negative and non-finite
    values are rejected rather than clamped, so a malformed field is skipped
    instead of quietly reading as zero-that-looks-real.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _coerce_count(int(text))
        except ValueError:
            pass
        try:
            return _coerce_count(float(text))
        except ValueError:
            return None
    return None


def _first_count(folded: _Payload, names: Sequence[str]) -> int | None:
    for name in names:
        if name in folded:
            found = _coerce_count(folded[name])
            if found is not None:
                return found
    return None


def read_token_counts(usage: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """Read ``(input, output, total)`` from an already-selected payload.

    Lenient by design (SPEC 13.5: "extract input/output/total token counts
    leniently from common field names within the selected payload"). A missing
    total is derived as ``input + output``; a payload with no recognizable
    count field at all returns ``None``.
    """
    selected = _as_mapping(usage)
    if selected is None:
        return None

    folded = {_norm(k): v for k, v in selected.items() if isinstance(k, str)}
    input_tokens = _first_count(folded, _INPUT_FIELDS)
    output_tokens = _first_count(folded, _OUTPUT_FIELDS)
    total_tokens = _first_count(folded, _TOTAL_FIELDS)

    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None

    input_tokens = 0 if input_tokens is None else input_tokens
    output_tokens = 0 if output_tokens is None else output_tokens
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return (input_tokens, output_tokens, total_tokens)


def extract_token_totals(payload: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """Absolute ``(input, output, total)`` thread totals, or ``None`` (SPEC 13.5).

    ``None`` means "this payload carries no absolute total" — including the
    case where it carries a *delta* such as ``last_token_usage``, which MUST
    NOT reach dashboard/API totals.
    """
    selected = select_absolute_usage(payload)
    if selected is None:
        return None
    return read_token_counts(selected)


def extract_rate_limits(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Latest rate-limit payload found in an agent update, or ``None``.

    SPEC 13.5 only requires tracking the latest payload seen; its shape is
    provider-owned, so it is returned as an opaque JSON-safe copy. Copying
    matters: the caller stores it in ``OrchestratorState.codex_rate_limits``
    and the source payload may be reused or mutated by the transport layer.

    An empty map returns ``None`` so a contentless update cannot erase the last
    good snapshot.
    """
    root = _as_mapping(payload)
    if root is None:
        return None

    for node in _iter_nodes(root):
        for key, value in node.items():
            if not isinstance(key, str) or _norm(key) not in _RATE_LIMIT_KEYS:
                continue
            if isinstance(value, Mapping) and value:
                safe = _json_safe(value)
                return safe if isinstance(safe, dict) else None
    return None


# --------------------------------------------------------------------------
# SPEC 13.5 — delta arithmetic against the last reported absolute
# --------------------------------------------------------------------------


def token_delta(session: LiveSession, totals: tuple[int, int, int]) -> tuple[int, int, int]:
    """Creditable increment for ``totals`` given what the session last reported.

    Pure — mutates nothing. Each component is floored at zero: a *decreasing*
    absolute total (thread reset, or updates arriving out of order) credits
    nothing rather than driving the aggregate backwards.
    """
    input_tokens, output_tokens, total_tokens = totals
    return (
        max(0, input_tokens - session.last_reported_input_tokens),
        max(0, output_tokens - session.last_reported_output_tokens),
        max(0, total_tokens - session.last_reported_total_tokens),
    )


def apply_token_totals(
    session: LiveSession,
    totals: tuple[int, int, int],
    aggregate: CodexTotals | None = None,
) -> tuple[int, int, int]:
    """Credit an absolute total to a session and OPTIONAL aggregate (SPEC 13.5).

    Returns the delta actually credited.

    The session's ``codex_*_tokens`` accumulate credited deltas, so they are
    monotonic and survive a thread reset; ``last_reported_*_tokens`` always
    track the raw absolute last seen — *including a lower one*. Rebasing on the
    lower value is what makes the next increase count from the new baseline
    instead of being swallowed until the old high-water mark is passed again.
    """
    delta = token_delta(session, totals)
    input_tokens, output_tokens, total_tokens = totals

    session.codex_input_tokens += delta[0]
    session.codex_output_tokens += delta[1]
    session.codex_total_tokens += delta[2]

    session.last_reported_input_tokens = input_tokens
    session.last_reported_output_tokens = output_tokens
    session.last_reported_total_tokens = total_tokens

    if aggregate is not None:
        aggregate.input_tokens += delta[0]
        aggregate.output_tokens += delta[1]
        aggregate.total_tokens += delta[2]

    return delta


def apply_event_tokens(
    event: AgentEvent,
    session: LiveSession,
    aggregate: CodexTotals | None = None,
) -> tuple[int, int, int] | None:
    """Apply one agent event's absolute totals, if it carries any (SPEC 13.5).

    Returns the credited delta, or ``None`` when the event carries no absolute
    total — in which case neither the session nor the aggregate is touched.
    """
    totals = event.token_totals()
    if totals is None:
        return None
    return apply_token_totals(session, totals, aggregate)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Best-effort JSON-safe copy; unknown scalars degrade to ``str``."""
    if depth >= _MAX_JSON_DEPTH:
        return str(value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v, depth + 1) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
