"""GitHub Projects v2 tracker adapter — SPEC 11 (11.1-11.5), 10.5, 15.3.

Scope is one ProjectV2 board owned by an organization or user. The *project
item* is the stable dispatch identity (``Issue.id``) because the item is what
carries board state; the underlying issue's node ID, repository, and number are
distinct underlying IDs preserved in ``native_ref`` (SPEC 11.2, 17.3).

Board state lives in a single-select project field (``Status`` by default), not
in the issue's own ``OPEN``/``CLOSED`` state, so configured ``active_states``
and ``terminal_states`` compare against that field value.

The profile REQUIRED by SPEC 11.2 lives at ``docs/adapters/github.md``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any, ClassVar

import httpx

from symphony.errors import (
    InvalidTrackerConfig,
    MissingTrackerSecret,
    TrackerPaginationError,
    TrackerRateLimited,
    TrackerRequestError,
    TrackerResponseError,
    TrackerStatusError,
)
from symphony.models import Issue, normalize_state
from symphony.trackers.base import (
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

__all__ = ["GITHUB_KIND", "GitHubProjectsAdapter"]

GITHUB_KIND = "github"

_LOG = logging.getLogger("symphony.trackers.github")

# GitHub's `nodes(ids:)` root field accepts at most 100 IDs per request.
_MAX_NODE_IDS = 100
# GitHub's connection `first:` argument is capped at 100.
_MAX_PAGE_SIZE = 100

_DEFAULT_ENDPOINT = "https://api.github.com/graphql"
_DEFAULT_USER_AGENT = "symphony-python/0.1 (github-projects-v2)"
_DEFAULT_SECONDARY_RETRY_MS = 60_000

# SPEC 11.3: priorities 1..4 rank ahead of null. GitHub's stock single-select
# priority fields spell these as words; `P<n>` is the other common convention.
_DEFAULT_PRIORITY_MAP: dict[str, int] = {"urgent": 1, "high": 2, "medium": 3, "low": 4}
_P_NUMBER = re.compile(r"^p\s*(\d+)$")

_VAR_REF = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")

_OWNER_ROOTS = {"organization": "organization", "user": "user"}

_BLOCKED_BY_SELECTION = """
      blockedBy(first: 20) {
        nodes { id number state repository { nameWithOwner } }
      }
"""

_ITEM_FRAGMENT = """
fragment SymphonyItem on ProjectV2Item {
  id
  isArchived
  type
  createdAt
  updatedAt
  project {
    id
    number
    title
    url
    owner {
      __typename
      ... on Organization { login }
      ... on User { login }
    }
  }
  fieldValues(first: 32) {
    nodes {
      __typename
      ... on ProjectV2ItemFieldSingleSelectValue {
        name
        optionId
        field { ... on ProjectV2FieldCommon { name } }
      }
      ... on ProjectV2ItemFieldNumberValue {
        number
        field { ... on ProjectV2FieldCommon { name } }
      }
      ... on ProjectV2ItemFieldTextValue {
        text
        field { ... on ProjectV2FieldCommon { name } }
      }
    }
  }
  content {
    __typename
    ... on DraftIssue { id title body createdAt updatedAt }
    ... on Issue {
      id
      number
      title
      body
      url
      state
      stateReason
      createdAt
      updatedAt
      repository { id name nameWithOwner owner { login } }
      assignees(first: 10) { nodes { id login } }
      labels(first: 50) { nodes { name } }
__BLOCKED_BY__
    }
    ... on PullRequest {
      id
      number
      title
      body
      url
      state
      createdAt
      updatedAt
      repository { id name nameWithOwner owner { login } }
      assignees(first: 10) { nodes { id login } }
      labels(first: 50) { nodes { name } }
    }
  }
}
"""

_LIST_QUERY = """
query SymphonyProjectItems($owner: String!, $number: Int!, $first: Int!, $after: String) {
  __ROOT__(login: $owner) {
    projectV2(number: $number) {
      id
      number
      title
      url
      items(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { ...SymphonyItem }
      }
    }
  }
}
"""

_BY_IDS_QUERY = """
query SymphonyProjectItemsByIds($ids: [ID!]!) {
  nodes(ids: $ids) {
    __typename
    ...SymphonyItem
  }
}
"""

_STATUS_FIELD_QUERY = """
query SymphonyStatusField($owner: String!, $number: Int!, $field: String!) {
  __ROOT__(login: $owner) {
    projectV2(number: $number) {
      id
      field(name: $field) {
        __typename
        ... on ProjectV2SingleSelectField {
          id
          name
          options { id name }
        }
      }
    }
  }
}
"""

_SET_STATUS_MUTATION = """
mutation SymphonySetStatus($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(
    input: {
      projectId: $project
      itemId: $item
      fieldId: $field
      value: { singleSelectOptionId: $option }
    }
  ) {
    projectV2Item { id }
  }
}
"""

_ADD_COMMENT_MUTATION = """
mutation SymphonyAddComment($subject: ID!, $body: String!) {
  addComment(input: { subjectId: $subject, body: $body }) {
    commentEdge { node { id url createdAt } }
  }
}
"""


# --------------------------------------------------------------------------
# Provider config coercion (SPEC 5.3.1 — adapter owns its provider schema)
# --------------------------------------------------------------------------


def _cfg_str(provider: dict[str, Any], key: str, default: str | None) -> str | None:
    raw = provider.get(key, default)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidTrackerConfig(
            f"tracker.provider.{key} must be a non-empty string", key=key, kind=GITHUB_KIND
        )
    return raw.strip()


def _cfg_int(provider: dict[str, Any], key: str, default: int, *, lo: int, hi: int) -> int:
    raw = provider.get(key, default)
    if isinstance(raw, bool) or raw is None:
        raise InvalidTrackerConfig(
            f"tracker.provider.{key} must be an integer", key=key, kind=GITHUB_KIND
        )
    if isinstance(raw, str):
        try:
            raw = int(raw.strip())
        except ValueError as exc:
            raise InvalidTrackerConfig(
                f"tracker.provider.{key} must be an integer, got {raw!r}",
                key=key,
                kind=GITHUB_KIND,
            ) from exc
    if not isinstance(raw, int):
        raise InvalidTrackerConfig(
            f"tracker.provider.{key} must be an integer", key=key, kind=GITHUB_KIND
        )
    if not lo <= raw <= hi:
        raise InvalidTrackerConfig(
            f"tracker.provider.{key} must be between {lo} and {hi}, got {raw}",
            key=key,
            kind=GITHUB_KIND,
        )
    return raw


def _cfg_bool(provider: dict[str, Any], key: str, default: bool) -> bool:
    raw = provider.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
        return raw.strip().lower() == "true"
    raise InvalidTrackerConfig(
        f"tracker.provider.{key} must be a boolean", key=key, kind=GITHUB_KIND
    )


def _cfg_str_list(provider: dict[str, Any], key: str) -> tuple[str, ...]:
    raw = provider.get(key, ())
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, Iterable):
        raise InvalidTrackerConfig(
            f"tracker.provider.{key} must be a list of strings", key=key, kind=GITHUB_KIND
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise InvalidTrackerConfig(
                f"tracker.provider.{key} entries must be non-empty strings",
                key=key,
                kind=GITHUB_KIND,
            )
        out.append(item.strip().lower())
    return tuple(dict.fromkeys(out))


def _cfg_priority_map(provider: dict[str, Any]) -> dict[str, int]:
    raw = provider.get("priority_map")
    merged = dict(_DEFAULT_PRIORITY_MAP)
    if raw is None:
        return merged
    if not isinstance(raw, dict):
        raise InvalidTrackerConfig(
            "tracker.provider.priority_map must be a mapping of option name to integer",
            key="priority_map",
            kind=GITHUB_KIND,
        )
    for name, value in raw.items():
        priority = coerce_priority(value)
        if not isinstance(name, str) or not name.strip() or priority is None:
            raise InvalidTrackerConfig(
                "tracker.provider.priority_map entries must map a non-empty option name "
                "to an integer",
                key="priority_map",
                kind=GITHUB_KIND,
            )
        merged[name.strip().lower()] = priority
    return merged


@register_adapter
class GitHubProjectsAdapter(TrackerAdapter):
    """Read kernel plus provider-native tools for a GitHub Projects v2 board.

    SPEC 11.2: the adapter owns endpoint, authentication, transport, timeouts,
    pagination, rate-limit handling, scope selection, normalization, the
    ``id``/``native_ref`` split, and the derivation of ``dispatchable``.
    """

    kind: ClassVar[str] = GITHUB_KIND

    #: SPEC 5.3.1 permits omitting ``active_states``/``terminal_states`` when
    #: the adapter profile documents defaults. These are the option names in
    #: GitHub's built-in project ``Status`` field.
    default_active_states: ClassVar[tuple[str, ...]] = ("Todo", "In Progress")
    default_terminal_states: ClassVar[tuple[str, ...]] = ("Done",)

    def __init__(
        self,
        provider: dict[str, Any],
        *,
        active_states: Sequence[str] | None = None,
        terminal_states: Sequence[str] | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        **_: Any,
    ) -> None:
        if not isinstance(provider, dict):
            raise InvalidTrackerConfig(
                "tracker.provider must be an object", kind=GITHUB_KIND, key="provider"
            )
        super().__init__(provider)

        self.active_states: tuple[str, ...] = tuple(active_states or self.default_active_states)
        self.terminal_states: tuple[str, ...] = tuple(
            terminal_states or self.default_terminal_states
        )

        self.endpoint = _cfg_str(provider, "endpoint", _DEFAULT_ENDPOINT) or _DEFAULT_ENDPOINT
        if not self.endpoint.lower().startswith(("http://", "https://")):
            raise InvalidTrackerConfig(
                "tracker.provider.endpoint must be an http(s) URL",
                key="endpoint",
                kind=GITHUB_KIND,
            )

        owner = _cfg_str(provider, "owner", None)
        if owner is None:
            raise InvalidTrackerConfig(
                "tracker.provider.owner is required (the organization or user login that "
                "owns the project)",
                key="owner",
                kind=GITHUB_KIND,
            )
        self.owner = owner

        owner_type = (_cfg_str(provider, "owner_type", "organization") or "organization").lower()
        if owner_type not in _OWNER_ROOTS:
            raise InvalidTrackerConfig(
                f"tracker.provider.owner_type must be one of {sorted(_OWNER_ROOTS)}, "
                f"got {owner_type!r}",
                key="owner_type",
                kind=GITHUB_KIND,
            )
        self.owner_type = owner_type
        self._owner_root = _OWNER_ROOTS[owner_type]

        if provider.get("project_number") is None:
            raise InvalidTrackerConfig(
                "tracker.provider.project_number is required (the number in the project URL)",
                key="project_number",
                kind=GITHUB_KIND,
            )
        self.project_number = _cfg_int(provider, "project_number", 0, lo=1, hi=2**31 - 1)

        self.status_field = _cfg_str(provider, "status_field", "Status") or "Status"
        self.priority_field = _cfg_str(provider, "priority_field", "Priority") or "Priority"
        self.branch_field = _cfg_str(provider, "branch_field", None)
        self.priority_map = _cfg_priority_map(provider)

        self.page_size = _cfg_int(provider, "page_size", 50, lo=1, hi=_MAX_PAGE_SIZE)
        self.max_pages = _cfg_int(provider, "max_pages", 20, lo=1, hi=10_000)
        self.timeout_ms = _cfg_int(provider, "timeout_ms", 20_000, lo=1, hi=600_000)

        self.require_assignee = _cfg_bool(provider, "require_assignee", False)
        self.assignee_logins = _cfg_str_list(provider, "assignee_logins")
        self.issue_dependencies = _cfg_bool(provider, "issue_dependencies", False)
        self.user_agent = _cfg_str(provider, "user_agent", _DEFAULT_USER_AGENT)

        # SPEC 15.3: resolve and validate presence without ever logging the value.
        self._token, self._secret_env_names = self._resolve_token(provider)

        self._fragment = _ITEM_FRAGMENT.replace(
            "__BLOCKED_BY__", _BLOCKED_BY_SELECTION if self.issue_dependencies else ""
        )
        self._clock = clock
        self._transport = transport
        self._http = client
        self._owns_http = client is None
        self._status_field_cache: dict[str, Any] | None = None

    # -- configuration / secrets --------------------------------------------

    @staticmethod
    def _resolve_token(provider: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
        """Resolve the API token and the env names the launcher must strip.

        SPEC 5.3.1: a documented secret ``$VAR`` resolving to an empty string is
        *missing*. SPEC 15.3: validate presence without printing the value —
        every error below names only the key or the environment variable.
        """
        token_env = _cfg_str(provider, "token_env", "GITHUB_TOKEN") or "GITHUB_TOKEN"
        raw = provider.get("token")

        if raw is not None:
            if not isinstance(raw, str) or not raw.strip():
                raise InvalidTrackerConfig(
                    "tracker.provider.token must be a non-empty string or a $VAR reference",
                    key="token",
                    kind=GITHUB_KIND,
                )
            match = _VAR_REF.match(raw.strip())
            if match is None:
                # Literal credential: nothing is read from the environment, so
                # there is no env name for the launcher to strip.
                return raw.strip(), ()
            name = match.group(1)
            value = os.environ.get(name, "")
            if not value.strip():
                raise MissingTrackerSecret(
                    f"tracker.provider.token references ${name}, which is unset or empty",
                    key="token",
                    env=name,
                    kind=GITHUB_KIND,
                )
            return value.strip(), (name,)

        value = os.environ.get(token_env, "")
        if not value.strip():
            raise MissingTrackerSecret(
                f"github tracker credential is missing: environment variable {token_env} "
                "is unset or empty",
                key="token_env",
                env=token_env,
                kind=GITHUB_KIND,
            )
        return value.strip(), (token_env,)

    def secret_environment_names(self) -> list[str]:
        """Env names the launcher strips from child environments (SPEC 15.3).

        Only names this adapter actually reads are declared. A literal
        ``provider.token`` declares nothing because nothing is read from the
        environment — but SPEC 15.3 warns against literal credentials in a
        repo-owned ``WORKFLOW.md`` the child can read.
        """
        return list(self._secret_env_names)

    # -- transport -----------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout_ms / 1000.0),
                "headers": {
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                    "User-Agent": self.user_agent or _DEFAULT_USER_AGENT,
                },
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._http = httpx.AsyncClient(**kwargs)
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        """Release the HTTP client. Safe to call more than once (SPEC 11.2)."""
        client, self._http = self._http, None
        if client is not None and self._owns_http:
            await client.aclose()

    def _query(self, body: str) -> str:
        return body.replace("__ROOT__", self._owner_root) + self._fragment

    async def _graphql(
        self, query: str, variables: dict[str, Any], *, where: str
    ) -> dict[str, Any]:
        """POST one GraphQL document and map every failure onto SPEC 11.4."""
        try:
            response = await self._client().post(
                self.endpoint, json={"query": query, "variables": variables}
            )
        except httpx.TimeoutException as exc:
            raise TrackerRequestError(
                f"github graphql request timed out during {where}",
                retryable=True,
                where=where,
                reason="timeout",
                detail=str(exc)[:200],
            ) from exc
        except httpx.HTTPError as exc:
            raise TrackerRequestError(
                f"github graphql transport failure during {where}: {type(exc).__name__}",
                retryable=True,
                where=where,
                reason="transport",
                detail=str(exc)[:200],
            ) from exc

        rate = self._rate_limit_error(response, where=where)
        if rate is not None:
            raise rate

        if response.status_code >= 400:
            raise TrackerStatusError(
                f"github graphql returned HTTP {response.status_code} during {where}",
                retryable=response.status_code >= 500,
                where=where,
                status=response.status_code,
                detail=_body_snippet(response),
            )

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise TrackerResponseError(
                f"github graphql returned a non-JSON body during {where}",
                where=where,
                status=response.status_code,
                detail=_body_snippet(response),
            ) from exc

        if not isinstance(payload, dict):
            raise TrackerResponseError(
                f"github graphql returned a non-object body during {where}", where=where
            )

        errors = payload.get("errors")
        if errors:
            raise self._graphql_error(errors, response, where=where)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise TrackerResponseError(
                f"github graphql response has no 'data' object during {where}", where=where
            )
        return data

    def _rate_limit_error(
        self, response: httpx.Response, *, where: str
    ) -> TrackerRateLimited | None:
        """Detect primary *and* secondary rate limits (SPEC 11.4).

        GitHub signals them differently: a primary limit exhausts
        ``x-ratelimit-remaining`` and reports an epoch in ``x-ratelimit-reset``;
        a secondary limit sends ``retry-after`` (or only a body message) and
        leaves the primary budget intact.
        """
        if response.status_code not in (403, 429):
            return None
        headers = response.headers
        body = _body_snippet(response).lower()

        retry_after = _parse_int(headers.get("retry-after"))
        if retry_after is not None:
            return TrackerRateLimited(
                f"github secondary rate limit during {where}; retry after {retry_after}s",
                retryable=True,
                retry_after_ms=max(0, retry_after) * 1000,
                where=where,
                status=response.status_code,
                limit="secondary",
            )

        if "secondary rate limit" in body or "abuse detection" in body:
            return TrackerRateLimited(
                f"github secondary rate limit during {where}",
                retryable=True,
                retry_after_ms=_DEFAULT_SECONDARY_RETRY_MS,
                where=where,
                status=response.status_code,
                limit="secondary",
            )

        remaining = _parse_int(headers.get("x-ratelimit-remaining"))
        if remaining == 0:
            reset = _parse_int(headers.get("x-ratelimit-reset"))
            wait_ms = max(0, int(reset - self._clock())) * 1000 if reset is not None else None
            return TrackerRateLimited(
                f"github primary rate limit exhausted during {where}",
                retryable=True,
                retry_after_ms=wait_ms,
                where=where,
                status=response.status_code,
                limit="primary",
            )
        return None

    def _graphql_error(
        self, errors: Any, response: httpx.Response, *, where: str
    ) -> TrackerRateLimited | TrackerStatusError | InvalidTrackerConfig | TrackerResponseError:
        """Map a 200-with-``errors`` GraphQL body onto a SPEC 11.4 category."""
        entries = [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
        types = {str(e.get("type", "")).upper() for e in entries}
        message = "; ".join(
            str(e.get("message", "")).strip() for e in entries if str(e.get("message", "")).strip()
        )
        message = message[:400] or "unspecified graphql error"
        lowered = message.lower()

        if "RATE_LIMITED" in types or "rate limit" in lowered or "abuse detection" in lowered:
            retry_after = _parse_int(response.headers.get("retry-after"))
            secondary = "secondary rate limit" in lowered or "abuse detection" in lowered
            return TrackerRateLimited(
                f"github graphql rate limited during {where}: {message}",
                retryable=True,
                retry_after_ms=(
                    max(0, retry_after) * 1000
                    if retry_after is not None
                    else (_DEFAULT_SECONDARY_RETRY_MS if secondary else None)
                ),
                where=where,
                limit="secondary" if secondary else "primary",
            )

        if "NOT_FOUND" in types:
            return InvalidTrackerConfig(
                f"github project {self.owner_type} {self.owner!r} #{self.project_number} was not "
                f"found or is not visible to the configured token: {message}",
                where=where,
                key="project_number",
                kind=GITHUB_KIND,
            )

        if "FORBIDDEN" in types or "INSUFFICIENT_SCOPES" in types:
            return TrackerStatusError(
                f"github graphql rejected the request during {where}: {message}",
                retryable=False,
                where=where,
                status=response.status_code,
            )

        return TrackerResponseError(
            f"github graphql reported errors during {where}: {message}",
            where=where,
            error_types=sorted(t for t in types if t),
        )

    # -- REQUIRED read kernel (SPEC 11.1) ------------------------------------

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        """Page the configured project and return items in ``state_names``.

        SPEC 11.1: an empty ``state_names`` list returns ``[]`` without a
        provider request; individually malformed records are omitted and
        logged, because such a record was never safe to dispatch.

        The GraphQL API exposes no server-side predicate over project field
        values, so provider-side selection is the *project* (plus pagination)
        and the state filter is applied after normalization. States compare
        case-insensitively while the provider's spelling is preserved on the
        Issue (SPEC 11.3).
        """
        if not state_names:
            return []
        wanted = {normalize_state(name) for name in state_names if normalize_state(name)}
        if not wanted:
            return []

        query = self._query(_LIST_QUERY)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_items: set[str] = set()
        issues: list[Issue] = []
        pages = 0

        while True:
            data = await self._graphql(
                query,
                {
                    "owner": self.owner,
                    "number": self.project_number,
                    "first": self.page_size,
                    "after": cursor,
                },
                where="fetch_issues_by_states",
            )
            pages += 1
            connection = self._project_items(data, where="fetch_issues_by_states")
            nodes = connection.get("nodes")
            if nodes is None:
                nodes = []
            if not isinstance(nodes, list):
                raise TrackerResponseError(
                    "github project items 'nodes' is not a list",
                    where="fetch_issues_by_states",
                )

            for node in nodes:
                item_id = node.get("id") if isinstance(node, dict) else None
                if isinstance(item_id, str) and item_id in seen_items:
                    # A concurrent board edit can shift an item across a page
                    # boundary. Keep the first occurrence so order is stable.
                    continue
                try:
                    issue = self._normalize_item(node, where="fetch_issues_by_states")
                except TrackerResponseError as exc:
                    _LOG.warning(
                        "tracker_record_omitted adapter=github where=fetch_issues_by_states "
                        "item_id=%s reason=%s",
                        item_id or "<unknown>",
                        exc.message,
                    )
                    continue
                if isinstance(item_id, str):
                    seen_items.add(item_id)
                if issue.normalized_state in wanted:
                    issues.append(issue)

            page_info = connection.get("pageInfo")
            if not isinstance(page_info, dict):
                raise TrackerResponseError(
                    "github project items response is missing 'pageInfo'",
                    where="fetch_issues_by_states",
                )
            if not page_info.get("hasNextPage"):
                return issues

            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise TrackerPaginationError(
                    "github reported hasNextPage with no endCursor",
                    where="fetch_issues_by_states",
                    reason="missing_cursor",
                    pages=pages,
                )
            if next_cursor in seen_cursors:
                raise TrackerPaginationError(
                    "github pagination cursor repeated; refusing to loop",
                    where="fetch_issues_by_states",
                    reason="cursor_loop",
                    pages=pages,
                )
            if pages >= self.max_pages:
                raise TrackerPaginationError(
                    f"github project exceeded max_pages={self.max_pages} at page_size="
                    f"{self.page_size}; raise tracker.provider.max_pages rather than "
                    "silently truncating the board",
                    where="fetch_issues_by_states",
                    reason="page_limit_exceeded",
                    pages=pages,
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        """Refresh full snapshots for opaque project-item IDs (SPEC 11.1).

        Empty input returns ``[]`` without a provider request. IDs that no
        longer resolve, resolve to something other than a project item, or
        belong to a different project are *omitted* — the orchestrator reads
        omission as "no longer visible". A malformed **requested** record fails
        the whole call instead, because omission is meaningful here.
        """
        unique = list(
            dict.fromkeys(i.strip() for i in issue_ids if isinstance(i, str) and i.strip())
        )
        if not unique:
            return []

        query = self._query(_BY_IDS_QUERY)
        issues: list[Issue] = []
        for start in range(0, len(unique), _MAX_NODE_IDS):
            batch = unique[start : start + _MAX_NODE_IDS]
            data = await self._graphql(
                query, {"ids": batch}, where="fetch_issues_by_ids"
            )
            nodes = data.get("nodes")
            if not isinstance(nodes, list):
                raise TrackerResponseError(
                    "github nodes(ids:) response is not a list", where="fetch_issues_by_ids"
                )
            for node in nodes:
                if node is None:
                    continue
                if not isinstance(node, dict):
                    raise TrackerResponseError(
                        "github nodes(ids:) returned a non-object entry",
                        where="fetch_issues_by_ids",
                    )
                if node.get("__typename") not in (None, "ProjectV2Item"):
                    continue
                if not self._in_scope(node):
                    continue
                issues.append(self._normalize_item(node, where="fetch_issues_by_ids"))
        return issues

    def _project_items(self, data: dict[str, Any], *, where: str) -> dict[str, Any]:
        root = data.get(self._owner_root)
        if not isinstance(root, dict):
            raise InvalidTrackerConfig(
                f"github {self.owner_type} {self.owner!r} was not found or is not visible to "
                "the configured token",
                where=where,
                key="owner",
                kind=GITHUB_KIND,
            )
        project = root.get("projectV2")
        if not isinstance(project, dict):
            raise InvalidTrackerConfig(
                f"github project #{self.project_number} was not found under {self.owner_type} "
                f"{self.owner!r}",
                where=where,
                key="project_number",
                kind=GITHUB_KIND,
            )
        items = project.get("items")
        if not isinstance(items, dict):
            raise TrackerResponseError(
                "github projectV2 response is missing an 'items' connection", where=where
            )
        return items

    def _in_scope(self, node: dict[str, Any]) -> bool:
        """True when a refreshed item still belongs to the configured project."""
        project = node.get("project")
        if not isinstance(project, dict):
            return False
        if project.get("number") != self.project_number:
            return False
        owner = project.get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        if isinstance(login, str) and login.strip():
            return login.strip().casefold() == self.owner.casefold()
        # Older/GHES schemas may omit the owner union; the project number plus
        # the token's visibility is the best available scope check.
        return True

    # -- normalization (SPEC 11.3) -------------------------------------------

    def _normalize_item(self, node: Any, *, where: str) -> Issue:
        if not isinstance(node, dict):
            raise TrackerResponseError(
                "github project item is not an object", where=where, field="item"
            )

        content = node.get("content")
        content = content if isinstance(content, dict) else {}
        content_type = content.get("__typename") or _TYPE_FROM_ITEM.get(str(node.get("type") or ""))
        fields = _field_values(node)

        item_id = node.get("id")
        item_id = item_id.strip() if isinstance(item_id, str) else ""

        repository = content.get("repository")
        repository = repository if isinstance(repository, dict) else {}
        repo_slug = _opt_text(repository.get("nameWithOwner"))
        if repo_slug is None:
            repo_owner = repository.get("owner")
            owner_login = (
                _opt_text(repo_owner.get("login")) if isinstance(repo_owner, dict) else None
            )
            repo_name = _opt_text(repository.get("name"))
            repo_slug = f"{owner_login}/{repo_name}" if owner_login and repo_name else None

        number = content.get("number")
        number = number if isinstance(number, int) and not isinstance(number, bool) else None

        if content_type == "DraftIssue":
            # Draft items have no repository and no number, but they are real
            # board entries with a Status. The item ID keeps the identifier
            # unique within scope (SPEC 4.1.1) even though drafts never
            # dispatch.
            identifier: str | None = f"draft:{item_id}" if item_id else None
        elif repo_slug and number is not None:
            identifier = f"{repo_slug}#{number}"
        else:
            identifier = None

        assignees = _nodes(content.get("assignees"))
        assignee_logins = tuple(
            login
            for login in (_opt_text(a.get("login")) for a in assignees if isinstance(a, dict))
            if login
        )
        assignee_id = next(
            (
                _opt_text(a.get("id"))
                for a in assignees
                if isinstance(a, dict) and _opt_text(a.get("id"))
            ),
            None,
        )

        blockers = coerce_blockers(
            [
                {
                    "id": _opt_text(b.get("id")),
                    "identifier": _blocker_identifier(b),
                    "state": _opt_text(b.get("state")),
                }
                for b in _nodes(content.get("blockedBy"))
                if isinstance(b, dict)
            ]
        )

        status_entry = fields.get(self.status_field.strip().lower())
        record = {
            "id": item_id,
            "identifier": identifier or "",
            "title": _opt_text(content.get("title")) or "",
            "state": _field_text(status_entry) or "",
        }
        issue_id = require_str(record, "id", where=where)
        issue_identifier = require_str(record, "identifier", where=where)
        issue_title = require_str(record, "title", where=where)
        issue_state = require_str(record, "state", where=where)

        content_state = _opt_text(content.get("state"))
        is_archived = bool(node.get("isArchived"))
        reasons = self._dispatch_blockers(
            content_type=content_type,
            content_state=content_state,
            is_archived=is_archived,
            assignee_logins=assignee_logins,
            blockers=blockers,
        )

        native_ref: dict[str, Any] = {
            "provider": GITHUB_KIND,
            "project_item_id": issue_id,
            "project_id": _project_attr(node, "id"),
            "project_number": _project_attr(node, "number"),
            "project_url": _project_attr(node, "url"),
            "project_owner": _project_owner_login(node) or self.owner,
            "owner_type": self.owner_type,
            "content_type": content_type,
            # A DraftIssue node is not a commentable issue subject, so it is
            # kept as content_node_id only — never as issue_node_id.
            "content_node_id": _opt_text(content.get("id")),
            "issue_node_id": (
                _opt_text(content.get("id"))
                if content_type in ("Issue", "PullRequest")
                else None
            ),
            "issue_number": number,
            "issue_state": content_state,
            "issue_state_reason": _opt_text(content.get("stateReason")),
            "repository": repo_slug,
            "repository_node_id": _opt_text(repository.get("id")),
            "status_field": self.status_field,
            "status_option_id": (
                _opt_text(status_entry.get("optionId")) if isinstance(status_entry, dict) else None
            ),
            "is_archived": is_archived,
            "assignee_logins": list(assignee_logins),
            # Informational only. SPEC 11.3 forbids the scheduler from
            # reconstructing eligibility from native_ref; this exists so
            # prompts and provider-native tools can explain themselves.
            "not_dispatchable_reasons": reasons,
        }

        return Issue(
            id=issue_id,
            identifier=issue_identifier,
            title=issue_title,
            state=issue_state,
            dispatchable=not reasons,
            native_ref=native_ref,
            description=_opt_text(content.get("body")),
            priority=self._priority(fields),
            branch_name=self._branch_name(fields),
            url=_opt_text(content.get("url")),
            assignee_id=assignee_id,
            labels=normalize_labels(_nodes(content.get("labels"))),
            blocked_by=blockers,
            created_at=parse_rfc3339(content.get("createdAt") or node.get("createdAt")),
            updated_at=parse_rfc3339(content.get("updatedAt") or node.get("updatedAt")),
        )

    def _dispatch_blockers(
        self,
        *,
        content_type: Any,
        content_state: str | None,
        is_archived: bool,
        assignee_logins: tuple[str, ...],
        blockers: tuple[Any, ...],
    ) -> list[str]:
        """Fold provider routing rules into the explicit boolean (SPEC 11.3).

        Returns the *reasons* the item is not dispatchable; an empty list means
        ``dispatchable=True``. The generic scheduler never reconstructs these.
        """
        reasons: list[str] = []
        if content_type == "DraftIssue":
            reasons.append("draft_item")
        elif content_type == "PullRequest":
            reasons.append("pull_request_item")
        elif content_type != "Issue":
            reasons.append("unsupported_content_type")
        if is_archived:
            reasons.append("archived_on_board")
        if content_type == "Issue" and (content_state or "").upper() != "OPEN":
            reasons.append("issue_not_open")
        if self.require_assignee and not assignee_logins:
            reasons.append("unassigned")
        if self.assignee_logins:
            folded = {login.casefold() for login in assignee_logins}
            if not folded.intersection(self.assignee_logins):
                reasons.append("assignee_not_allowed")
        if any((blocker.state or "").upper() != "CLOSED" for blocker in blockers):
            reasons.append("blocked_by_open_dependency")
        return reasons

    def _priority(self, fields: dict[str, dict[str, Any]]) -> int | None:
        entry = fields.get(self.priority_field.strip().lower())
        if not isinstance(entry, dict):
            return None
        number = entry.get("number")
        if number is not None:
            return coerce_priority(number)
        text = _field_text(entry)
        if text is None:
            return None
        folded = text.strip().lower()
        if folded in self.priority_map:
            return self.priority_map[folded]
        match = _P_NUMBER.match(folded)
        if match:
            return coerce_priority(match.group(1))
        return coerce_priority(folded)

    def _branch_name(self, fields: dict[str, dict[str, Any]]) -> str | None:
        if not self.branch_field:
            return None
        return _field_text(fields.get(self.branch_field.strip().lower()))

    # -- OPTIONAL provider-native agent tools (SPEC 10.5, 11.5) --------------

    def agent_tool_specs(self) -> list[ToolSpec]:
        """Two host-side tools; the child never receives the token (SPEC 15.3)."""
        return [
            ToolSpec(
                name="github_set_project_status",
                description=(
                    "Move the current project item to a different option of the "
                    f"{self.status_field!r} single-select field on the configured GitHub "
                    "project board. Operates only on the issue currently being worked."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "description": (
                                f"Target {self.status_field} option name, matched "
                                "case-insensitively."
                            ),
                        }
                    },
                    "required": ["status"],
                    "additionalProperties": False,
                },
                mutates_tracker=True,
            ),
            ToolSpec(
                name="github_add_issue_comment",
                description=(
                    "Post a comment on the GitHub issue backing the current project item. "
                    "Draft items have no issue and are rejected."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "body": {"type": "string", "description": "Markdown comment body."}
                    },
                    "required": ["body"],
                    "additionalProperties": False,
                },
                mutates_tracker=True,
            ),
        ]

    async def execute_agent_tool(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """Run a provider-native tool host-side, never raising (SPEC 10.5)."""
        try:
            if name == "github_set_project_status":
                return await self._tool_set_status(arguments, context)
            if name == "github_add_issue_comment":
                return await self._tool_add_comment(arguments, context)
        except Exception as exc:  # an unhandled raise would stall the session (SPEC 10.5)
            return ToolResult.failure(
                _tool_error_message(exc), category=getattr(exc, "category", "tool_error")
            )
        return ToolResult.failure(f"unsupported tool: {name}")

    async def _tool_set_status(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        issue = context.issue
        if issue is None or not isinstance(issue.native_ref, dict):
            return ToolResult.failure("no current issue context for github_set_project_status")
        target = arguments.get("status")
        if not isinstance(target, str) or not target.strip():
            return ToolResult.failure("'status' must be a non-empty string")

        field = await self._status_field_metadata()
        options = field.get("options") or []
        chosen = next(
            (
                option
                for option in options
                if isinstance(option, dict)
                and str(option.get("name", "")).strip().casefold() == target.strip().casefold()
            ),
            None,
        )
        if chosen is None:
            return ToolResult.failure(
                f"unknown {self.status_field} option {target!r}",
                available=[o.get("name") for o in options if isinstance(o, dict)],
            )

        project_id = issue.native_ref.get("project_id") or field.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            return ToolResult.failure("project id unavailable for github_set_project_status")

        await self._graphql(
            _SET_STATUS_MUTATION,
            {
                "project": project_id,
                "item": issue.id,
                "field": field.get("id"),
                "option": chosen.get("id"),
            },
            where="github_set_project_status",
        )
        return ToolResult.success(
            {
                "project_item_id": issue.id,
                "field": self.status_field,
                "status": chosen.get("name"),
            }
        )

    async def _tool_add_comment(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        issue = context.issue
        if issue is None or not isinstance(issue.native_ref, dict):
            return ToolResult.failure("no current issue context for github_add_issue_comment")
        body = arguments.get("body")
        if not isinstance(body, str) or not body.strip():
            return ToolResult.failure("'body' must be a non-empty string")

        subject = issue.native_ref.get("issue_node_id")
        if not isinstance(subject, str) or not subject:
            return ToolResult.failure(
                "this project item has no backing GitHub issue to comment on",
                content_type=issue.native_ref.get("content_type"),
            )

        data = await self._graphql(
            _ADD_COMMENT_MUTATION,
            {"subject": subject, "body": body},
            where="github_add_issue_comment",
        )
        edge = ((data.get("addComment") or {}).get("commentEdge") or {}).get("node") or {}
        return ToolResult.success(
            {"issue_node_id": subject, "comment_id": edge.get("id"), "url": edge.get("url")}
        )

    async def _status_field_metadata(self) -> dict[str, Any]:
        if self._status_field_cache is not None:
            return self._status_field_cache
        data = await self._graphql(
            _STATUS_FIELD_QUERY.replace("__ROOT__", self._owner_root),
            {"owner": self.owner, "number": self.project_number, "field": self.status_field},
            where="github_status_field",
        )
        root = data.get(self._owner_root)
        project = root.get("projectV2") if isinstance(root, dict) else None
        field = project.get("field") if isinstance(project, dict) else None
        if not isinstance(field, dict) or not field.get("id"):
            raise InvalidTrackerConfig(
                f"github project field {self.status_field!r} is not a single-select field on "
                f"project #{self.project_number}",
                key="status_field",
                kind=GITHUB_KIND,
            )
        field = dict(field)
        field["project_id"] = project.get("id") if isinstance(project, dict) else None
        self._status_field_cache = field
        return field


# --------------------------------------------------------------------------
# Payload helpers
# --------------------------------------------------------------------------

_TYPE_FROM_ITEM = {
    "ISSUE": "Issue",
    "PULL_REQUEST": "PullRequest",
    "DRAFT_ISSUE": "DraftIssue",
}


def _opt_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _nodes(connection: Any) -> list[Any]:
    if not isinstance(connection, dict):
        return []
    nodes = connection.get("nodes")
    return nodes if isinstance(nodes, list) else []


def _field_values(node: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index a project item's field values by lowercased field name."""
    out: dict[str, dict[str, Any]] = {}
    for entry in _nodes(node.get("fieldValues")):
        if not isinstance(entry, dict):
            continue
        field = entry.get("field")
        name = _opt_text(field.get("name")) if isinstance(field, dict) else None
        if name:
            out.setdefault(name.lower(), entry)
    return out


def _field_text(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    for key in ("name", "text"):
        value = _opt_text(entry.get(key))
        if value:
            return value
    return None


def _blocker_identifier(blocker: dict[str, Any]) -> str | None:
    repository = blocker.get("repository")
    slug = _opt_text(repository.get("nameWithOwner")) if isinstance(repository, dict) else None
    number = blocker.get("number")
    if slug and isinstance(number, int) and not isinstance(number, bool):
        return f"{slug}#{number}"
    return None


def _project_attr(node: dict[str, Any], key: str) -> Any:
    project = node.get("project")
    return project.get(key) if isinstance(project, dict) else None


def _project_owner_login(node: dict[str, Any]) -> str | None:
    owner = _project_attr(node, "owner")
    return _opt_text(owner.get("login")) if isinstance(owner, dict) else None


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _body_snippet(response: httpx.Response, limit: int = 400) -> str:
    """Response bodies are safe to quote; the credential travels in a header."""
    try:
        return response.text[:limit]
    except (UnicodeDecodeError, httpx.ResponseNotRead):  # pragma: no cover - defensive
        return ""


def _tool_error_message(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    return f"{type(exc).__name__}: {exc}"[:400]
