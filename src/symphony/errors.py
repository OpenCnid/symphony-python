"""Error taxonomy for Symphony.

Every error class in this module carries a stable, machine-readable ``category``
string. The spec names these categories directly (SPEC 5.5, 10.6, 11.4) and the
orchestrator's public behavior is defined in terms of them, so they are part of
the implementation contract and MUST NOT be renamed casually.

The orchestrator itself relies only on success-vs-failure (SPEC 11.1). The
categories exist for operators, logs, tests, and the RLM introspection surface.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SymphonyError",
    # SPEC 5.5 — workflow / template
    "WorkflowError",
    "MissingWorkflowFile",
    "WorkflowParseError",
    "WorkflowFrontMatterNotAMap",
    "TemplateParseError",
    "TemplateRenderError",
    # SPEC 6.3 — config validation
    "ConfigValidationError",
    # SPEC 9 — workspace
    "WorkspaceError",
    "WorkspacePathEscapesRoot",
    "WorkspaceCreationError",
    "HookError",
    "HookTimeout",
    # SPEC 10.6 — agent / codex
    "AgentError",
    "CodexNotFound",
    "InvalidWorkspaceCwd",
    "ResponseTimeout",
    "TurnTimeout",
    "PortExit",
    "ResponseError",
    "TurnFailed",
    "TurnCancelled",
    "TurnInputRequired",
    # SPEC 11.4 — tracker
    "TrackerError",
    "UnsupportedTrackerKind",
    "InvalidTrackerConfig",
    "MissingTrackerSecret",
    "TrackerRequestError",
    "TrackerStatusError",
    "TrackerResponseError",
    "TrackerPaginationError",
    "TrackerRateLimited",
]


class SymphonyError(Exception):
    """Base for every Symphony error.

    Subclasses set ``category`` to the stable spec-defined slug. ``details``
    carries JSON-safe structured context; it MUST NOT contain secrets
    (SPEC 15.3).
    """

    category: str = "symphony_error"

    def __init__(self, message: str, /, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe rendering, used by the HTTP API and RLM surface."""
        return {"category": self.category, "message": self.message, "details": self.details}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(category={self.category!r}, message={self.message!r})"


# --------------------------------------------------------------------------
# SPEC 5.5 — Workflow validation and error surface
# --------------------------------------------------------------------------


class WorkflowError(SymphonyError):
    category = "workflow_error"


class MissingWorkflowFile(WorkflowError):
    category = "missing_workflow_file"


class WorkflowParseError(WorkflowError):
    category = "workflow_parse_error"


class WorkflowFrontMatterNotAMap(WorkflowError):
    category = "workflow_front_matter_not_a_map"


class TemplateParseError(WorkflowError):
    category = "template_parse_error"


class TemplateRenderError(WorkflowError):
    category = "template_render_error"


# --------------------------------------------------------------------------
# SPEC 6.3 — Dispatch preflight validation
# --------------------------------------------------------------------------


class ConfigValidationError(SymphonyError):
    category = "config_validation_error"


# --------------------------------------------------------------------------
# SPEC 9 — Workspace management and safety
# --------------------------------------------------------------------------


class WorkspaceError(SymphonyError):
    category = "workspace_error"


class WorkspacePathEscapesRoot(WorkspaceError):
    """SPEC 9.5 Invariant 2 violation. Never recoverable; never retried blindly."""

    category = "workspace_path_escapes_root"


class WorkspaceCreationError(WorkspaceError):
    category = "workspace_creation_error"


class HookError(WorkspaceError):
    category = "hook_error"


class HookTimeout(WorkspaceError):
    category = "hook_timeout"


# --------------------------------------------------------------------------
# SPEC 10.6 — Agent runner timeouts and error mapping
# --------------------------------------------------------------------------


class AgentError(SymphonyError):
    category = "agent_error"


class CodexNotFound(AgentError):
    category = "codex_not_found"


class InvalidWorkspaceCwd(AgentError):
    """SPEC 9.5 Invariant 1: cwd != workspace_path at launch time."""

    category = "invalid_workspace_cwd"


class ResponseTimeout(AgentError):
    category = "response_timeout"


class TurnTimeout(AgentError):
    category = "turn_timeout"


class PortExit(AgentError):
    """The app-server subprocess exited while a turn was active."""

    category = "port_exit"


class ResponseError(AgentError):
    category = "response_error"


class TurnFailed(AgentError):
    category = "turn_failed"


class TurnCancelled(AgentError):
    category = "turn_cancelled"


class TurnInputRequired(AgentError):
    """SPEC 10.5: this implementation's documented policy fails such turns."""

    category = "turn_input_required"


# --------------------------------------------------------------------------
# SPEC 11.4 — Tracker error handling contract
# --------------------------------------------------------------------------


class TrackerError(SymphonyError):
    """Base tracker error.

    ``retryable`` and ``retry_after_ms`` are optional enrichments the spec
    permits; the orchestrator ignores them and relies only on success vs.
    failure.
    """

    category = "tracker_error"

    def __init__(
        self,
        message: str,
        /,
        *,
        retryable: bool | None = None,
        retry_after_ms: int | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message, **details)
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        if self.retryable is not None:
            out["retryable"] = self.retryable
        if self.retry_after_ms is not None:
            out["retry_after_ms"] = self.retry_after_ms
        return out


class UnsupportedTrackerKind(TrackerError):
    category = "unsupported_tracker_kind"


class InvalidTrackerConfig(TrackerError):
    category = "invalid_tracker_config"


class MissingTrackerSecret(TrackerError):
    """SPEC 5.3.1: a documented secret ``$VAR`` resolving to '' is *missing*."""

    category = "missing_tracker_secret"


class TrackerRequestError(TrackerError):
    """Transport failure — connection refused, DNS, TLS, read error."""

    category = "tracker_request"


class TrackerStatusError(TrackerError):
    """Non-success HTTP/provider response."""

    category = "tracker_status"


class TrackerResponseError(TrackerError):
    """Malformed or semantically invalid payload."""

    category = "tracker_response"


class TrackerPaginationError(TrackerError):
    """Pagination integrity failure — cursor loop, page count mismatch."""

    category = "tracker_pagination"


class TrackerRateLimited(TrackerError):
    category = "tracker_rate_limited"
