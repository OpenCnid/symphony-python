"""Linear tracker adapter — ``tracker.kind: linear`` (SPEC 11).

Linear's work object maps onto the normalized Issue of SPEC 4.1.1 more directly
than most providers: it already has a stable UUID, a human ticket key, a
provider-native workflow state name, labels, and RFC 3339 timestamps. The
interesting work is therefore in the three places where the models *do not*
line up:

1. **Priority.** Linear's scale is ``0..4`` where ``0`` means *No priority*,
   while SPEC 8.2 privileges ``1..4`` and sorts everything else with null.
   Passing ``0`` through raw would rank unprioritized work into a bucket the
   spec did not intend, and SPEC 4.1.1 ("lower numbers are higher priority")
   would read it as the *most* urgent value. This adapter maps ``0`` to
   ``None`` and preserves the raw integer in ``native_ref``. SPEC 11.3 permits
   a different mapping only when it is documented, so it is documented in
   ``docs/adapters/linear.md``.
2. **States.** ``active_states``/``terminal_states`` compare against Linear
   workflow state *names* (``"In Progress"``), not the ``WorkflowState.type``
   enum. The type is used only to derive ``dispatchable`` and to decide whether
   a blocking issue is still open.
3. **Blockers.** Linear expresses "A blocks B" as an ``IssueRelation`` of type
   ``blocks``; from B's side it appears in ``inverseRelations``. That is the
   only blocker shape this adapter claims to understand (SPEC 11.3 forbids
   inventing blocker semantics an adapter cannot represent reliably).

Provider-native agent tools (SPEC 10.5, 11.5) ship here rather than as generic
orchestrator CRUD: state transitions, comments, and PR-link attachment are
exactly the ticket mutations the spec assigns to the agent. Every tool runs
host-side with the configured credential and receives the normalized issue as
context, never the credential itself.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, ClassVar

import httpx

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
from symphony.models import Issue
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
    "ATTACHMENT_LINK_MUTATION",
    "COMMENT_CREATE_MUTATION",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_SECRET_ENV",
    "DEFAULT_TIMEOUT_MS",
    "ISSUES_QUERY",
    "ISSUE_UPDATE_MUTATION",
    "LABEL_PAGE_SIZE",
    "LINEAR_ENDPOINT",
    "LINEAR_NO_PRIORITY",
    "LINEAR_TOOL_SPECS",
    "MAX_PAGE_SIZE",
    "NON_DISPATCHABLE_STATE_TYPES",
    "RELATION_PAGE_SIZE",
    "TEAMS_BY_KEY_QUERY",
    "TEAM_STATES_QUERY",
    "TERMINAL_STATE_TYPES",
    "LinearAdapter",
    "map_priority",
    "normalize_issue",
]

# --------------------------------------------------------------------------
# Provider constants (SPEC 11.2 — the adapter owns endpoint and request limits)
# --------------------------------------------------------------------------

LINEAR_ENDPOINT = "https://api.linear.app/graphql"

#: Env var consulted when ``provider.api_key`` is omitted, and always declared
#: to the launcher for stripping (SPEC 15.3).
DEFAULT_SECRET_ENV = "LINEAR_API_KEY"

DEFAULT_PAGE_SIZE = 50
#: Linear rejects ``first`` above 250 on a connection.
MAX_PAGE_SIZE = 250
DEFAULT_MAX_PAGES = 100
DEFAULT_TIMEOUT_MS = 30_000

#: Nested connections are NOT paginated; an issue carrying more labels or
#: relations than these caps has the surplus dropped. Documented as a provider
#: request limit in the adapter profile (SPEC 11.2).
LABEL_PAGE_SIZE = 50
RELATION_PAGE_SIZE = 25

#: Linear's ``priority`` sentinel for "No priority".
LINEAR_NO_PRIORITY = 0

#: ``WorkflowState.type`` values meaning the work object is finished or
#: abandoned. Used to decide whether a *blocking* issue still blocks.
TERMINAL_STATE_TYPES: tuple[str, ...] = ("completed", "canceled")

#: ``WorkflowState.type`` values that make an issue ineligible regardless of
#: the configured ``active_states``. ``completed`` is deliberately absent: a
#: team may legitimately configure a completed-type state as an active handoff
#: state (SPEC 11.5), and silently refusing to dispatch a state the operator
#: listed would be worse than honoring the explicit configuration.
NON_DISPATCHABLE_STATE_TYPES: tuple[str, ...] = ("canceled", "triage")

_AUTH_SCHEMES = ("raw", "bearer")

# GraphQL ``extensions.code`` values, grouped by SPEC 11.4 category.
_GQL_RATE_LIMIT_CODES = frozenset({"RATELIMITED", "RATE_LIMITED", "USAGE_LIMIT_EXCEEDED"})
_GQL_STATUS_CODES = frozenset(
    {
        "AUTHENTICATION_ERROR",
        "FORBIDDEN",
        "FEATURE_NOT_ACCESSIBLE",
        "UNAUTHENTICATED",
        "USER_ERROR",
    }
)
_GQL_TRANSPORT_CODES = frozenset({"INTERNAL_SERVER_ERROR", "NETWORK_ERROR", "SHUTDOWN", "TIMEOUT"})


# --------------------------------------------------------------------------
# Logging (SPEC 11.1 — omitted malformed records SHOULD be logged)
# --------------------------------------------------------------------------

try:  # pragma: no cover - depends on sibling module landing
    from symphony.observability.logging import get_logger as _get_logger
except ImportError:  # pragma: no cover - fallback until it lands
    _get_logger = None


class _FallbackLogger:
    """stdlib shim matching the ``StructuredLogger`` call shape (SPEC 13.1)."""

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def _emit(self, level: int, message: str, fields: dict[str, Any]) -> None:
        if fields:
            message = f"{message} " + " ".join(f"{k}={v}" for k, v in sorted(fields.items()))
        self._log.log(level, message)

    def debug(self, message: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, message, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit(logging.INFO, message, fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._emit(logging.WARNING, message, fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit(logging.ERROR, message, fields)


_LOG: Any = _get_logger(__name__) if _get_logger is not None else _FallbackLogger(__name__)


# --------------------------------------------------------------------------
# GraphQL documents
# --------------------------------------------------------------------------

_ISSUE_FIELDS = """
  id
  identifier
  number
  title
  description
  priority
  priorityLabel
  url
  branchName
  createdAt
  updatedAt
  archivedAt
  state { id name type }
  assignee { id }
  team { id key }
  project { id name }
  labels(first: $labelPageSize) { nodes { id name } }
  inverseRelations(first: $relationPageSize) {
    nodes {
      type
      issue { id identifier state { name type } }
    }
  }
"""

ISSUES_QUERY = (
    """
query SymphonyLinearIssues(
  $filter: IssueFilter
  $first: Int!
  $after: String
  $labelPageSize: Int!
  $relationPageSize: Int!
) {
  issues(filter: $filter, first: $first, after: $after, orderBy: createdAt) {
    pageInfo { hasNextPage endCursor }
    nodes {"""
    + _ISSUE_FIELDS
    + """}
  }
}
"""
)

TEAMS_BY_KEY_QUERY = """
query SymphonyLinearTeamByKey($key: String!) {
  teams(filter: { key: { eq: $key } }, first: 1) {
    nodes { id key name }
  }
}
"""

TEAM_STATES_QUERY = """
query SymphonyLinearTeamStates($teamId: String!) {
  team(id: $teamId) {
    id
    key
    states(first: 100) {
      nodes { id name type position }
    }
  }
}
"""

ISSUE_UPDATE_MUTATION = """
mutation SymphonyLinearIssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue {
      id
      identifier
      updatedAt
      state { id name type }
    }
  }
}
"""

COMMENT_CREATE_MUTATION = """
mutation SymphonyLinearCommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment { id url createdAt }
  }
}
"""

ATTACHMENT_LINK_MUTATION = """
mutation SymphonyLinearAttachmentLinkURL($issueId: String!, $url: String!, $title: String) {
  attachmentLinkURL(issueId: $issueId, url: $url, title: $title) {
    success
    attachment { id url title }
  }
}
"""


# --------------------------------------------------------------------------
# Normalization helpers (SPEC 11.3)
# --------------------------------------------------------------------------


def _text_or_none(value: Any) -> str | None:
    """Nullable string field: blank and non-string values normalize to ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _sub(record: dict[str, Any], key: str) -> dict[str, Any]:
    """Read a nested object, tolerating ``null`` and wrong types."""
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def map_priority(raw: Any) -> int | None:
    """Map Linear's ``0..4`` priority onto the SPEC 8.2 scheduler buckets.

    Linear: ``0`` No priority, ``1`` Urgent, ``2`` High, ``3`` Medium,
    ``4`` Low. ``1..4`` already agree with SPEC 4.1.1 ("lower numbers are
    higher priority") and pass through unchanged.

    ``0`` normalizes to ``None``. SPEC 11.3 ranks ``1..4`` ahead of
    null/unknown and sorts other integers with null; ``0`` means *absence of a
    priority*, which is the null bucket semantically. Emitting the literal
    ``0`` would depend on every consumer bucketing it correctly and would read
    as "highest priority" to any reader applying SPEC 4.1.1 naively. SPEC 11.3
    allows a documented alternative mapping, and this is it.

    Values outside ``0..4`` are unusable provider metadata for a nullable
    field and normalize to ``None`` (SPEC 11.3). The raw integer survives in
    ``native_ref['priority_raw']``, so the mapping is lossless.
    """
    value = coerce_priority(raw)
    if value is None or value == LINEAR_NO_PRIORITY:
        return None
    if 1 <= value <= 4:
        return value
    return None


def _relation_blockers(record: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Extract ``blocked_by`` candidates and whether any blocker is still open.

    Linear models "A blocks B" as an ``IssueRelation`` with ``type='blocks'``
    whose ``issue`` is A. From B the relation surfaces in ``inverseRelations``,
    so every inverse ``blocks`` relation names an issue blocking this one. No
    other relation type is interpreted as a blocker (SPEC 11.3).
    """
    nodes = _sub(record, "inverseRelations").get("nodes")
    if not isinstance(nodes, list):
        return [], False

    raw: list[dict[str, Any]] = []
    has_open = False
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "blocks":
            continue
        blocker = _sub(node, "issue")
        if not blocker:
            continue
        state = _sub(blocker, "state")
        raw.append(
            {
                "id": blocker.get("id"),
                "identifier": blocker.get("identifier"),
                "state": state.get("name"),
            }
        )
        state_type = _text_or_none(state.get("type"))
        # An unknown state type is treated as open: refusing to dispatch is the
        # safe direction when blocker status cannot be established.
        if state_type is None or state_type.lower() not in TERMINAL_STATE_TYPES:
            has_open = True
    return raw, has_open


def normalize_issue(
    record: dict[str, Any],
    *,
    where: str = "linear.issues",
    require_assignee: bool = False,
    block_on_open_blockers: bool = True,
    non_dispatchable_state_types: tuple[str, ...] = NON_DISPATCHABLE_STATE_TYPES,
) -> Issue:
    """Map one Linear ``Issue`` node onto the normalized model (SPEC 4.1.1, 11.3).

    Raises :class:`~symphony.errors.TrackerResponseError` when ``id``,
    ``identifier``, ``title``, or the workflow state name is missing — the four
    fields whose absence makes a record malformed (SPEC 11.1). Every other
    field degrades to ``None`` or an empty collection.
    """
    state = _sub(record, "state")
    team = _sub(record, "team")
    project = _sub(record, "project")

    required = {
        "id": record.get("id"),
        "identifier": record.get("identifier"),
        "title": record.get("title"),
        "state": state.get("name"),
    }
    issue_id = require_str(required, "id", where=where)
    identifier = require_str(required, "identifier", where=where)
    title = require_str(required, "title", where=where)
    state_name = require_str(required, "state", where=where)

    state_type = (_text_or_none(state.get("type")) or "").lower()
    assignee_id = _text_or_none(_sub(record, "assignee").get("id"))
    raw_blockers, has_open_blocker = _relation_blockers(record)

    archived = _text_or_none(record.get("archivedAt")) is not None
    dispatchable = (
        not archived
        and state_type not in non_dispatchable_state_types
        and not (block_on_open_blockers and has_open_blocker)
        and not (require_assignee and assignee_id is None)
    )

    raw_priority = coerce_priority(record.get("priority"))
    number = coerce_priority(record.get("number"))

    native_ref: dict[str, Any] = {
        "issue_id": issue_id,
        "number": number,
        "team_id": _text_or_none(team.get("id")),
        "team_key": _text_or_none(team.get("key")),
        "state_id": _text_or_none(state.get("id")),
        "state_name": state_name,
        "state_type": state_type or None,
        "priority_raw": raw_priority,
        "priority_label": _text_or_none(record.get("priorityLabel")),
        "project_id": _text_or_none(project.get("id")),
        "project_name": _text_or_none(project.get("name")),
    }

    return Issue(
        id=issue_id,
        identifier=identifier,
        title=title,
        # Provider spelling is preserved; only comparisons fold case (SPEC 11.3).
        state=state_name,
        dispatchable=dispatchable,
        native_ref=native_ref,
        description=_text_or_none(record.get("description")),
        priority=map_priority(record.get("priority")),
        branch_name=_text_or_none(record.get("branchName")),
        url=_text_or_none(record.get("url")),
        assignee_id=assignee_id,
        labels=normalize_labels(_sub(record, "labels").get("nodes")),
        blocked_by=coerce_blockers(raw_blockers),
        created_at=parse_rfc3339(record.get("createdAt")),
        updated_at=parse_rfc3339(record.get("updatedAt")),
    )


# --------------------------------------------------------------------------
# Provider-native agent tools (SPEC 10.5, 11.5)
# --------------------------------------------------------------------------

_ISSUE_ID_PROPERTY = {
    "type": "string",
    "description": (
        "Optional Linear issue UUID. Omit to target the issue this run was "
        "dispatched for. If supplied it MUST match that issue."
    ),
}

LINEAR_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="linear_set_issue_state",
        description=(
            "Move the current Linear issue to a workflow state by name "
            "(case-insensitive). Use linear_list_workflow_states first if the "
            "exact state name is unknown."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "state_name": {
                    "type": "string",
                    "description": "Target workflow state name, e.g. 'In Review'.",
                },
                "issue_id": _ISSUE_ID_PROPERTY,
            },
            "required": ["state_name"],
            "additionalProperties": False,
        },
        mutates_tracker=True,
    ),
    ToolSpec(
        name="linear_add_comment",
        description="Post a Markdown comment on the current Linear issue.",
        input_schema={
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "Markdown comment body."},
                "issue_id": _ISSUE_ID_PROPERTY,
            },
            "required": ["body"],
            "additionalProperties": False,
        },
        mutates_tracker=True,
    ),
    ToolSpec(
        name="linear_attach_link",
        description=(
            "Attach a URL (typically a pull request) to the current Linear "
            "issue. Re-attaching the same URL updates the existing attachment "
            "rather than creating a duplicate."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http(s) URL to attach."},
                "title": {"type": "string", "description": "Optional attachment title."},
                "issue_id": _ISSUE_ID_PROPERTY,
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        mutates_tracker=True,
    ),
    ToolSpec(
        name="linear_list_workflow_states",
        description=(
            "List the workflow states available on the current issue's Linear "
            "team, with their names and types. Read-only."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        mutates_tracker=False,
    ),
)


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


@register_adapter
class LinearAdapter(TrackerAdapter):
    """Linear GraphQL adapter (SPEC 11.1, 11.2).

    Scope is one Linear team, optionally narrowed to a project or assignee.
    Linear ticket identifiers embed the team key (``ENG-123``) and team keys
    are unique per workspace, so ``identifier`` is already unique within any
    scope reachable by one API key — no extra disambiguation is required
    (SPEC 4.1.1).

    The credential is resolved once at construction so SPEC 6.3 dispatch
    preflight fails fast, and a workflow reload builds a *new* adapter, which
    is what binds tool specs and tracker settings to one session snapshot
    (SPEC 10.5).
    """

    kind: ClassVar[str] = "linear"

    #: SPEC 5.3.1 permits omitting ``active_states``/``terminal_states`` when
    #: the adapter profile documents defaults. These match Linear's default
    #: team workflow.
    default_active_states: ClassVar[tuple[str, ...]] = ("Todo", "In Progress")
    default_terminal_states: ClassVar[tuple[str, ...]] = ("Done", "Canceled", "Duplicate")

    _TOOL_HANDLERS: ClassVar[dict[str, str]] = {
        "linear_set_issue_state": "_tool_set_issue_state",
        "linear_add_comment": "_tool_add_comment",
        "linear_attach_link": "_tool_attach_link",
        "linear_list_workflow_states": "_tool_list_workflow_states",
    }

    def __init__(
        self,
        provider: dict[str, Any],
        *,
        active_states: Any = None,
        terminal_states: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_: Any,
    ) -> None:
        super().__init__(provider if isinstance(provider, dict) else {})
        cfg = self.provider

        self.active_states: tuple[str, ...] = _as_str_tuple(active_states) or (
            self.default_active_states
        )
        self.terminal_states: tuple[str, ...] = _as_str_tuple(terminal_states) or (
            self.default_terminal_states
        )

        self.endpoint = _text_or_none(cfg.get("endpoint")) or LINEAR_ENDPOINT
        self.team_id = _text_or_none(cfg.get("team_id"))
        self.team_key = _text_or_none(cfg.get("team_key"))
        self.project_id = _text_or_none(cfg.get("project_id"))
        self.assignee_id = _text_or_none(cfg.get("assignee_id"))
        if not self.team_id and not self.team_key:
            raise InvalidTrackerConfig(
                "linear tracker.provider requires 'team_key' or 'team_id'",
                kind=self.kind,
                missing=["team_key", "team_id"],
            )

        self.require_assignee = _as_bool(cfg.get("require_assignee"), default=False)
        self.block_on_open_blockers = _as_bool(cfg.get("block_on_open_blockers"), default=True)
        self.non_dispatchable_state_types = (
            _as_lower_tuple(cfg.get("non_dispatchable_state_types"))
            if cfg.get("non_dispatchable_state_types") is not None
            else NON_DISPATCHABLE_STATE_TYPES
        )

        self.page_size = _as_bounded_int(
            cfg.get("page_size"),
            default=DEFAULT_PAGE_SIZE,
            low=1,
            high=MAX_PAGE_SIZE,
            key="page_size",
        )
        self.max_pages = _as_bounded_int(
            cfg.get("max_pages"), default=DEFAULT_MAX_PAGES, low=1, high=100_000, key="max_pages"
        )
        self.timeout_ms = _as_bounded_int(
            cfg.get("timeout_ms"), default=DEFAULT_TIMEOUT_MS, low=1, high=600_000, key="timeout_ms"
        )

        scheme = (_text_or_none(cfg.get("auth_scheme")) or "raw").lower()
        if scheme not in _AUTH_SCHEMES:
            raise InvalidTrackerConfig(
                f"linear tracker.provider.auth_scheme must be one of {list(_AUTH_SCHEMES)}",
                kind=self.kind,
                key="auth_scheme",
            )
        self.auth_scheme = scheme

        self.secret_env_name = _text_or_none(cfg.get("api_key_env"))
        raw_key = cfg.get("api_key")
        if isinstance(raw_key, str) and raw_key.startswith("$") and len(raw_key) > 1:
            self.secret_env_name = raw_key[1:].strip() or self.secret_env_name
        self._api_key = self._resolve_api_key(raw_key)

        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._team_id_cache: str | None = self.team_id

        #: Populated on every read so callers can inspect omitted malformed
        #: records without scraping logs (SPEC 11.1).
        self.last_normalization_report = NormalizationReport()

    # -- construction helpers -------------------------------------------------

    def _resolve_api_key(self, raw_key: Any) -> str:
        """Resolve the credential, validating presence without printing it (SPEC 15.3).

        Accepts a literal string, a ``$VAR_NAME`` indirection, or omission (in
        which case ``api_key_env`` or ``LINEAR_API_KEY`` is read). SPEC 5.3.1:
        a documented secret ``$VAR_NAME`` resolving to an empty string is
        *missing*, not empty.
        """
        env_name = self.secret_env_name or DEFAULT_SECRET_ENV
        if isinstance(raw_key, str) and raw_key.startswith("$") and len(raw_key) > 1:
            value = os.environ.get(raw_key[1:].strip(), "")
        elif isinstance(raw_key, str) and raw_key.strip():
            value = raw_key
        elif raw_key is None or (isinstance(raw_key, str) and not raw_key.strip()):
            value = os.environ.get(env_name, "")
        else:
            raise InvalidTrackerConfig(
                "linear tracker.provider.api_key must be a string or '$VAR_NAME'",
                kind=self.kind,
                key="api_key",
            )

        if not value.strip():
            raise MissingTrackerSecret(
                "linear tracker credential is missing or empty; set "
                f"tracker.provider.api_key or the {env_name} environment variable",
                kind=self.kind,
                key="api_key",
                env=env_name,
            )
        return value.strip()

    def __repr__(self) -> str:
        """Credential-free repr — the object is reachable from the RLM surface."""
        scope = self.team_id or self.team_key
        return f"LinearAdapter(team={scope!r}, endpoint={self.endpoint!r})"

    # -- transport ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        value = f"Bearer {self._api_key}" if self.auth_scheme == "bearer" else self._api_key
        return {
            "Authorization": value,
            "Content-Type": "application/json",
            "User-Agent": "symphony-python/0.1 (+linear-adapter)",
        }

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_ms / 1000.0),
                transport=self._transport,
                headers=self._headers(),
            )
        return self._client

    async def aclose(self) -> None:
        """Release the HTTP client. Safe to call more than once."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _post(self, document: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Execute one GraphQL request and return ``data`` (SPEC 11.4 mapping)."""
        payload = {"query": document, "variables": variables}
        try:
            response = await self._get_client().post(self.endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise TrackerRequestError(
                f"linear request timed out after {self.timeout_ms}ms",
                retryable=True,
                kind=self.kind,
                endpoint=self.endpoint,
                cause=type(exc).__name__,
            ) from exc
        except httpx.HTTPError as exc:
            raise TrackerRequestError(
                f"linear transport failure: {type(exc).__name__}: {exc}",
                retryable=True,
                kind=self.kind,
                endpoint=self.endpoint,
            ) from exc

        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None

        if isinstance(body, dict):
            errors = body.get("errors")
            if isinstance(errors, list) and errors:
                raise self._graphql_error(errors, response)

        if response.status_code == 429:
            raise TrackerRateLimited(
                "linear rate limit exceeded (HTTP 429)",
                retryable=True,
                retry_after_ms=_retry_after_ms(response.headers),
                kind=self.kind,
                status=429,
            )
        if response.status_code >= 400:
            raise TrackerStatusError(
                f"linear returned HTTP {response.status_code}",
                retryable=response.status_code >= 500,
                kind=self.kind,
                status=response.status_code,
            )
        if not isinstance(body, dict):
            raise TrackerResponseError(
                "linear returned a non-JSON-object body",
                kind=self.kind,
                status=response.status_code,
            )

        data = body.get("data")
        if not isinstance(data, dict):
            raise TrackerResponseError(
                "linear response is missing a 'data' object",
                kind=self.kind,
                status=response.status_code,
            )
        return data

    def _graphql_error(self, errors: list[Any], response: httpx.Response) -> TrackerError:
        """Map a GraphQL ``errors`` array onto a SPEC 11.4 category."""
        first = errors[0] if isinstance(errors[0], dict) else {}
        extensions = first.get("extensions") if isinstance(first.get("extensions"), dict) else {}
        code = str(extensions.get("code") or "").upper()
        message = _text_or_none(first.get("message")) or "linear GraphQL error"
        detail = {
            "kind": self.kind,
            "status": response.status_code,
            "graphql_code": code or None,
            "error_count": len(errors),
        }

        if code in _GQL_RATE_LIMIT_CODES or response.status_code == 429:
            return TrackerRateLimited(
                f"linear rate limited: {message}",
                retryable=True,
                retry_after_ms=_retry_after_ms(response.headers),
                **detail,
            )
        if code in _GQL_STATUS_CODES or response.status_code in (401, 403):
            return TrackerStatusError(
                f"linear rejected the request: {message}", retryable=False, **detail
            )
        if code in _GQL_TRANSPORT_CODES or response.status_code >= 500:
            return TrackerRequestError(
                f"linear upstream failure: {message}", retryable=True, **detail
            )
        return TrackerResponseError(f"linear GraphQL error: {message}", retryable=False, **detail)

    # -- REQUIRED read kernel (SPEC 11.1) ------------------------------------

    def _scope_filter(self) -> dict[str, Any]:
        """Provider-side scope selection (SPEC 11.2)."""
        scope: dict[str, Any] = {}
        if self.team_id:
            scope["team"] = {"id": {"eq": self.team_id}}
        elif self.team_key:
            scope["team"] = {"key": {"eq": self.team_key}}
        if self.project_id:
            scope["project"] = {"id": {"eq": self.project_id}}
        if self.assignee_id:
            scope["assignee"] = {"id": {"eq": self.assignee_id}}
        return scope

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        """Issues in scope whose workflow state name matches (SPEC 11.1).

        Matching is provider-side and case-insensitive: the filter is an ``or``
        group of ``eqIgnoreCase`` comparators over ``state.name``, because the
        scheduler compares provider-native state *names* case-insensitively
        (SPEC 5.3.1). ``dispatchable=False`` issues are still returned — the
        scheduler owns that filter.
        """
        wanted = _unique_states(state_names)
        if not wanted:
            # SPEC 11.1: an empty list MUST NOT reach the provider. A list of
            # only blank names can match nothing, so it is treated the same.
            self.last_normalization_report = NormalizationReport()
            return []

        filter_ = self._scope_filter()
        filter_["or"] = [{"state": {"name": {"eqIgnoreCase": name}}} for name in wanted]
        return await self._paginate(filter_, where="linear.fetch_issues_by_states", strict=False)

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        """Full normalized snapshots for opaque dispatch IDs (SPEC 11.1).

        Input IDs are treated as a set. IDs outside the configured scope (or
        archived) are simply absent from the result — the orchestrator reads
        omission as "no longer visible". A malformed *requested* record fails
        the call instead of being omitted, because omission is meaningful here.
        """
        wanted = _unique_ids(issue_ids)
        if not wanted:
            self.last_normalization_report = NormalizationReport()
            return []

        filter_ = self._scope_filter()
        filter_["id"] = {"in": wanted}
        return await self._paginate(filter_, where="linear.fetch_issues_by_ids", strict=True)

    async def _paginate(
        self, filter_: dict[str, Any], *, where: str, strict: bool
    ) -> list[Issue]:
        """Walk Linear's cursor connection, preserving order across pages.

        Pagination integrity failures — a repeated cursor, a truthy
        ``hasNextPage`` with no cursor, or exceeding ``max_pages`` — raise
        :class:`~symphony.errors.TrackerPaginationError` rather than returning
        a silently truncated page set (SPEC 11.4).
        """
        issues: list[Issue] = []
        report = NormalizationReport()
        cursor: str | None = None
        seen: set[str] = set()
        page = 0

        while True:
            page += 1
            if page > self.max_pages:
                raise TrackerPaginationError(
                    f"linear pagination exceeded max_pages={self.max_pages}",
                    kind=self.kind,
                    where=where,
                )
            data = await self._post(
                ISSUES_QUERY,
                {
                    "filter": filter_,
                    "first": self.page_size,
                    "after": cursor,
                    "labelPageSize": LABEL_PAGE_SIZE,
                    "relationPageSize": RELATION_PAGE_SIZE,
                },
            )
            connection = data.get("issues")
            if not isinstance(connection, dict):
                raise TrackerResponseError(
                    "linear response is missing the 'issues' connection",
                    kind=self.kind,
                    where=where,
                )
            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise TrackerResponseError(
                    "linear 'issues.nodes' is not a list", kind=self.kind, where=where
                )

            for node in nodes:
                if not isinstance(node, dict):
                    if strict:
                        raise TrackerResponseError(
                            "linear returned a non-object issue node",
                            kind=self.kind,
                            where=where,
                        )
                    report.omit("non-object issue node")
                    continue
                try:
                    issues.append(
                        normalize_issue(
                            node,
                            where=where,
                            require_assignee=self.require_assignee,
                            block_on_open_blockers=self.block_on_open_blockers,
                            non_dispatchable_state_types=self.non_dispatchable_state_types,
                        )
                    )
                except (TrackerResponseError, ValueError) as exc:
                    if strict:
                        raise
                    report.omit(str(exc))

            page_info = connection.get("pageInfo")
            page_info = page_info if isinstance(page_info, dict) else {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor.strip():
                raise TrackerPaginationError(
                    "linear reported hasNextPage without a usable endCursor",
                    kind=self.kind,
                    where=where,
                )
            if cursor in seen:
                raise TrackerPaginationError(
                    "linear returned a repeated pagination cursor",
                    kind=self.kind,
                    where=where,
                )
            seen.add(cursor)

        self.last_normalization_report = report
        if report.omitted:
            _LOG.warning(
                "tracker.linear.omitted_malformed_records",
                where=where,
                omitted=len(report.omitted),
                first_reason=report.omitted[0],
            )
        return issues

    # -- OPTIONAL provider-native agent tools (SPEC 10.5, 11.5) --------------

    def agent_tool_specs(self) -> list[ToolSpec]:
        """State transition, comment, PR-link, and state discovery tools."""
        return list(LINEAR_TOOL_SPECS)

    def secret_environment_names(self) -> list[str]:
        """Env names the launcher strips from child environments (SPEC 15.3).

        ``LINEAR_API_KEY`` is always declared even when the credential came
        from a literal or a differently named variable: if it is set in the
        host environment the coding-agent child still must not inherit it.
        """
        names = {DEFAULT_SECRET_ENV}
        if self.secret_env_name:
            names.add(self.secret_env_name)
        return sorted(names)

    async def execute_agent_tool(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Run a tool host-side with the configured credential (SPEC 10.5).

        Never raises: an unsupported name, a bad argument, or a provider
        failure all return a structured :class:`ToolResult` failure so the
        session continues instead of stalling.
        """
        handler = self._TOOL_HANDLERS.get(name)
        if handler is None:
            return ToolResult.failure(
                f"unsupported tool: {name}",
                tool=name,
                supported=[spec.name for spec in LINEAR_TOOL_SPECS],
            )
        if not isinstance(arguments, dict):
            return ToolResult.failure("tool arguments must be a JSON object", tool=name)
        try:
            return await getattr(self, handler)(arguments, context)
        except TrackerError as exc:
            return ToolResult.failure(exc.message, tool=name, category=exc.category)
        except Exception as exc:  # never stall the session (SPEC 10.5)
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}", tool=name, category="tracker_error"
            )

    def _target_issue_id(
        self, arguments: dict[str, Any], context: ToolContext | None
    ) -> tuple[str | None, ToolResult | None]:
        """Resolve the issue a mutation may touch.

        Authorization boundary: tools act only on the issue this run was
        dispatched for. The credential's own reach is wider, so the adapter —
        not the model — decides the target (SPEC 10.5).
        """
        issue = context.issue if context is not None else None
        if issue is None:
            return None, ToolResult.failure("no issue in tool context")

        native = issue.native_ref if isinstance(issue.native_ref, dict) else {}
        resolved = _text_or_none(native.get("issue_id")) or issue.id

        requested = arguments.get("issue_id")
        if requested is not None:
            requested_id = _text_or_none(requested)
            if requested_id is None:
                return None, ToolResult.failure("issue_id must be a non-empty string")
            if requested_id not in {resolved, issue.id}:
                return None, ToolResult.failure(
                    "issue_id does not match the issue in context; tools may only "
                    "act on the dispatched issue",
                    requested=requested_id,
                    allowed=issue.identifier,
                )
        return resolved, None

    async def _resolve_team_id(self, context: ToolContext | None) -> str:
        """Team id for state lookups: context issue, then config, then by key."""
        issue = context.issue if context is not None else None
        if issue is not None and isinstance(issue.native_ref, dict):
            from_issue = _text_or_none(issue.native_ref.get("team_id"))
            if from_issue:
                return from_issue
        if self._team_id_cache:
            return self._team_id_cache
        if self.team_key:
            data = await self._post(TEAMS_BY_KEY_QUERY, {"key": self.team_key})
            nodes = _sub(data, "teams").get("nodes")
            if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict):
                team_id = _text_or_none(nodes[0].get("id"))
                if team_id:
                    self._team_id_cache = team_id
                    return team_id
        raise InvalidTrackerConfig(
            "cannot resolve a linear team id for tool execution",
            kind=self.kind,
            team_key=self.team_key,
        )

    async def _workflow_states(self, context: ToolContext | None) -> list[dict[str, Any]]:
        team_id = await self._resolve_team_id(context)
        data = await self._post(TEAM_STATES_QUERY, {"teamId": team_id})
        nodes = _sub(_sub(data, "team"), "states").get("nodes")
        if not isinstance(nodes, list):
            raise TrackerResponseError(
                "linear team states response is malformed", kind=self.kind, team_id=team_id
            )
        return [n for n in nodes if isinstance(n, dict) and _text_or_none(n.get("name"))]

    async def _tool_list_workflow_states(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        states = await self._workflow_states(context)
        return ToolResult.success(
            {
                "states": [
                    {
                        "id": _text_or_none(s.get("id")),
                        "name": _text_or_none(s.get("name")),
                        "type": _text_or_none(s.get("type")),
                    }
                    for s in states
                ]
            }
        )

    async def _tool_set_issue_state(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        issue_id, failure = self._target_issue_id(arguments, context)
        if failure is not None:
            return failure
        wanted = _text_or_none(arguments.get("state_name"))
        if wanted is None:
            return ToolResult.failure("state_name is required and must be a non-empty string")

        states = await self._workflow_states(context)
        match = next(
            (s for s in states if (_text_or_none(s.get("name")) or "").lower() == wanted.lower()),
            None,
        )
        if match is None:
            return ToolResult.failure(
                f"no workflow state named {wanted!r} on this team",
                available=[_text_or_none(s.get("name")) for s in states],
            )

        data = await self._post(
            ISSUE_UPDATE_MUTATION, {"id": issue_id, "input": {"stateId": match.get("id")}}
        )
        payload = _sub(data, "issueUpdate")
        if not payload.get("success"):
            return ToolResult.failure(
                "linear rejected the state transition", issue_id=issue_id, state=wanted
            )
        updated = _sub(payload, "issue")
        return ToolResult.success(
            {
                "issue_id": _text_or_none(updated.get("id")) or issue_id,
                "identifier": _text_or_none(updated.get("identifier")),
                "state": _text_or_none(_sub(updated, "state").get("name")) or wanted,
                "state_type": _text_or_none(_sub(updated, "state").get("type")),
                "updated_at": _text_or_none(updated.get("updatedAt")),
            }
        )

    async def _tool_add_comment(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        issue_id, failure = self._target_issue_id(arguments, context)
        if failure is not None:
            return failure
        body = arguments.get("body")
        if not isinstance(body, str) or not body.strip():
            return ToolResult.failure("body is required and must be a non-empty string")

        data = await self._post(
            COMMENT_CREATE_MUTATION, {"input": {"issueId": issue_id, "body": body}}
        )
        payload = _sub(data, "commentCreate")
        if not payload.get("success"):
            return ToolResult.failure("linear rejected the comment", issue_id=issue_id)
        comment = _sub(payload, "comment")
        return ToolResult.success(
            {
                "comment_id": _text_or_none(comment.get("id")),
                "url": _text_or_none(comment.get("url")),
                "issue_id": issue_id,
            }
        )

    async def _tool_attach_link(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        issue_id, failure = self._target_issue_id(arguments, context)
        if failure is not None:
            return failure
        url = _text_or_none(arguments.get("url"))
        if url is None:
            return ToolResult.failure("url is required and must be a non-empty string")
        if not url.lower().startswith(("http://", "https://")):
            return ToolResult.failure("url must be an absolute http(s) URL", url=url)
        title = _text_or_none(arguments.get("title"))

        data = await self._post(
            ATTACHMENT_LINK_MUTATION, {"issueId": issue_id, "url": url, "title": title}
        )
        payload = _sub(data, "attachmentLinkURL")
        if not payload.get("success"):
            return ToolResult.failure(
                "linear rejected the attachment", issue_id=issue_id, url=url
            )
        attachment = _sub(payload, "attachment")
        return ToolResult.success(
            {
                "attachment_id": _text_or_none(attachment.get("id")),
                "url": _text_or_none(attachment.get("url")) or url,
                "title": _text_or_none(attachment.get("title")),
                "issue_id": issue_id,
            }
        )


# --------------------------------------------------------------------------
# Small config coercions
# --------------------------------------------------------------------------


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())


def _as_lower_tuple(value: Any) -> tuple[str, ...]:
    return tuple(v.lower() for v in _as_str_tuple(value))


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _as_bounded_int(value: Any, *, default: int, low: int, high: int, key: str) -> int:
    if value is None:
        return default
    parsed = coerce_priority(value)
    if parsed is None or not (low <= parsed <= high):
        raise InvalidTrackerConfig(
            f"linear tracker.provider.{key} must be an integer in [{low}, {high}]",
            kind=LinearAdapter.kind,
            key=key,
        )
    return parsed


def _unique_states(state_names: Any) -> list[str]:
    """Trim, drop blanks, and de-duplicate case-insensitively, preserving order."""
    if not isinstance(state_names, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in state_names:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    return out


def _unique_ids(issue_ids: Any) -> list[str]:
    """SPEC 11.1: input IDs are treated as a set."""
    if not isinstance(issue_ids, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in issue_ids:
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _retry_after_ms(headers: Any) -> int | None:
    """Best-effort ``retry_after_ms`` from Linear's rate-limit headers.

    ``Retry-After`` is seconds; ``X-RateLimit-Requests-Reset`` is a unix epoch
    (Linear emits milliseconds). Never raises — an unusable header simply
    yields ``None``.
    """
    try:
        raw = headers.get("Retry-After")
        if raw is not None:
            return max(int(float(raw)) * 1000, 0)
        reset = headers.get("X-RateLimit-Requests-Reset")
        if reset is not None:
            value = float(reset)
            epoch_ms = value if value > 1e11 else value * 1000.0
            return max(int(epoch_ms - time.time() * 1000.0), 0)
    except (TypeError, ValueError, AttributeError):
        return None
    return None
