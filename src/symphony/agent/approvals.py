"""Approval, tool-call, and user-input policy — SPEC 10.5.

SPEC 10.5 leaves the posture implementation-defined but binds two things:

1. the chosen approval, sandbox, and operator-confirmation posture MUST be
   documented;
2. approval requests and user-input-required signals MUST NOT leave a run
   stalled indefinitely.

This implementation's documented posture (CONTRACTS.md section 5, README,
SPEC 15.1 trusted environments) is :data:`TRUSTED_AUTO_APPROVE`: auto-approve
command-execution and file-change approvals *for the session*, and treat
user-input-required turns as a hard failure.

The posture is a named, swappable object rather than a set of conditionals
inside the event plumbing. A deployment wanting a stricter stance substitutes
another :class:`ApprovalPolicy` — ``set_approval_policy(DENY_ALL)`` at startup,
or a per-call ``policy=`` argument — without editing the app-server client.

The no-stall guarantee is structural, not a matter of care: every
:class:`ApprovalDecision` is immediately actionable (answer the request, or end
the run), so no code path can decide to wait for a human. There is deliberately
no ``ESCALATE`` member; this implementation ships no operator channel, and a
decision that has nowhere to go is exactly the indefinite stall SPEC 10.5
forbids.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from symphony.errors import TurnInputRequired
from symphony.trackers.base import ToolResult

__all__ = [
    "APPROVAL_POLICIES",
    "DEFAULT_APPROVAL_POLICY",
    "DENY_ALL",
    "TRUSTED_AUTO_APPROVE",
    "ApprovalDecision",
    "ApprovalKind",
    "ApprovalPolicy",
    "StaticApprovalPolicy",
    "classify_approval",
    "decide_approval",
    "decide_user_input",
    "get_approval_policy",
    "policy_by_name",
    "set_approval_policy",
    "unsupported_tool_result",
    "user_input_failure",
]


class ApprovalKind(StrEnum):
    """What the agent is asking permission for (SPEC 10.5)."""

    COMMAND_EXECUTION = "command_execution"
    FILE_CHANGE = "file_change"
    USER_INPUT = "user_input"
    #: Request that could not be classified. Never assumed benign.
    UNKNOWN = "unknown"


class ApprovalDecision(StrEnum):
    """A policy's answer. Every member is immediately actionable (SPEC 10.5)."""

    #: Allow this one request.
    APPROVE = "approve"
    #: Allow this request and remember the answer for the rest of the session.
    APPROVE_FOR_SESSION = "approve_for_session"
    #: Refuse this request; the turn continues and the agent may adapt.
    DENY = "deny"
    #: Refuse and end the attempt (mapped to ``turn_input_required`` by the
    #: runner, which the orchestrator then retries per SPEC 16.6).
    FAIL_RUN = "fail_run"

    @property
    def is_approval(self) -> bool:
        return self in (ApprovalDecision.APPROVE, ApprovalDecision.APPROVE_FOR_SESSION)

    @property
    def remembers_for_session(self) -> bool:
        """Whether the client should stop round-tripping later requests of this kind."""
        return self is ApprovalDecision.APPROVE_FOR_SESSION

    @property
    def ends_run(self) -> bool:
        return self is ApprovalDecision.FAIL_RUN


@runtime_checkable
class ApprovalPolicy(Protocol):
    """The swappable posture object (SPEC 10.5).

    Any object with a ``name`` and a total :meth:`decide` is a policy. "Total"
    is the whole contract: it MUST return a decision for every
    :class:`ApprovalKind`, including :attr:`ApprovalKind.UNKNOWN`. Returning
    nothing, blocking, or awaiting an operator would reintroduce the stall
    SPEC 10.5 forbids.
    """

    @property
    def name(self) -> str: ...

    def decide(
        self, kind: ApprovalKind, request: Mapping[str, Any] | None = None
    ) -> ApprovalDecision: ...


@dataclass(frozen=True, slots=True)
class StaticApprovalPolicy:
    """Table-driven policy: one fixed decision per approval kind.

    Static by intent — the decision does not depend on the request contents, so
    the posture is auditable by reading the table. A deployment needing
    content-sensitive rules (allowlisted commands, path-scoped writes) supplies
    its own object satisfying :class:`ApprovalPolicy` instead of growing
    conditionals here.
    """

    name: str
    decisions: Mapping[ApprovalKind, ApprovalDecision]
    default: ApprovalDecision = ApprovalDecision.DENY

    def __post_init__(self) -> None:
        # Freeze the table so a shipped constant cannot be mutated at runtime
        # into a posture nobody documented.
        object.__setattr__(self, "decisions", MappingProxyType(dict(self.decisions)))

    def decide(
        self, kind: ApprovalKind, request: Mapping[str, Any] | None = None
    ) -> ApprovalDecision:
        """Total function over :class:`ApprovalKind` (SPEC 10.5 no-stall rule)."""
        return self.decisions.get(kind, self.default)


#: This implementation's documented posture (CONTRACTS.md section 5).
TRUSTED_AUTO_APPROVE = StaticApprovalPolicy(
    name="trusted-auto-approve",
    decisions={
        ApprovalKind.COMMAND_EXECUTION: ApprovalDecision.APPROVE_FOR_SESSION,
        ApprovalKind.FILE_CHANGE: ApprovalDecision.APPROVE_FOR_SESSION,
        ApprovalKind.USER_INPUT: ApprovalDecision.FAIL_RUN,
        ApprovalKind.UNKNOWN: ApprovalDecision.DENY,
    },
)

#: Stricter posture for untrusted environments; still non-stalling.
DENY_ALL = StaticApprovalPolicy(
    name="deny-all",
    decisions={
        ApprovalKind.COMMAND_EXECUTION: ApprovalDecision.DENY,
        ApprovalKind.FILE_CHANGE: ApprovalDecision.DENY,
        ApprovalKind.USER_INPUT: ApprovalDecision.FAIL_RUN,
        ApprovalKind.UNKNOWN: ApprovalDecision.DENY,
    },
)

DEFAULT_APPROVAL_POLICY: ApprovalPolicy = TRUSTED_AUTO_APPROVE

#: Name -> policy, for configuration-driven selection.
APPROVAL_POLICIES: Mapping[str, ApprovalPolicy] = MappingProxyType(
    {policy.name: policy for policy in (TRUSTED_AUTO_APPROVE, DENY_ALL)}
)

_active_policy: ApprovalPolicy = DEFAULT_APPROVAL_POLICY


def get_approval_policy() -> ApprovalPolicy:
    """The process-wide active posture."""
    return _active_policy


def set_approval_policy(policy: ApprovalPolicy) -> ApprovalPolicy:
    """Swap the active posture; returns the previous one.

    Intended for startup wiring (and for REPL-driven inspection). In-flight
    sessions read the policy per request, so a mid-run swap takes effect on the
    next approval rather than retroactively.
    """
    if not isinstance(policy, ApprovalPolicy):
        raise TypeError("approval policy must provide .name and .decide(kind, request)")
    global _active_policy
    previous = _active_policy
    _active_policy = policy
    return previous


def policy_by_name(name: str) -> ApprovalPolicy:
    """Resolve a shipped policy by name; raises ``ValueError`` if unknown.

    Only this module's own posture names resolve. Codex's protocol-level
    approval strings are a different vocabulary and pass through
    ``CodexConfig.approval_policy`` untouched (SPEC 10.1).
    """
    key = (name or "").strip().lower()
    policy = APPROVAL_POLICIES.get(key)
    if policy is None:
        known = ", ".join(sorted(APPROVAL_POLICIES))
        raise ValueError(f"unknown approval policy {name!r}; known policies: {known}")
    return policy


# --------------------------------------------------------------------------
# Request classification
# --------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]")

# Checked in this order, so a name matching several hints resolves
# deterministically toward the most restrictive reading.
_KIND_HINTS: tuple[tuple[ApprovalKind, tuple[str, ...]], ...] = (
    (
        ApprovalKind.USER_INPUT,
        ("userinput", "inputrequired", "requestinput", "askuser", "elicit", "userquestion"),
    ),
    (
        ApprovalKind.FILE_CHANGE,
        ("patch", "filechange", "applychange", "writefile", "editapproval", "diffapproval"),
    ),
    (
        ApprovalKind.COMMAND_EXECUTION,
        ("execcommand", "commandapproval", "execapproval", "shell", "runcommand", "exec"),
    ),
)

#: Structural fallbacks when no name hint matches.
_STRUCTURAL_HINTS: tuple[tuple[ApprovalKind, frozenset[str]], ...] = (
    (ApprovalKind.FILE_CHANGE, frozenset({"changes", "filechanges", "patch", "diff", "edits"})),
    (ApprovalKind.COMMAND_EXECUTION, frozenset({"command", "argv", "cmd", "commandline"})),
    (ApprovalKind.USER_INPUT, frozenset({"question", "inputrequest", "prompttouser"})),
)

_NAME_KEYS = frozenset({"method", "type", "event", "kind", "name", "subtype", "eventtype"})
_ENVELOPE_KEYS = frozenset({"params", "payload", "data", "request", "msg", "update"})


def _norm(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower())


def _candidate_names(request: Mapping[Any, Any], depth: int = 0) -> list[str]:
    names: list[str] = []
    if depth > 3:
        return names
    for key, value in request.items():
        if not isinstance(key, str):
            continue
        folded_key = _norm(key)
        if folded_key in _NAME_KEYS and isinstance(value, str):
            names.append(_norm(value))
        elif folded_key in _ENVELOPE_KEYS and isinstance(value, Mapping):
            names.extend(_candidate_names(value, depth + 1))
    return names


def _structural_keys(request: Mapping[Any, Any], depth: int = 0) -> set[str]:
    keys: set[str] = set()
    if depth > 3:
        return keys
    for key, value in request.items():
        if not isinstance(key, str):
            continue
        keys.add(_norm(key))
        if _norm(key) in _ENVELOPE_KEYS and isinstance(value, Mapping):
            keys |= _structural_keys(value, depth + 1)
    return keys


def classify_approval(request: Mapping[str, Any] | str | None) -> ApprovalKind:
    """Classify an approval/user-input request into an :class:`ApprovalKind`.

    Accepts a bare method/type name or the raw request payload. Matching is
    lenient about spelling (``execCommandApproval``, ``exec_command_approval``)
    because SPEC 10.4/10.5 defer protocol shape to the targeted Codex
    app-server version. Anything unrecognized classifies as
    :attr:`ApprovalKind.UNKNOWN` — never as an approvable default.
    """
    # Typed as ``object`` on purpose: the request is decoded from the agent
    # process, so the shape is asserted at runtime, not assumed.
    raw: object = request
    if isinstance(raw, str):
        names = [_norm(raw)]
        structural: set[str] = set()
    elif isinstance(raw, Mapping):
        names = _candidate_names(raw)
        structural = _structural_keys(raw)
    else:
        return ApprovalKind.UNKNOWN

    for kind, hints in _KIND_HINTS:
        for name in names:
            if any(hint in name for hint in hints):
                return kind

    for kind, keys in _STRUCTURAL_HINTS:
        if structural & keys:
            return kind

    return ApprovalKind.UNKNOWN


# --------------------------------------------------------------------------
# Decision functions
# --------------------------------------------------------------------------


def decide_approval(
    request: Mapping[str, Any] | str | None,
    *,
    policy: ApprovalPolicy | None = None,
) -> ApprovalDecision:
    """Decide one approval request (SPEC 10.5).

    Classifies the request, then defers entirely to the policy object. Under
    the documented posture this returns
    :attr:`ApprovalDecision.APPROVE_FOR_SESSION` for command-execution and
    file-change requests, and :attr:`ApprovalDecision.DENY` for anything that
    does not classify.
    """
    active = policy if policy is not None else _active_policy
    kind = classify_approval(request)
    payload = request if isinstance(request, Mapping) else None
    return active.decide(kind, payload)


def decide_user_input(
    request: Mapping[str, Any] | str | None = None,
    *,
    policy: ApprovalPolicy | None = None,
) -> ApprovalDecision:
    """Decide a user-input-required signal (SPEC 10.5).

    Always resolves — the run either proceeds or ends, never waits. Under the
    documented posture this is :attr:`ApprovalDecision.FAIL_RUN`.
    """
    active = policy if policy is not None else _active_policy
    payload = request if isinstance(request, Mapping) else None
    return active.decide(ApprovalKind.USER_INPUT, payload)


def user_input_failure(request: Mapping[str, Any] | str | None = None) -> TurnInputRequired:
    """Build the SPEC 10.6 ``turn_input_required`` error for a failed-out turn.

    Returned, not raised, so the caller controls unwinding. Details carry the
    request's method name only: the free-text prompt is agent-authored and may
    quote workspace content, and SPEC 15.3 keeps unnecessary payload text out
    of error surfaces.
    """
    method: str | None = None
    if isinstance(request, str):
        method = request
    elif isinstance(request, Mapping):
        names = [
            value
            for key, value in request.items()
            if isinstance(key, str) and _norm(key) in _NAME_KEYS and isinstance(value, str)
        ]
        method = names[0] if names else None

    return TurnInputRequired(
        "agent requested user input; documented policy fails the run",
        policy=get_approval_policy().name,
        method=method,
    )


def unsupported_tool_result(tool_name: str, *, supported: tuple[str, ...] = ()) -> ToolResult:
    """Structured failure for an unadvertised dynamic tool call (SPEC 10.5).

    SPEC 10.5 requires a tool *failure response* rather than an error that ends
    the turn: "this prevents the session from stalling on unsupported tool
    execution paths". The session continues after this result.
    """
    return ToolResult.failure(
        f"unsupported tool: {tool_name}",
        tool_name=tool_name,
        supported_tools=list(supported),
    )
