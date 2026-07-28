"""In-process ``memory`` tracker adapter — SPEC 11.

This adapter is the reference implementation of the SPEC 11.1 read kernel. It
holds provider-shaped records in a Python list, so it needs no network and can
be driven directly from a REPL::

    >>> from symphony.trackers.memory import MemoryTrackerAdapter
    >>> t = MemoryTrackerAdapter()
    >>> t.add(id="1", identifier="ABC-1", title="Fix login", state="Todo")
    >>> await t.fetch_issues_by_states(["todo"])

Because it is in-process it can also be made to misbehave on purpose. The
fault-injection controls (:meth:`fail_requests`, :meth:`fail_status`,
:meth:`rate_limit`, :meth:`fail_pagination`) let sibling modules and the
conformance suite exercise the SPEC 11.4 error categories without a provider.

The two read operations differ in one way that must not be flattened
(SPEC 11.1): ``fetch_issues_by_states`` MAY omit an individually malformed
record and SHOULD log the omission, while ``fetch_issues_by_ids`` MUST fail
rather than silently omit a malformed *requested* record, because omission is
meaningful there — the orchestrator reads it as "no longer visible".

The adapter profile required by SPEC 11.2 lives at ``docs/adapters/memory.md``.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from symphony.errors import (
    InvalidTrackerConfig,
    MissingTrackerSecret,
    TrackerError,
    TrackerPaginationError,
    TrackerRateLimited,
    TrackerRequestError,
    TrackerResponseError,
    TrackerStatusError,
)
from symphony.models import Issue, normalize_state
from symphony.trackers.base import (
    NormalizationReport,
    ToolContext,
    ToolResult,
    ToolSpec,
    TrackerAdapter,
    coerce_blockers,
    coerce_priority,
    normalize_labels,
    parse_rfc3339,
    register_adapter,
    require_str,
)

__all__ = [
    "PROVIDER_KEYS",
    "TOOL_SPECS",
    "FaultInjector",
    "MemoryTrackerAdapter",
    "ProviderCall",
]

# Keys accepted inside ``tracker.provider``. Unknown keys are a documented
# validation error (SPEC 5.3.1 makes each adapter own its provider schema).
PROVIDER_KEYS = frozenset(
    {
        "seed",
        "scope",
        "page_size",
        "max_ids_per_request",
        "require_assignee",
        "secret_env",
    }
)

# ``native_ref`` MUST contain only non-secret values (SPEC 11.3, 15.3). Keys
# that look credential-shaped are dropped rather than trusted.
_SECRETISH_KEY = re.compile(
    r"(?i)(token|secret|password|passwd|api[-_ ]?key|authoriz|credential|cookie|session[-_ ]?id)"
)

_MAX_NATIVE_REF_DEPTH = 6


# --------------------------------------------------------------------------
# Small local helpers
# --------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    """Return a trimmed non-empty string, or ``None`` for anything else."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _json_safe(value: Any, *, depth: int = 0) -> bool:
    """True when ``value`` round-trips through JSON without custom encoding."""
    if depth > _MAX_NATIVE_REF_DEPTH:
        return False
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, (list, tuple)):
        return all(_json_safe(item, depth=depth + 1) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _json_safe(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False


class _StdlibLogger:
    """Minimal SPEC 13.1 ``key=value`` logger.

    Used only until ``symphony.observability.logging`` is importable; the real
    structured logger is preferred whenever it exists.
    """

    __slots__ = ("_bound", "_log")

    def __init__(self, name: str, **bound: Any) -> None:
        self._log = logging.getLogger(name)
        self._bound = bound

    def bind(self, **fields: Any) -> _StdlibLogger:
        merged = {**self._bound, **fields}
        return _StdlibLogger(self._log.name, **merged)

    def _emit(self, level: int, message: str, fields: dict[str, Any]) -> None:
        merged = {**self._bound, **fields}
        suffix = " ".join(f"{key}={value}" for key, value in merged.items())
        self._log.log(level, f"{message} {suffix}".strip())

    def debug(self, message: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, message, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit(logging.INFO, message, fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._emit(logging.WARNING, message, fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit(logging.ERROR, message, fields)


def _default_logger() -> Any:
    try:  # pragma: no cover - depends on sibling module landing
        from symphony.observability.logging import get_logger
    except Exception:
        return _StdlibLogger("symphony.trackers.memory")
    try:  # pragma: no cover - defensive
        return get_logger("symphony.trackers.memory")
    except Exception:
        return _StdlibLogger("symphony.trackers.memory")


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Observability of provider traffic + fault injection (SPEC 11.4)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderCall:
    """One simulated provider round trip.

    Recorded so tests can assert the SPEC 11.1 rule that an empty input list
    performs *no* provider request, and count pages.
    """

    op: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FaultInjector:
    """Armed faults, mapped 1:1 onto the SPEC 11.4 error categories.

    ``remaining`` counts how many provider calls the armed faults still apply
    to. ``None`` means "until explicitly cleared".
    """

    request_error: str | None = None
    status_code: int | None = None
    status_message: str | None = None
    rate_limited: bool = False
    retry_after_ms: int | None = None
    pagination_after_pages: int | None = None
    remaining: int | None = None

    @property
    def armed(self) -> bool:
        return (
            self.request_error is not None
            or self.status_code is not None
            or self.rate_limited
            or self.pagination_after_pages is not None
        )

    def clear(self) -> None:
        self.request_error = None
        self.status_code = None
        self.status_message = None
        self.rate_limited = False
        self.retry_after_ms = None
        self.pagination_after_pages = None
        self.remaining = None

    def consume(self) -> None:
        """Burn one use of a `times=N` fault, clearing it when exhausted."""
        if self.remaining is None:
            return
        self.remaining -= 1
        if self.remaining <= 0:
            self.clear()

    def check(self, *, where: str) -> None:
        """Raise the armed transport/status/rate-limit fault, if any."""
        if self.request_error is not None:
            message = self.request_error
            self.consume()
            raise TrackerRequestError(
                f"memory tracker transport failure during {where}: {message}",
                retryable=True,
                where=where,
            )
        if self.status_code is not None:
            status = self.status_code
            detail = self.status_message or "injected non-success response"
            self.consume()
            raise TrackerStatusError(
                f"memory tracker returned status {status} during {where}: {detail}",
                retryable=status >= 500,
                status=status,
                where=where,
            )
        if self.rate_limited:
            retry_after = self.retry_after_ms
            self.consume()
            raise TrackerRateLimited(
                f"memory tracker rate limited during {where}",
                retryable=True,
                retry_after_ms=retry_after,
                where=where,
            )


# --------------------------------------------------------------------------
# Provider-native agent tools (SPEC 10.5, 11.5)
# --------------------------------------------------------------------------

_ISSUE_ID_PROPERTY = {
    "type": "string",
    "description": "Dispatch ID of the target issue. Defaults to the current issue.",
}

TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="memory_get_issue",
        description=(
            "Read the current normalized snapshot of an issue in the memory tracker, "
            "including its labels, blockers, native_ref, and comments."
        ),
        input_schema={
            "type": "object",
            "properties": {"issue_id": _ISSUE_ID_PROPERTY},
            "required": [],
            "additionalProperties": False,
        },
        mutates_tracker=False,
    ),
    ToolSpec(
        name="memory_add_comment",
        description="Append a comment to an issue in the memory tracker.",
        input_schema={
            "type": "object",
            "properties": {
                "issue_id": _ISSUE_ID_PROPERTY,
                "body": {"type": "string", "description": "Comment text. Must be non-empty."},
            },
            "required": ["body"],
            "additionalProperties": False,
        },
        mutates_tracker=True,
    ),
    ToolSpec(
        name="memory_set_state",
        description=(
            "Move an issue to a new provider-native state, e.g. the next handoff state "
            "for the workflow (SPEC 11.5)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "issue_id": _ISSUE_ID_PROPERTY,
                "state": {
                    "type": "string",
                    "description": "Provider-native state name. Must be non-empty.",
                },
            },
            "required": ["state"],
            "additionalProperties": False,
        },
        mutates_tracker=True,
    ),
)

_TOOL_NAMES = tuple(spec.name for spec in TOOL_SPECS)


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


@register_adapter
class MemoryTrackerAdapter(TrackerAdapter):
    """In-process tracker adapter for ``tracker.kind: memory`` (SPEC 11.2).

    Scope selection is the provider key ``scope``: when set, only records whose
    ``scope`` field matches are visible to either read operation, to tools, or
    to the mutation helpers. When unset, every seeded record is in scope.
    """

    kind: ClassVar[str] = "memory"

    #: SPEC 5.3.1 permits omitting ``tracker.active_states`` /
    #: ``tracker.terminal_states`` when the adapter profile documents defaults.
    default_active_states: ClassVar[tuple[str, ...]] = ("Todo", "In Progress")
    default_terminal_states: ClassVar[tuple[str, ...]] = ("Done", "Canceled")

    def __init__(
        self,
        provider: Mapping[str, Any] | None = None,
        *,
        active_states: Iterable[str] | None = None,
        terminal_states: Iterable[str] | None = None,
        logger: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        **_: Any,
    ) -> None:
        provider = {} if provider is None else provider
        if not isinstance(provider, Mapping):
            raise InvalidTrackerConfig(
                "tracker.provider must be a mapping for tracker.kind 'memory'",
                got=type(provider).__name__,
            )
        super().__init__(dict(provider))

        unknown = sorted(set(self.provider) - PROVIDER_KEYS)
        if unknown:
            raise InvalidTrackerConfig(
                f"unknown tracker.provider key(s) for 'memory': {', '.join(unknown)}",
                unknown=unknown,
                supported=sorted(PROVIDER_KEYS),
            )

        self._scope = self._read_optional_str("scope")
        self._page_size = self._read_non_negative_int("page_size", default=0)
        self._max_ids_per_request = self._read_non_negative_int("max_ids_per_request", default=0)
        self._require_assignee = self._read_bool("require_assignee", default=False)
        self._secret_env = self._read_optional_str("secret_env")
        if self._secret_env is not None and not os.environ.get(self._secret_env, "").strip():
            # SPEC 5.3.1: a documented secret resolving to '' is *missing*. The
            # value itself is never read into an attribute (SPEC 15.3).
            raise MissingTrackerSecret(
                f"tracker secret environment variable {self._secret_env!r} is unset or empty",
                env_name=self._secret_env,
            )

        self.active_states: tuple[str, ...] = self._states(
            active_states, self.default_active_states
        )
        self.terminal_states: tuple[str, ...] = self._states(
            terminal_states, self.default_terminal_states
        )
        self._terminal_set = {normalize_state(s) for s in self.terminal_states}

        self._records: list[dict[str, Any]] = []
        for index, record in enumerate(self._read_seed()):
            self._records.append(dict(record))
            self._assert_unique(self._records[-1], where=f"tracker.provider.seed[{index}]")

        self.faults = FaultInjector()
        self.calls: list[ProviderCall] = []
        self.last_normalization_report = NormalizationReport()
        self._closed = False
        self._log = logger if logger is not None else _default_logger()
        self._clock = clock if clock is not None else _utc_now

    # -- construction-time validation ---------------------------------------

    def _read_seed(self) -> list[Mapping[str, Any]]:
        raw = self.provider.get("seed", [])
        if raw is None:
            return []
        if not isinstance(raw, (list, tuple)):
            raise InvalidTrackerConfig(
                "tracker.provider.seed must be a list of provider records",
                got=type(raw).__name__,
            )
        out: list[Mapping[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise InvalidTrackerConfig(
                    f"tracker.provider.seed[{index}] must be a mapping",
                    index=index,
                    got=type(item).__name__,
                )
            out.append(item)
        return out

    def _read_optional_str(self, key: str) -> str | None:
        value = self.provider.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise InvalidTrackerConfig(
                f"tracker.provider.{key} must be a string", key=key, got=type(value).__name__
            )
        return value.strip() or None

    def _read_non_negative_int(self, key: str, *, default: int) -> int:
        value = self.provider.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidTrackerConfig(
                f"tracker.provider.{key} must be an integer >= 0", key=key, got=repr(value)
            )
        return value

    def _read_bool(self, key: str, *, default: bool) -> bool:
        value = self.provider.get(key, default)
        if not isinstance(value, bool):
            raise InvalidTrackerConfig(
                f"tracker.provider.{key} must be a boolean", key=key, got=repr(value)
            )
        return value

    @staticmethod
    def _states(supplied: Iterable[str] | None, fallback: tuple[str, ...]) -> tuple[str, ...]:
        if supplied is None:
            return fallback
        return tuple(s for s in supplied if isinstance(s, str) and s.strip())

    def _assert_unique(self, record: dict[str, Any], *, where: str) -> None:
        """SPEC 4.1.1: ``id`` and ``identifier`` are unique within the scope."""
        scope = _text(record.get("scope"))
        new_id = self._dispatch_id(record)
        new_identifier = _text(record.get("identifier"))
        for other in self._records:
            if other is record or _text(other.get("scope")) != scope:
                continue
            if new_id is not None and self._dispatch_id(other) == new_id:
                raise InvalidTrackerConfig(
                    f"duplicate dispatch id {new_id!r} within scope {scope!r} at {where}",
                    issue_id=new_id,
                    scope=scope,
                )
            if new_identifier is not None and _text(other.get("identifier")) == new_identifier:
                raise InvalidTrackerConfig(
                    f"duplicate identifier {new_identifier!r} within scope {scope!r} at {where}",
                    identifier=new_identifier,
                    scope=scope,
                )

    # -- REQUIRED read kernel (SPEC 11.1) ------------------------------------

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        """Return normalized issues in scope whose state matches (SPEC 11.1).

        An empty ``state_names`` list returns ``[]`` without a provider request
        — armed faults do not fire, because no request is made. Issues with
        ``dispatchable=False`` are included; the scheduler owns that filter.
        Individually malformed records are omitted and logged.
        """
        if not state_names:
            return []

        wanted = {normalize_state(s) for s in state_names if isinstance(s, str) and s.strip()}
        selected = self._select_by_state(wanted)
        report = NormalizationReport()
        issues: list[Issue] = []

        for served, page in enumerate(self._paginate(selected)):
            self._provider_call("fetch_issues_by_states", page=served, size=len(page))
            if (
                self.faults.pagination_after_pages is not None
                and served >= self.faults.pagination_after_pages
            ):
                self.faults.consume()
                raise TrackerPaginationError(
                    f"memory tracker pagination integrity failure after {served} page(s)",
                    retryable=True,
                    page=served,
                    where="fetch_issues_by_states",
                )
            for record in page:
                try:
                    issues.append(self._normalize(record, where="fetch_issues_by_states"))
                except TrackerResponseError as exc:
                    reason = exc.message
                    report.omit(reason)
                    self._log.warning(
                        "tracker record omitted outcome=omitted",
                        tracker_kind=self.kind,
                        issue_id=self._dispatch_id(record) or "(unknown)",
                        issue_identifier=_text(record.get("identifier")) or "(unknown)",
                        reason=reason,
                    )

        self.last_normalization_report = report
        return issues

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        """Return full normalized snapshots for opaque dispatch IDs (SPEC 11.1).

        An empty ``issue_ids`` list returns ``[]`` without a provider request.
        Input is treated as a set and each dispatch ID appears at most once.
        IDs no longer visible in scope are omitted, but a *requested* record
        that is malformed raises instead of being silently dropped, because
        omission means "no longer visible" to the orchestrator.
        """
        if not issue_ids:
            return []

        wanted: list[str] = []
        seen_input: set[str] = set()
        for raw in issue_ids:
            value = _text(raw)
            if value is not None and value not in seen_input:
                seen_input.add(value)
                wanted.append(value)

        by_id: dict[str, dict[str, Any]] = {}
        for record in self._records:
            dispatch_id = self._dispatch_id(record)
            if dispatch_id is not None and self._in_scope(record):
                by_id.setdefault(dispatch_id, record)

        out: list[Issue] = []
        for batch in self._batches(wanted):
            self._provider_call("fetch_issues_by_ids", ids=len(batch))
            for dispatch_id in batch:
                target = by_id.get(dispatch_id)
                if target is None:
                    continue  # no longer visible in the configured scope
                out.append(self._normalize(target, where="fetch_issues_by_ids"))
        return out

    # -- provider simulation -------------------------------------------------

    def _provider_call(self, op: str, **detail: Any) -> None:
        if self._closed:
            raise TrackerRequestError(
                f"memory tracker adapter is closed; cannot perform {op}",
                retryable=False,
                where=op,
            )
        self.calls.append(ProviderCall(op=op, detail=detail))
        self.faults.check(where=op)

    def _in_scope(self, record: Mapping[str, Any]) -> bool:
        if self._scope is None:
            return True
        return _text(record.get("scope")) == self._scope

    def _select_by_state(self, wanted: set[str]) -> list[dict[str, Any]]:
        """Records in scope matching a requested state, in seed order.

        A record whose ``state`` is missing or blank cannot be ruled out by the
        filter, so it is selected and then reported as malformed. That keeps
        the SPEC 11.1 omission log honest no matter which required field broke.
        """
        out: list[dict[str, Any]] = []
        for record in self._records:
            if not self._in_scope(record):
                continue
            state = _text(record.get("state"))
            if state is None or normalize_state(state) in wanted:
                out.append(record)
        return out

    def _paginate(self, records: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Split into pages; always at least one page, so one request happens."""
        size = self._page_size or len(records) or 1
        pages = [list(records[i : i + size]) for i in range(0, len(records), size)]
        return pages or [[]]

    def _batches(self, ids: Sequence[str]) -> list[list[str]]:
        size = self._max_ids_per_request or len(ids) or 1
        batches = [list(ids[i : i + size]) for i in range(0, len(ids), size)]
        return batches or [[]]

    # -- normalization (SPEC 11.3) -------------------------------------------

    @staticmethod
    def _dispatch_id(record: Mapping[str, Any]) -> str | None:
        """Dispatch identity: the project-item ID when present, else the ticket ID."""
        return _text(record.get("item_id")) or _text(record.get("id"))

    def _normalize(self, record: dict[str, Any], *, where: str) -> Issue:
        """Map a provider record to :class:`~symphony.models.Issue` (SPEC 11.3).

        Raises :class:`~symphony.errors.TrackerResponseError` when the record is
        malformed — that is, when ``id``, ``identifier``, ``title``, ``state``,
        or an explicit ``dispatchable`` cannot be produced. Unusable optional
        values fall back to ``None`` / empty collections instead.
        """
        dispatch_id = self._dispatch_id(record)
        if dispatch_id is None:
            raise TrackerResponseError(
                f"malformed record from {where}: missing or empty required field 'id'",
                field="id",
                where=where,
            )
        identifier = require_str(record, "identifier", where=where)
        title = require_str(record, "title", where=where)
        state = require_str(record, "state", where=where)
        dispatchable = self._dispatchable(record, where=where)
        raw_description = record.get("description")
        description = raw_description if isinstance(raw_description, str) else None

        return Issue(
            id=dispatch_id,
            identifier=identifier,
            title=title,
            state=state,
            dispatchable=dispatchable,
            native_ref=self._native_ref(record),
            description=description,
            priority=coerce_priority(record.get("priority")),
            branch_name=_text(record.get("branch_name")),
            url=_text(record.get("url")),
            assignee_id=_text(record.get("assignee_id")),
            labels=normalize_labels(record.get("labels")),
            blocked_by=coerce_blockers(record.get("blocked_by")),
            created_at=parse_rfc3339(record.get("created_at")),
            updated_at=parse_rfc3339(record.get("updated_at")),
        )

    def _dispatchable(self, record: dict[str, Any], *, where: str) -> bool:
        """Derive explicit dispatchability from provider routing rules (SPEC 11.3).

        An explicit boolean in the record is honored verbatim. Otherwise the
        documented memory routing rule applies: archived records, records with
        an unresolved blocker, and (when ``require_assignee`` is set) unassigned
        records are not dispatchable.
        """
        if "dispatchable" in record:
            value = record["dispatchable"]
            if not isinstance(value, bool):
                raise TrackerResponseError(
                    f"malformed record from {where}: 'dispatchable' must be an explicit boolean",
                    field="dispatchable",
                    where=where,
                )
            return value
        if record.get("archived"):
            return False
        if self._require_assignee and _text(record.get("assignee_id")) is None:
            return False
        for blocker in coerce_blockers(record.get("blocked_by")):
            if normalize_state(blocker.state) not in self._terminal_set:
                return False
        return True

    def _native_ref(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Retain only non-secret, JSON-safe provider metadata (SPEC 11.3)."""
        out: dict[str, Any] = {}
        raw = record.get("native_ref")
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                if not isinstance(key, str) or _SECRETISH_KEY.search(key):
                    continue
                if _json_safe(value):
                    out[key] = value
        item_id = _text(record.get("item_id"))
        ticket_id = _text(record.get("id"))
        if item_id is not None and ticket_id is not None and ticket_id != item_id:
            # The dispatch ID is the project-item ID, so the distinct underlying
            # ticket ID must survive for provider-native tools (SPEC 11.2).
            out["ticket_id"] = ticket_id
        return out or None

    # -- fault-injection controls (SPEC 11.4) --------------------------------

    def fail_requests(
        self, message: str = "connection reset by peer", *, times: int | None = None
    ) -> None:
        """Arm a transport failure -> ``TrackerRequestError`` (``tracker_request``)."""
        self.faults.clear()
        self.faults.request_error = message
        self.faults.remaining = times

    def fail_status(
        self, status_code: int = 500, message: str | None = None, *, times: int | None = None
    ) -> None:
        """Arm a non-success response -> ``TrackerStatusError`` (``tracker_status``)."""
        self.faults.clear()
        self.faults.status_code = status_code
        self.faults.status_message = message
        self.faults.remaining = times

    def rate_limit(self, *, retry_after_ms: int | None = 1000, times: int | None = None) -> None:
        """Arm rate limiting -> ``TrackerRateLimited`` (``tracker_rate_limited``)."""
        self.faults.clear()
        self.faults.rate_limited = True
        self.faults.retry_after_ms = retry_after_ms
        self.faults.remaining = times

    def fail_pagination(self, *, after_pages: int = 1, times: int | None = None) -> None:
        """Arm a paging integrity failure -> ``TrackerPaginationError``.

        ``after_pages`` pages are served normally before the failure, so tests
        can prove a partial page walk is not observable to the scheduler.
        """
        self.faults.clear()
        self.faults.pagination_after_pages = after_pages
        self.faults.remaining = times

    def clear_faults(self) -> None:
        """Disarm every injected fault."""
        self.faults.clear()

    def reset_calls(self) -> None:
        """Forget recorded provider traffic."""
        self.calls.clear()

    @property
    def provider_calls(self) -> int:
        """Number of simulated provider round trips since the last reset."""
        return len(self.calls)

    # -- record store helpers (test/REPL affordances) ------------------------

    def add(self, **fields: Any) -> dict[str, Any]:
        """Append a provider record and return it (mutable by reference)."""
        record = dict(fields)
        self._assert_unique(record, where="add()")
        self._records.append(record)
        return record

    def extend(self, records: Iterable[Mapping[str, Any]]) -> None:
        """Append several provider records, preserving order."""
        for record in records:
            self.add(**dict(record))

    def update(self, issue_id: str, **fields: Any) -> dict[str, Any]:
        """Patch the record with dispatch id ``issue_id``."""
        record = self._require_record(issue_id)
        record.update(fields)
        return record

    def remove(self, issue_id: str) -> bool:
        """Drop a record so it stops being visible. Returns whether it existed."""
        for index, record in enumerate(self._records):
            if self._in_scope(record) and self._dispatch_id(record) == issue_id:
                del self._records[index]
                return True
        return False

    def corrupt(self, issue_id: str, field_name: str = "title", value: Any = "") -> dict[str, Any]:
        """Make a record malformed on purpose (SPEC 11.1 asymmetry testing)."""
        record = self._require_record(issue_id)
        record[field_name] = value
        return record

    def records(self) -> list[dict[str, Any]]:
        """Raw provider records currently in scope, in seed order."""
        return [record for record in self._records if self._in_scope(record)]

    def _find_record(self, issue_id: str) -> dict[str, Any] | None:
        for record in self._records:
            if self._in_scope(record) and self._dispatch_id(record) == issue_id:
                return record
        return None

    def _require_record(self, issue_id: str) -> dict[str, Any]:
        record = self._find_record(issue_id)
        if record is None:
            raise KeyError(f"no record with dispatch id {issue_id!r} in the configured scope")
        return record

    # -- OPTIONAL provider-native agent tools (SPEC 10.5, 11.5) --------------

    def agent_tool_specs(self) -> list[ToolSpec]:
        """Tools advertised to the app-server session (SPEC 10.5)."""
        return list(TOOL_SPECS)

    def secret_environment_names(self) -> list[str]:
        """Env names the launcher strips from child environments (SPEC 10.5/15.3)."""
        return [self._secret_env] if self._secret_env else []

    async def execute_agent_tool(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Run a provider-native tool host-side (SPEC 10.5).

        Every failure path returns a structured :class:`ToolResult` rather than
        raising, so an unsupported name, an out-of-scope target, a bad argument,
        or an injected tracker fault continues the session instead of stalling.
        """
        if name not in _TOOL_NAMES:
            return ToolResult.failure(
                f"unsupported tool: {name}", tool=name, supported=list(_TOOL_NAMES)
            )
        # Typed as a mapping, but a live app-server can send anything; a bad
        # payload must not stall the session (SPEC 10.5).
        payload: Any = {} if arguments is None else arguments
        if not isinstance(payload, Mapping):
            return ToolResult.failure(
                "tool arguments must be a JSON object",
                tool=name,
                got=type(payload).__name__,
            )
        try:
            self._provider_call(f"tool:{name}")
        except TrackerError as exc:
            return ToolResult.failure(exc.message, tool=name, category=exc.category)

        record, failure = self._tool_target(payload, context)
        if failure is not None:
            return failure
        assert record is not None

        if name == "memory_get_issue":
            return self._tool_get_issue(record)
        if name == "memory_add_comment":
            return self._tool_add_comment(record, payload)
        return self._tool_set_state(record, payload)

    def _tool_target(
        self, arguments: Mapping[str, Any], context: ToolContext | None
    ) -> tuple[dict[str, Any] | None, ToolResult | None]:
        issue_id = _text(arguments.get("issue_id"))
        if issue_id is None and context is not None and context.issue is not None:
            issue_id = context.issue.id
        if issue_id is None:
            return None, ToolResult.failure(
                "issue_id is required when no current issue is in tool context"
            )
        record = self._find_record(issue_id)
        if record is None:
            # Authorization boundary: tools see exactly the configured scope.
            return None, ToolResult.failure(
                f"issue {issue_id!r} is not visible in the configured tracker scope",
                issue_id=issue_id,
                scope=self._scope,
            )
        return record, None

    def _tool_get_issue(self, record: dict[str, Any]) -> ToolResult:
        try:
            issue = self._normalize(record, where="memory_get_issue")
        except TrackerResponseError as exc:
            return ToolResult.failure(exc.message, category=exc.category)
        comments = record.get("comments")
        return ToolResult.success(
            {
                "issue": issue.to_template_context(),
                "comments": list(comments) if isinstance(comments, list) else [],
            }
        )

    def _tool_add_comment(self, record: dict[str, Any], arguments: Mapping[str, Any]) -> ToolResult:
        body = arguments.get("body")
        if not isinstance(body, str) or not body.strip():
            return ToolResult.failure("'body' must be a non-empty string")
        comments = record.setdefault("comments", [])
        if not isinstance(comments, list):
            return ToolResult.failure("provider record has a non-list 'comments' field")
        entry = {"body": body, "created_at": self._clock().isoformat()}
        comments.append(entry)
        return ToolResult.success(
            {
                "issue_id": self._dispatch_id(record),
                "comment_index": len(comments) - 1,
                "created_at": entry["created_at"],
            }
        )

    def _tool_set_state(self, record: dict[str, Any], arguments: Mapping[str, Any]) -> ToolResult:
        state = arguments.get("state")
        if not isinstance(state, str) or not state.strip():
            return ToolResult.failure("'state' must be a non-empty string")
        previous = record.get("state")
        record["state"] = state.strip()
        return ToolResult.success(
            {
                "issue_id": self._dispatch_id(record),
                "previous_state": previous,
                "state": record["state"],
            }
        )

    # -- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        """Release the (nonexistent) transport. Idempotent; reads then fail."""
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"MemoryTrackerAdapter(records={len(self._records)}, scope={self._scope!r}, "
            f"page_size={self._page_size}, faults_armed={self.faults.armed}, "
            f"provider_calls={self.provider_calls})"
        )
