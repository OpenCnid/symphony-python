"""Core domain model — SPEC section 4.

These types are the shared vocabulary of every other module. They are
deliberately provider-agnostic: the orchestrator MUST NOT inspect provider
payloads or branch on provider-specific semantics (SPEC 11.2). Anything
provider-shaped travels opaquely in :attr:`Issue.native_ref`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

__all__ = [
    "BlockerRef",
    "Issue",
    "WorkflowDefinition",
    "Workspace",
    "RunPhase",
    "RunAttempt",
    "ClaimState",
    "LiveSession",
    "RetryEntry",
    "CodexTotals",
    "RunningEntry",
    "OrchestratorState",
    "WORKSPACE_KEY_ALLOWED",
    "normalize_state",
    "normalize_label",
    "workspace_key",
    "session_id",
]

# SPEC 4.2 / 9.5 Invariant 3 — the only characters permitted in a workspace
# directory name.
WORKSPACE_KEY_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")

# 8 bytes of BLAKE2b == 64 bits of entropy, rendered as 16 hex chars, all of
# which are inside WORKSPACE_KEY_ALLOWED. SPEC 4.2 requires "at least 64 bits".
_HASH_BYTES = 8


# --------------------------------------------------------------------------
# SPEC 4.2 — Stable identifiers and normalization rules
# --------------------------------------------------------------------------


def normalize_state(state: str | None) -> str:
    """Fold a provider-native state name for scheduler comparison.

    SPEC 4.2: compare states after trimming surrounding whitespace and applying
    lowercase. The provider's original spelling is preserved on the Issue
    (SPEC 11.3); only comparisons are folded.
    """
    return (state or "").strip().lower()


def normalize_label(label: str | None) -> str:
    """SPEC 11.3: labels are trimmed, lowercased strings."""
    return (label or "").strip().lower()


def workspace_key(identifier: str) -> str:
    """Derive a collision-resistant workspace directory name (SPEC 4.2, 9.5).

    Characters outside ``[A-Za-z0-9._-]`` are replaced with ``_``. When that
    replacement changes the identifier, a stable 64-bit hash of the *original*
    identifier is appended, so two distinct identifiers that sanitize to the
    same text still receive distinct keys.

    Identifiers unchanged by sanitization keep their plain deterministic key —
    the hash suffix is added only when it is needed to preserve injectivity.
    """
    if not identifier:
        raise ValueError("issue identifier must be a non-empty string")

    sanitized = WORKSPACE_KEY_ALLOWED.sub("_", identifier)
    if sanitized == identifier:
        return sanitized

    digest = hashlib.blake2b(identifier.encode("utf-8"), digest_size=_HASH_BYTES).hexdigest()
    return f"{sanitized}-{digest}"


def session_id(thread_id: str, turn_id: str) -> str:
    """SPEC 4.2 / 10.2: ``session_id = "<thread_id>-<turn_id>"``."""
    return f"{thread_id}-{turn_id}"


# --------------------------------------------------------------------------
# SPEC 4.1.1 — Issue
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockerRef:
    """Best-effort provider blocker metadata (SPEC 4.1.1).

    Adapters MUST NOT invent blocker semantics they cannot represent reliably
    (SPEC 11.3); every field is nullable for that reason.
    """

    id: str | None = None
    identifier: str | None = None
    state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "identifier": self.identifier, "state": self.state}


@dataclass(frozen=True, slots=True)
class Issue:
    """Normalized schedulable work item (SPEC 4.1.1).

    ``id`` is an *opaque dispatch identity*. It may be a project-item or
    board-entry ID rather than the provider's underlying ticket ID; core logic
    treats it as a bare map key and nothing more.
    """

    id: str
    identifier: str
    title: str
    state: str
    dispatchable: bool
    native_ref: dict[str, Any] | None = None
    description: str | None = None
    priority: int | None = None
    branch_name: str | None = None
    url: str | None = None
    assignee_id: str | None = None
    labels: tuple[str, ...] = ()
    blocked_by: tuple[BlockerRef, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        # SPEC 11.3: these four MUST be non-empty strings, and ``dispatchable``
        # MUST be explicit. A record failing this is malformed by definition.
        for name in ("id", "identifier", "title", "state"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Issue.{name} must be a non-empty string, got {value!r}")
        if not isinstance(self.dispatchable, bool):
            raise ValueError("Issue.dispatchable must be an explicit bool")

    @property
    def normalized_state(self) -> str:
        return normalize_state(self.state)

    @property
    def label_set(self) -> frozenset[str]:
        return frozenset(self.labels)

    def has_labels(self, required: tuple[str, ...] | list[str]) -> bool:
        """SPEC 5.3.1: every configured label must be present.

        Matching ignores case and surrounding whitespace. A blank configured
        label matches no issue, so it makes the whole check fail.
        """
        mine = self.label_set
        for raw in required:
            want = normalize_label(raw)
            if not want or want not in mine:
                return False
        return True

    def to_template_context(self) -> dict[str, Any]:
        """Render-ready mapping for the prompt template (SPEC 12.2).

        Keys are strings; nested collections stay iterable so templates can
        loop over ``issue.labels`` and ``issue.blocked_by``.
        """
        return {
            "id": self.id,
            "native_ref": self.native_ref,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "state": self.state,
            "branch_name": self.branch_name,
            "url": self.url,
            "assignee_id": self.assignee_id,
            "labels": list(self.labels),
            "blocked_by": [b.to_dict() for b in self.blocked_by],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "dispatchable": self.dispatchable,
        }


# --------------------------------------------------------------------------
# SPEC 4.1.2 / 4.1.4 — Workflow definition and workspace
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Parsed ``WORKFLOW.md`` payload (SPEC 4.1.2).

    ``config`` is the front-matter root object itself, *not* nested under a
    ``config`` key (SPEC 5.2).
    """

    config: dict[str, Any]
    prompt_template: str
    source_path: str | None = None


@dataclass(frozen=True, slots=True)
class Workspace:
    """Filesystem workspace assigned to one issue identifier (SPEC 4.1.4)."""

    path: str
    workspace_key: str
    created_now: bool


# --------------------------------------------------------------------------
# SPEC 7.2 / 4.1.5 — Run attempt lifecycle
# --------------------------------------------------------------------------


class RunPhase(str, Enum):
    """SPEC 7.2. Terminal reasons are distinct because retry logic differs."""

    PREPARING_WORKSPACE = "PreparingWorkspace"
    BUILDING_PROMPT = "BuildingPrompt"
    LAUNCHING_AGENT_PROCESS = "LaunchingAgentProcess"
    INITIALIZING_SESSION = "InitializingSession"
    STREAMING_TURN = "StreamingTurn"
    FINISHING = "Finishing"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    TIMED_OUT = "TimedOut"
    STALLED = "Stalled"
    CANCELED_BY_RECONCILIATION = "CanceledByReconciliation"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_PHASES

    @property
    def is_success(self) -> bool:
        return self is RunPhase.SUCCEEDED


_TERMINAL_PHASES = frozenset(
    {
        RunPhase.SUCCEEDED,
        RunPhase.FAILED,
        RunPhase.TIMED_OUT,
        RunPhase.STALLED,
        RunPhase.CANCELED_BY_RECONCILIATION,
    }
)


@dataclass(slots=True)
class RunAttempt:
    """One execution attempt for one issue (SPEC 4.1.5).

    ``attempt`` is ``None`` on the first run and ``>= 1`` for retries and
    continuations (SPEC 12.3).
    """

    issue_id: str
    issue_identifier: str
    attempt: int | None
    workspace_path: str
    started_at: datetime
    status: RunPhase = RunPhase.PREPARING_WORKSPACE
    error: str | None = None


class ClaimState(str, Enum):
    """SPEC 7.1 — the service's internal claim state.

    Distinct from tracker states (``Todo``, ``In Progress``, ...).
    """

    UNCLAIMED = "Unclaimed"
    CLAIMED = "Claimed"
    RUNNING = "Running"
    RETRY_QUEUED = "RetryQueued"
    RELEASED = "Released"


# --------------------------------------------------------------------------
# SPEC 4.1.6 — Live session (agent session metadata)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class LiveSession:
    """State tracked while a coding-agent subprocess is running (SPEC 4.1.6).

    The ``last_reported_*`` fields exist so absolute thread totals can be
    turned into deltas without double-counting (SPEC 13.5).
    """

    session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    codex_app_server_pid: str | None = None
    last_codex_event: str | None = None
    last_codex_timestamp: datetime | None = None
    last_codex_message: str | None = None
    codex_input_tokens: int = 0
    codex_output_tokens: int = 0
    codex_total_tokens: int = 0
    last_reported_input_tokens: int = 0
    last_reported_output_tokens: int = 0
    last_reported_total_tokens: int = 0
    turn_count: int = 0


# --------------------------------------------------------------------------
# SPEC 4.1.7 — Retry entry
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RetryEntry:
    """Scheduled retry state for an issue (SPEC 4.1.7).

    ``due_at_ms`` is a *monotonic* clock reading, not wall time; wall-clock
    ``due_at`` for the API is derived at snapshot time.
    """

    issue_id: str
    identifier: str | None
    attempt: int
    due_at_ms: float
    timer_handle: Any = None
    error: str | None = None


# --------------------------------------------------------------------------
# SPEC 4.1.8 — Orchestrator runtime state
# --------------------------------------------------------------------------


@dataclass(slots=True)
class CodexTotals:
    """Aggregate token + runtime counters (SPEC 13.3, 13.5)."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    seconds_running: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "seconds_running": round(self.seconds_running, 1),
        }


@dataclass(slots=True)
class RunningEntry:
    """One row of the orchestrator ``running`` map (SPEC 16.4)."""

    issue: Issue
    identifier: str
    started_at: datetime
    worker_handle: Any = None
    session: LiveSession = field(default_factory=LiveSession)
    retry_attempt: int | None = None
    workspace_path: str | None = None
    phase: RunPhase = RunPhase.PREPARING_WORKSPACE
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    last_error: str | None = None


@dataclass(slots=True)
class OrchestratorState:
    """Single authoritative in-memory state (SPEC 4.1.8).

    The orchestrator is the only component that mutates this (SPEC 7). Every
    worker outcome is reported back and converted into an explicit transition.
    """

    poll_interval_ms: int = 30_000
    max_concurrent_agents: int = 10
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    codex_totals: CodexTotals = field(default_factory=CodexTotals)
    codex_rate_limits: dict[str, Any] | None = None

    def claim_state(self, issue_id: str) -> ClaimState:
        """Report the SPEC 7.1 claim state for one issue."""
        if issue_id in self.running:
            return ClaimState.RUNNING
        if issue_id in self.retry_attempts:
            return ClaimState.RETRY_QUEUED
        if issue_id in self.claimed:
            return ClaimState.CLAIMED
        return ClaimState.UNCLAIMED

    def running_count(self) -> int:
        return len(self.running)

    def running_count_for_state(self, state: str) -> int:
        """Count running issues by their *currently tracked* state (SPEC 8.3)."""
        want = normalize_state(state)
        return sum(1 for e in self.running.values() if e.issue.normalized_state == want)
