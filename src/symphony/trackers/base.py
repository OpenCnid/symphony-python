"""Issue tracker integration contract — SPEC section 11.

The tracker boundary is deliberately small: a portable *read kernel* for
scheduling (two operations) plus OPTIONAL provider-native agent tools. Generic
comment/state/attachment CRUD is explicitly out of scope — it loses provider
semantics and the orchestrator does not need it (SPEC 11, 11.5).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

from symphony.errors import TrackerResponseError, UnsupportedTrackerKind
from symphony.models import BlockerRef, Issue, normalize_label

__all__ = [
    "ToolSpec",
    "ToolResult",
    "ToolContext",
    "TrackerAdapter",
    "register_adapter",
    "build_adapter",
    "adapter_kinds",
    "parse_rfc3339",
    "normalize_labels",
    "coerce_priority",
    "coerce_blockers",
]


# --------------------------------------------------------------------------
# Provider-native agent tools (SPEC 10.5, OPTIONAL extension)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One provider-native tool advertised to the app-server session.

    Tool names, schemas, and result payloads are adapter-owned. Symphony does
    not standardize a lowest-common-denominator CRUD API (SPEC 10.5).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    mutates_tracker: bool = False


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result of a host-side tool execution.

    MUST distinguish success from failure and carry JSON-safe structured
    output translatable to the targeted app-server protocol (SPEC 10.5).
    """

    ok: bool
    content: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "content": self.content, "error": self.error}

    @classmethod
    def success(cls, content: Any) -> ToolResult:
        return cls(ok=True, content=content)

    @classmethod
    def failure(cls, error: str, **extra: Any) -> ToolResult:
        return cls(ok=False, error=error, content=extra or None)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Internal execution context handed to a tool.

    Contains the current normalized issue — **never the credential**
    (SPEC 10.5). The adapter may read ``issue.id`` and ``issue.native_ref`` to
    preserve provider richness without teaching the orchestrator provider
    semantics.
    """

    issue: Issue | None = None


# --------------------------------------------------------------------------
# Adapter contract (SPEC 11.1, 11.2)
# --------------------------------------------------------------------------


class TrackerAdapter(ABC):
    """Base class every tracker adapter implements.

    Subclasses declare :attr:`kind` (the exact supported ``tracker.kind``
    value) and are constructed from the current effective tracker
    configuration, including active/terminal states (SPEC 11.2).

    Both read operations return ``list[Issue]`` or raise a
    :class:`~symphony.errors.TrackerError` subclass. The orchestrator relies
    only on success versus failure (SPEC 11.1).
    """

    #: Exact ``tracker.kind`` value this adapter answers to.
    kind: ClassVar[str] = ""

    #: Adapter-documented default active states, used when the workflow omits
    #: ``tracker.active_states`` (SPEC 5.3.1).
    default_active_states: ClassVar[tuple[str, ...]] = ()
    default_terminal_states: ClassVar[tuple[str, ...]] = ()

    def __init__(self, provider: dict[str, Any], **_: Any) -> None:
        self.provider = provider

    # -- REQUIRED read kernel ------------------------------------------------

    @abstractmethod
    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        """Return normalized issues in the configured scope and requested states.

        The adapter MUST apply provider-side scope selection and pagination.
        An empty ``state_names`` list MUST return ``[]`` without a provider
        request.

        When used for candidate polling, active scoped issues are included
        even when ``dispatchable=False`` — the scheduler owns that final
        filter (SPEC 11.1).

        An individually malformed provider record MAY be omitted here (it was
        never safe to dispatch) and SHOULD be logged.
        """

    @abstractmethod
    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        """Return current full normalized snapshots for opaque dispatch IDs.

        An empty ``issue_ids`` list MUST return ``[]`` without a provider
        request. IDs no longer visible in scope are *omitted*; the
        orchestrator reads omission as "no longer visible" rather than
        inventing a synthetic state.

        Unlike the state-list read, this call MUST fail rather than silently
        omit a malformed *requested* record, because omission is meaningful
        here (SPEC 11.1).
        """

    # -- OPTIONAL provider-native agent tools (SPEC 10.5) --------------------

    def agent_tool_specs(self) -> list[ToolSpec]:
        """Tools advertised to the app-server session. Default: none."""
        return []

    def secret_environment_names(self) -> list[str]:
        """Env var names the launcher strips from child environments.

        SPEC 10.5/15.3: tracker credentials MUST NOT be inherited by the
        coding-agent child process. An adapter resolving credentials from the
        environment MUST declare those names here.
        """
        return []

    async def execute_agent_tool(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Execute a tool host-side with the configured adapter credential.

        Unsupported tool names MUST return a structured failure rather than
        raising, so the session continues instead of stalling (SPEC 10.5).
        """
        return ToolResult.failure(f"unsupported tool: {name}")

    async def aclose(self) -> None:
        """Release transport resources. Safe to call more than once."""
        return None


# --------------------------------------------------------------------------
# Registry — maps ``tracker.kind`` to an adapter class
# --------------------------------------------------------------------------

_REGISTRY: dict[str, type[TrackerAdapter]] = {}


def register_adapter(cls: type[TrackerAdapter]) -> type[TrackerAdapter]:
    """Class decorator registering an adapter under its ``kind``."""
    if not cls.kind:
        raise ValueError(f"{cls.__name__} must declare a non-empty 'kind'")
    _REGISTRY[cls.kind] = cls
    return cls


def adapter_kinds() -> list[str]:
    """Every supported ``tracker.kind``, sorted. Used by validation errors."""
    return sorted(_REGISTRY)


def build_adapter(kind: str, provider: dict[str, Any], **kwargs: Any) -> TrackerAdapter:
    """Construct the adapter for ``kind`` (SPEC 6.3 preflight uses this)."""
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise UnsupportedTrackerKind(
            f"unsupported tracker.kind {kind!r}; supported: {', '.join(adapter_kinds()) or '(none)'}",
            kind=kind,
            supported=adapter_kinds(),
        )
    return cls(provider, **kwargs)


# --------------------------------------------------------------------------
# Shared normalization helpers (SPEC 11.3)
# --------------------------------------------------------------------------

_RFC3339_Z = re.compile(r"Z$", re.IGNORECASE)


def parse_rfc3339(value: Any) -> datetime | None:
    """Parse an RFC 3339 instant, or return ``None`` if unusable.

    SPEC 11.3 permits unusable values for nullable fields to normalize to
    ``None``; that fallback alone does not make a record malformed.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(_RFC3339_Z.sub("+00:00", value.strip()))
    except ValueError:
        return None


def normalize_labels(raw: Any) -> tuple[str, ...]:
    """Trim, lowercase, drop blanks, remove duplicates, preserve order."""
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    seen: dict[str, None] = {}
    for item in raw:
        if isinstance(item, dict):
            item = item.get("name")
        if not isinstance(item, str):
            continue
        label = normalize_label(item)
        if label:
            seen.setdefault(label, None)
    return tuple(seen)


def coerce_priority(value: Any) -> int | None:
    """SPEC 11.3: priority MUST be an integer or ``None``.

    ``bool`` is rejected explicitly — it is an ``int`` subclass in Python and
    would otherwise silently become priority 0 or 1.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def coerce_blockers(raw: Any) -> tuple[BlockerRef, ...]:
    """Best-effort blocker normalization; unusable entries are dropped."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[BlockerRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ref = BlockerRef(
            id=_opt_str(item.get("id")),
            identifier=_opt_str(item.get("identifier")),
            state=_opt_str(item.get("state")),
        )
        if ref.id or ref.identifier or ref.state:
            out.append(ref)
    return tuple(out)


def _opt_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def require_str(record: dict[str, Any], key: str, *, where: str) -> str:
    """Read a REQUIRED non-empty string field or raise ``TrackerResponseError``.

    Used by adapters for ``id``, ``identifier``, ``title``, and ``state`` —
    the four fields whose absence makes a record malformed (SPEC 11.1).
    """
    value = record.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise TrackerResponseError(
        f"malformed record from {where}: missing or empty required field {key!r}",
        field=key,
        where=where,
    )


@dataclass(slots=True)
class NormalizationReport:
    """Counters an adapter fills in while normalizing a page of records.

    Exposed so ``fetch_issues_by_states`` can log omitted malformed records
    (SPEC 11.1) without inventing a per-adapter logging convention.
    """

    omitted: list[str] = field(default_factory=list)

    def omit(self, reason: str) -> None:
        self.omitted.append(reason)
