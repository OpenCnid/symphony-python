# Adapter profile: `github` (GitHub Projects v2)

This is the compact adapter profile REQUIRED by **SPEC 11.2**. It documents the
exact `tracker.kind`, the `tracker.provider` schema, scope selection,
pagination, the `id`/`native_ref` mapping, normalization, `dispatchable`
derivation, provider-native tools, and the mapping from this implementation's
language-native error forms to the portable SPEC 11.4 categories.

- Module: `src/symphony/trackers/github.py`
- Class: `symphony.trackers.github.GitHubProjectsAdapter`
- Tests: `tests/test_tracker_github.py`

---

## 1. Supported `tracker.kind`

```
github
```

Registered via `@register_adapter`, so `build_adapter("github", provider)`
returns this adapter and `adapter_kinds()` lists it.

## 2. `tracker.provider` keys

Unknown keys are preserved and ignored (SPEC 5.3.1 forbids core from
prescribing a cross-provider schema). All validation errors below are raised at
**construction time**, so SPEC 6.3 dispatch preflight catches them before the
scheduling loop starts.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `owner` | string | — **REQUIRED** | Organization or user login that owns the project. |
| `project_number` | integer | — **REQUIRED** | The number in the project URL (`/projects/<N>`). |
| `owner_type` | `organization` \| `user` | `organization` | Selects the GraphQL root field. |
| `endpoint` | string | `https://api.github.com/graphql` | GraphQL endpoint; set to `https://<host>/api/graphql` for GHES. |
| `token` | string | unset | Literal credential **or** `$VAR` / `${VAR}` indirection. |
| `token_env` | string | `GITHUB_TOKEN` | Environment variable read when `token` is unset. |
| `status_field` | string | `Status` | Project field carrying board state. |
| `priority_field` | string | `Priority` | Project field carrying priority. |
| `priority_map` | map[string, int] | `{urgent: 1, high: 2, medium: 3, low: 4}` | Merged over the defaults; keys match option names case-insensitively. |
| `branch_field` | string | unset | Project text field holding branch metadata. |
| `page_size` | integer 1..100 | `50` | `first:` on the project `items` connection. |
| `max_pages` | integer >= 1 | `20` | Hard cap on pages per state-list read. |
| `timeout_ms` | integer 1..600000 | `20000` | HTTP timeout per GraphQL request. |
| `require_assignee` | boolean | `false` | When true, unassigned items are not dispatchable. |
| `assignee_logins` | list[string] | `[]` | When non-empty, at least one assignee login must match (case-insensitive). |
| `issue_dependencies` | boolean | `false` | Request GitHub issue dependencies (`blockedBy`) and fold them into `dispatchable`. |
| `user_agent` | string | `symphony-python/0.1 (github-projects-v2)` | `User-Agent` header. |

### Validation errors

| Condition | Error class | `category` |
|---|---|---|
| `provider` is not an object | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `owner` missing/blank | `InvalidTrackerConfig` (`details.key = "owner"`) | `invalid_tracker_config` |
| `project_number` missing, non-integer, or `< 1` | `InvalidTrackerConfig` (`details.key = "project_number"`) | `invalid_tracker_config` |
| `owner_type` not `organization`/`user` | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `endpoint` not `http(s)://` | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `page_size`/`max_pages`/`timeout_ms` out of range or non-integer | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `require_assignee`/`issue_dependencies` not boolean | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `assignee_logins` not a list of non-empty strings | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `priority_map` not a mapping of name to integer | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `token` is `$VAR` and the variable is unset/empty | `MissingTrackerSecret` (`details.env = VAR`) | `missing_tracker_secret` |
| `token` unset and `token_env` unset/empty | `MissingTrackerSecret` (`details.env = token_env`) | `missing_tracker_secret` |
| `status_field` is not a single-select field (tool path only) | `InvalidTrackerConfig` | `invalid_tracker_config` |

### Default states (SPEC 5.3.1)

The workflow MAY omit `active_states`/`terminal_states`; this adapter documents
the option names of GitHub's built-in project `Status` field as defaults.

```yaml
active_states:   ["Todo", "In Progress"]
terminal_states: ["Done"]
```

### Secrets (SPEC 15.3)

- The token is resolved and its **presence** validated at construction. Errors
  name only the config key or the environment variable — never the value.
- The token is sent only as an `Authorization: Bearer` request header. It never
  appears in `native_ref`, in error `message`/`details`, or in logs.
- `secret_environment_names()` declares exactly the names this adapter reads,
  so the launcher strips them from local and remote child environments:
  - `token: "$VAR"` -> `["VAR"]`
  - `token` unset -> `[token_env]` (default `["GITHUB_TOKEN"]`)
  - `token` given literally -> `[]` (nothing is read from the environment)
- Consequence worth stating: with the default configuration `GITHUB_TOKEN` is
  removed from the coding agent's environment, so `gh` inside the workspace is
  unauthenticated. If the child needs its own credential, give the tracker a
  **different** env name via `token_env` and provision the child separately.
- SPEC 15.3 discourages a literal `token` in a repo-owned `WORKFLOW.md`,
  because the child can read that workspace.

## 3. Scope selection, pagination, and request limits

- **Scope** is one `ProjectV2` selected by `owner` + `owner_type` +
  `project_number`. Nothing outside that board is ever returned.
- **State-list read** pages `projectV2.items(first: page_size, after: cursor)`
  and follows `pageInfo.endCursor` until `hasNextPage` is false.
- The GraphQL API exposes **no server-side predicate over project field
  values**, so the state filter is applied after normalization. Provider-side
  selection is the project plus pagination, as SPEC 11.1 requires.
- **Order is preserved** across pages: nodes are appended in page order and
  never re-sorted. An item seen twice (a concurrent board edit can shift an
  item across a page boundary) keeps its first-seen position.
- **ID refresh** batches into `nodes(ids: [...])` at 100 IDs per request
  (GitHub's cap). Input IDs are trimmed, de-duplicated, and treated as a set;
  each dispatch ID appears at most once in the result.
- **Request limits**: `page_size <= 100`, `max_pages` pages per state-list read
  (default 20 -> up to 1000 items at the default `page_size`). Exceeding
  `max_pages` fails with `tracker_pagination` rather than silently truncating
  the board, because a truncated read looks to the orchestrator like issues
  that vanished.
- No internal retry. One request per page; the orchestrator owns retry cadence.

## 4. `id` and `native_ref` mapping

A project *item* and the *issue* it wraps have different node IDs. The **item**
is the dispatch identity because it is what carries board state.

| Normalized field | Source |
|---|---|
| `id` | `ProjectV2Item.id` (e.g. `PVTI_...`) — stable dispatch identity |
| `identifier` | `<owner>/<repo>#<number>` for issue- and PR-backed items; `draft:<item id>` for draft items |

`native_ref` is a JSON-safe, non-secret object preserving the distinct
underlying IDs (SPEC 11.2, 17.3):

| Key | Value |
|---|---|
| `provider` | `"github"` |
| `project_item_id` | `ProjectV2Item.id` (same as `Issue.id`) |
| `project_id`, `project_number`, `project_url`, `project_owner`, `owner_type` | Board coordinates |
| `content_type` | `Issue`, `PullRequest`, or `DraftIssue` |
| `content_node_id` | Node ID of the item's content, whatever its type |
| `issue_node_id` | Node ID **only** for `Issue`/`PullRequest` content; `null` for drafts, since a `DraftIssue` is not a commentable subject |
| `issue_number` | Issue/PR number, or `null` |
| `issue_state`, `issue_state_reason` | The issue's own `OPEN`/`CLOSED` state — distinct from board state |
| `repository`, `repository_node_id` | `owner/name` slug and repository node ID |
| `status_field`, `status_option_id` | Which field carries board state and which option is selected |
| `is_archived` | Board archive flag |
| `assignee_logins` | All assignee logins (non-secret) |
| `not_dispatchable_reasons` | **Informational only.** Why `dispatchable` is false. SPEC 11.3 forbids the scheduler from reconstructing eligibility from `native_ref`; this exists for prompt/tool context and operators. |

## 5. Normalization (SPEC 11.3)

| Field | Rule |
|---|---|
| `state` | The `status_field` single-select (or text) value, **provider spelling preserved**. Scheduler comparison is trimmed + lowercased. Board state, not the issue's `OPEN`/`CLOSED` state. |
| `title` | Issue/PR/draft title. REQUIRED. |
| `description` | Issue/PR/draft body, or `null`. |
| `labels` | Trimmed, lowercased, blanks dropped, duplicates removed, order preserved (`normalize_labels`). |
| `priority` | `priority_field` value -> integer or `null`. Number fields are used directly. Single-select option names match `priority_map` (default `urgent/high/medium/low` -> `1..4`), then the `P<n>` pattern (`P1`, `p3`), then a bare integer. Anything else -> `null`. |
| `assignee_id` | Node ID of the first assignee, or `null`. All logins live in `native_ref.assignee_logins`. |
| `branch_name` | `null` unless `branch_field` names a project text field. GitHub does not otherwise expose reliable branch metadata for an issue. |
| `url` | Issue/PR HTML URL; `null` for drafts. |
| `created_at` / `updated_at` | `parse_rfc3339` of the content's timestamps, falling back to the item's. Unparseable -> `null`. |
| `blocked_by` | `[]` unless `issue_dependencies` is enabled. Each entry is `{id, identifier: "<owner>/<repo>#<n>", state}` from GitHub issue dependencies, via `coerce_blockers`. No blocker semantics are invented from task lists, sub-issues, or free text. |
| `native_ref` | Always a JSON-safe non-secret object (never `null` for a valid record). |

### Malformed records

A record is malformed only when `id`, `identifier`, `title`, or `state` cannot
be produced (`require_str`). In practice that means: no item ID, no
`status_field` value (an item with `Status` cleared), a blank title, or an
issue-backed item with no repository/number.

- **State-list read** omits the record and logs a warning to the
  `symphony.trackers.github` logger:
  `tracker_record_omitted adapter=github where=... item_id=... reason=...`.
  It was never safe to dispatch (SPEC 11.1).
- **ID refresh** raises `TrackerResponseError` instead, because omission is
  meaningful there.

Unusable nullable/best-effort values (a bad timestamp, an unknown priority
option, a malformed blocker entry) normalize to `null`/empty **without**
hiding valid required fields.

### Invisible vs. malformed on ID refresh

Omitted (treated as "no longer visible"): the node resolves to `null`; the node
is not a `ProjectV2Item`; the item's project number or owner login does not
match the configured scope. Everything else that is returned must normalize, or
the whole call fails.

## 6. `dispatchable` derivation

`dispatchable` is `true` only when **every** check passes. The reason strings
below are also mirrored into `native_ref.not_dispatchable_reasons`.

| Reason | Rule |
|---|---|
| `draft_item` | `content_type == "DraftIssue"` — a draft has no repository, no number, and nothing to work on. |
| `pull_request_item` | `content_type == "PullRequest"` — PRs on the board are tracking artifacts, not work to dispatch. |
| `unsupported_content_type` | Any other content type. |
| `archived_on_board` | `ProjectV2Item.isArchived` is true. |
| `issue_not_open` | The underlying issue's own state is not `OPEN`. Board state alone is not enough: a closed issue can sit in an active column. |
| `unassigned` | `require_assignee` is true and the issue has no assignee. |
| `assignee_not_allowed` | `assignee_logins` is non-empty and no assignee login matches (case-insensitive). |
| `blocked_by_open_dependency` | `issue_dependencies` is enabled and some `blockedBy` issue is not `CLOSED`. |

The orchestrator still applies `required_labels`, configured states, claims,
retries, and concurrency on top (SPEC 4.1.1). It never reconstructs the rules
above.

**Note on candidate polling:** items failing these checks are still *returned*
by `fetch_issues_by_states` when their board state matches, with
`dispatchable=false`. The scheduler owns that final filter (SPEC 11.1).

## 7. Provider-native agent tools (SPEC 10.5, 11.5)

Both tools execute **host-side** with the adapter's credential. The child
process never receives a token. Unsupported names and every failure return a
structured `ToolResult` — nothing raises, so the session cannot stall.

### `github_set_project_status` (mutates tracker)

```json
{"type": "object",
 "properties": {"status": {"type": "string"}},
 "required": ["status"], "additionalProperties": false}
```

- **Scope:** only the project item in the current `ToolContext.issue`, only the
  configured project, only the configured `status_field`.
- **Behavior:** resolves the field and its options once per adapter instance
  (cached), matches `status` case-insensitively, then calls
  `updateProjectV2ItemFieldValue`.
- **Success:** `{project_item_id, field, status}`.
- **Failure:** no issue context; blank `status`; unknown option (the failure
  content carries `available`, the valid option names); any GraphQL/transport
  error, surfaced as `{error: <message>, content: {category: <SPEC 11.4 slug>}}`.
- This is the tool that reaches a workflow handoff state such as
  `Human Review` (SPEC 11.5).

### `github_add_issue_comment` (mutates tracker)

```json
{"type": "object",
 "properties": {"body": {"type": "string"}},
 "required": ["body"], "additionalProperties": false}
```

- **Scope:** the issue backing the current project item (`issue_node_id`).
- **Success:** `{issue_node_id, comment_id, url}`.
- **Failure:** no issue context; blank `body`; a draft item (no commentable
  subject — the failure content carries `content_type`); any GraphQL/transport
  error.

### Idempotency and rate limits

Neither tool is idempotent: re-running `github_add_issue_comment` posts another
comment. `github_set_project_status` is effectively idempotent, since setting
the option already selected is a no-op on the board. Both consume the shared
GitHub rate-limit budget described below; mutations also count against
GitHub's secondary (burst/content-creation) limits, so an agent that comments
in a tight loop will see `tracker_rate_limited`.

## 8. Error mapping (SPEC 11.4)

Every public error form is an exception subclassing
`symphony.errors.TrackerError`, carrying `.category`, `.message`, `.details`,
and optional `.retryable` / `.retry_after_ms`. `to_dict()` renders the portable
`{category, message, details}` object. The orchestrator relies only on
success vs. failure.

| GitHub failure shape | Exception | `category` | Notes |
|---|---|---|---|
| Connect/DNS/TLS/read error (`httpx.HTTPError`) | `TrackerRequestError` | `tracker_request` | `retryable=True`, `details.reason="transport"` |
| Request timeout (`httpx.TimeoutException`) | `TrackerRequestError` | `tracker_request` | `retryable=True`, `details.reason="timeout"` |
| HTTP 401 (bad credentials) | `TrackerStatusError` | `tracker_status` | `retryable=False`, `details.status=401` |
| HTTP 403 with no rate-limit signal | `TrackerStatusError` | `tracker_status` | `retryable=False` |
| HTTP 4xx (other) | `TrackerStatusError` | `tracker_status` | `retryable=False` |
| HTTP 5xx | `TrackerStatusError` | `tracker_status` | `retryable=True` |
| HTTP 403/429 with `x-ratelimit-remaining: 0` | `TrackerRateLimited` | `tracker_rate_limited` | **Primary** limit. `details.limit="primary"`; `retry_after_ms` from `x-ratelimit-reset` (epoch) minus the injected clock. |
| HTTP 403/429 with a `retry-after` header | `TrackerRateLimited` | `tracker_rate_limited` | **Secondary** limit. `details.limit="secondary"`; `retry_after_ms` from the header. |
| HTTP 403/429 whose body says `secondary rate limit` / `abuse detection` | `TrackerRateLimited` | `tracker_rate_limited` | **Secondary** limit with no header; `retry_after_ms` defaults to 60000. |
| HTTP 200 with `errors[].type == "RATE_LIMITED"`, or a rate-limit message | `TrackerRateLimited` | `tracker_rate_limited` | GraphQL signals rate limiting on a 200. |
| HTTP 200 with `errors[].type == "NOT_FOUND"` | `InvalidTrackerConfig` | `invalid_tracker_config` | The configured project is missing *or* invisible to the token; both are operator-actionable config problems. |
| `data.<root>` or `data.<root>.projectV2` is `null` | `InvalidTrackerConfig` | `invalid_tracker_config` | `details.key` is `owner` or `project_number`. |
| HTTP 200 with `errors[].type` `FORBIDDEN` / `INSUFFICIENT_SCOPES` | `TrackerStatusError` | `tracker_status` | |
| HTTP 200 with any other `errors` | `TrackerResponseError` | `tracker_response` | `details.error_types` lists the GraphQL types. |
| Non-JSON or non-object body | `TrackerResponseError` | `tracker_response` | |
| Missing `data`, missing `items`/`pageInfo`, non-list `nodes` | `TrackerResponseError` | `tracker_response` | |
| Required field missing on a **requested** record (ID refresh) | `TrackerResponseError` | `tracker_response` | `details.field` names the field. |
| `hasNextPage` true with no `endCursor` | `TrackerPaginationError` | `tracker_pagination` | `details.reason="missing_cursor"` |
| A cursor repeats | `TrackerPaginationError` | `tracker_pagination` | `details.reason="cursor_loop"` — refuses to loop |
| More than `max_pages` pages | `TrackerPaginationError` | `tracker_pagination` | `details.reason="page_limit_exceeded"` |

Response bodies are quoted (truncated to 400 characters) in `details.detail`;
the credential travels only in a request header, never in a body or URL.

Per SPEC 11.4 the orchestrator's own behavior is unchanged by category:
candidate-fetch failure skips dispatch for the tick, running-state refresh
failure keeps workers running, and startup terminal cleanup failure logs a
warning and continues.

## 9. Example workflow front matter

```yaml
tracker:
  kind: github
  provider:
    owner: octo-org
    project_number: 7
    token: $SYMPHONY_GITHUB_TOKEN
    status_field: Status
    require_assignee: true
  required_labels: ["agent-ready"]
  active_states: ["Todo", "In Progress"]
  terminal_states: ["Done", "Human Review"]
```

## 10. Known gaps

- `issue_dependencies` defaults to **false**. GitHub's `blockedBy` connection
  is not present on every GraphQL schema version (notably older GHES), and an
  unknown field fails the entire query rather than degrading. Enable it only
  against a schema that has it. With it disabled, `blocked_by` is always `[]`
  and blockers do not affect `dispatchable`.
- Sub-issue / task-list relationships are deliberately **not** mapped to
  `blocked_by`: "tracked by" is not "blocked by", and SPEC 11.3 forbids
  inventing blocker semantics that cannot be represented reliably.
- The `status_field` option list is cached for the adapter's lifetime. A board
  that gains a new `Status` option mid-run will not see it until the adapter is
  rebuilt (which a workflow reload does).
- `fetch_issues_by_states` reads the whole board and filters client-side. On
  very large boards raise `page_size` before raising `max_pages`.
