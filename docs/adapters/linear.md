# Adapter profile: `linear`

The compact adapter profile required by **SPEC 11.2**, for the Linear adapter
implemented in `src/symphony/trackers/linear.py`.

Linear's work object maps onto the normalized Issue of SPEC 4.1.1 more directly
than most providers — it already has a stable UUID, a human ticket key, a
provider-native workflow state name, lowercase-able labels, and RFC 3339
timestamps. What is worth reading closely is the handful of places where the two
models genuinely disagree: the **priority scale** (§7), the difference between
workflow state *names* and state *types* (§6), and **blocker direction** (§9).

---

## 1. Supported `tracker.kind`

```yaml
tracker:
  kind: linear
```

Exactly `linear`. Registered via `@register_adapter`, so
`symphony.trackers.base.build_adapter("linear", provider, **kwargs)` returns a
`LinearAdapter`.

> Registration happens on import of `symphony.trackers.linear`. Anything
> resolving `tracker.kind` must import the module (directly or through a package
> import that pulls it in) before calling `build_adapter`.

### Default states (SPEC 5.3.1)

`tracker.active_states` and `tracker.terminal_states` MAY be omitted; this
profile documents the defaults, which match Linear's out-of-the-box team
workflow:

| Setting | Default |
|---|---|
| `active_states` | `["Todo", "In Progress"]` |
| `terminal_states` | `["Done", "Canceled", "Duplicate"]` |

The effective values are passed to the constructor as the `active_states` /
`terminal_states` keyword arguments and exposed as attributes of the same name.
They are **workflow state names**, not `WorkflowState.type` values (§6).

---

## 2. `tracker.provider` keys

Every key is OPTIONAL except that **one of `team_key` or `team_id` is REQUIRED**.
Unknown keys are preserved and ignored (SPEC 5.3.1 requires core Symphony to
preserve them; this adapter does not reject them).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `api_key` | string \| `"$VAR_NAME"` \| omitted | read from env | The Linear credential (§2.1). |
| `api_key_env` | string | `LINEAR_API_KEY` | Env var consulted when `api_key` is omitted. |
| `auth_scheme` | `"raw"` \| `"bearer"` | `"raw"` | `Authorization: <key>` vs `Authorization: Bearer <key>`. |
| `endpoint` | string | `https://api.linear.app/graphql` | GraphQL endpoint override. |
| `team_key` | string | — | Linear team key, e.g. `ENG`. |
| `team_id` | string | — | Linear team UUID; takes precedence over `team_key`. |
| `project_id` | string \| null | `null` | Narrow scope to one project. |
| `assignee_id` | string \| null | `null` | Narrow scope to one assignee. |
| `page_size` | int in `[1, 250]` | `50` | Connection page size (`first`). |
| `max_pages` | int in `[1, 100000]` | `100` | Pagination safety cap. |
| `timeout_ms` | int in `[1, 600000]` | `30000` | HTTP request timeout. |
| `require_assignee` | bool | `false` | Unassigned issues become `dispatchable=false` (§9). |
| `block_on_open_blockers` | bool | `true` | Open blockers make `dispatchable=false` (§9). |
| `non_dispatchable_state_types` | list of strings | `["canceled", "triage"]` | State types vetoed regardless of `active_states` (§9). |

`auth_scheme` is explicit rather than sniffed from the key format: Linear
accepts a bare personal API key, while OAuth access tokens require `Bearer`.
Guessing from a prefix would fail silently against a rotated key format.

### 2.1 Secret keys, environment names, and validation

The credential is resolved **once at construction**, so SPEC 6.3 dispatch
preflight fails fast rather than at the first poll. A workflow reload builds a
new adapter, which is also what binds tool specs and tracker settings to a
single session snapshot (SPEC 10.5).

Resolution order:

1. `api_key: "$VAR_NAME"` → read `VAR_NAME` from the host environment.
2. `api_key: "<literal>"` → used as-is.
3. `api_key` omitted or blank → read `api_key_env`, defaulting to `LINEAR_API_KEY`.

Per SPEC 5.3.1, a documented secret `$VAR_NAME` that resolves to an empty (or
whitespace-only) string is **missing**, not empty.

`secret_environment_names()` always includes `LINEAR_API_KEY`, plus the
`$VAR_NAME` or `api_key_env` in use. `LINEAR_API_KEY` is declared even when the
credential came from a literal: if that variable is set in the host
environment, the coding-agent child still must not inherit it (SPEC 15.3).

The credential is never logged, never placed in `native_ref`, never present in
any `TrackerError.details`, and excluded from `repr(adapter)` — which matters
because the adapter is reachable from the RLM surface.

> Do not put a literal Linear key in a repo-owned `WORKFLOW.md`. The coding
> agent has workspace read access, so `$VAR_NAME` indirection is the only form
> that preserves the SPEC 15.3 isolation.

### 2.2 Validation errors

| Condition | Error class | `category` |
|---|---|---|
| Neither `team_key` nor `team_id` | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `api_key` is neither a string nor omitted | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `auth_scheme` not in `{raw, bearer}` | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `page_size` / `max_pages` / `timeout_ms` non-integer or out of range | `InvalidTrackerConfig` | `invalid_tracker_config` |
| Credential absent or resolves to `""` | `MissingTrackerSecret` | `missing_tracker_secret` |
| `tracker.kind` not registered | `UnsupportedTrackerKind` | `unsupported_tracker_kind` |

---

## 3. Scope selection

Scope is **one Linear team**, optionally narrowed. The filter is applied
provider-side on every request:

```json
{
  "team": {"key": {"eq": "ENG"}},
  "project": {"id": {"eq": "..."}},
  "assignee": {"id": {"eq": "..."}}
}
```

`team_id` produces `{"team": {"id": {"eq": ...}}}` and wins over `team_key`.

`fetch_issues_by_states` adds a case-insensitive `or` group over workflow state
names:

```json
{"or": [{"state": {"name": {"eqIgnoreCase": "Todo"}}},
        {"state": {"name": {"eqIgnoreCase": "In Progress"}}}]}
```

`eqIgnoreCase` is used rather than `in` because SPEC 5.3.1 compares
provider-native state names case-insensitively and Linear's `in` comparator is
case-sensitive. Requested names are trimmed, blank-filtered, and de-duplicated
case-insensitively before the filter is built.

`fetch_issues_by_ids` adds `{"id": {"in": [...]}}` to the same scope filter.
Input IDs are trimmed, blank-filtered, and de-duplicated, so they are treated
as a set (SPEC 11.1).

**Identifier uniqueness.** Linear identifiers embed the team key (`ENG-123`)
and team keys are unique per workspace, so `identifier` is already unique within
any scope one API key can reach. No extra disambiguation is applied
(SPEC 4.1.1).

**Archived issues** are excluded by Linear's connection default. For
`fetch_issues_by_ids` this means an archived issue is simply absent, which the
orchestrator correctly reads as "no longer visible". `archivedAt` is still
normalized and forces `dispatchable=false` as a defensive second gate.

---

## 4. Pagination and provider request limits

Cursor pagination over the `issues` connection, ordered `orderBy: createdAt`.
Pages are concatenated in arrival order, so **order is preserved across pages**.

| Limit | Value | Note |
|---|---|---|
| `first` per page | `page_size`, default `50`, max `250` | Linear rejects `first > 250`. |
| Pages per call | `max_pages`, default `100` | Safety cap, not a Linear limit. |
| Labels per issue | `50` | Nested connection, **not paginated**. |
| Blocker relations per issue | `25` | Nested connection, **not paginated**. |

The two nested caps are the real request limits worth knowing: an issue carrying
more than 50 labels has the surplus dropped, which could in principle make a
`required_labels` check fail for an issue that does satisfy it. Linear's own
per-issue label counts are far below this in practice. Lower `page_size` if you
hit Linear's query-complexity ceiling — complexity scales with
`page_size × nested node counts`.

Pagination integrity failures raise `TrackerPaginationError`
(`tracker_pagination`) rather than returning a silently truncated result:

- `hasNextPage: true` with a missing or blank `endCursor`;
- the same cursor returned twice (cursor loop);
- more than `max_pages` pages requested.

---

## 5. `id` and `native_ref` mapping

| Normalized field | Linear source |
|---|---|
| `id` | `issue.id` — the Linear issue UUID |
| `identifier` | `issue.identifier`, e.g. `ENG-123` |

Linear's dispatch identity and underlying ticket ID are the same value, so
unlike a board/project-item provider there is nothing to reconcile. `native_ref`
carries the *other* provider identifiers that tools and prompts need, and is
always a JSON-safe object with a stable key set (missing values are `null`
rather than absent, so tool code can index it without guarding):

```json
{
  "issue_id":       "8f1e-…",
  "number":         123,
  "team_id":        "team-uuid",
  "team_key":       "ENG",
  "state_id":       "state-uuid",
  "state_name":     "In Progress",
  "state_type":     "started",
  "priority_raw":   0,
  "priority_label": "No priority",
  "project_id":     "project-uuid",
  "project_name":   "Core"
}
```

`priority_raw` is what makes the priority mapping in §7 lossless.
`team_id` is what lets the state-transition tool resolve workflow states without
an extra lookup. No value in `native_ref` is secret, so it is safe in prompt and
tool context (SPEC 11.3).

---

## 6. State normalization

`state` is `issue.state.name`, preserved with Linear's exact spelling
(`"In Progress"`). Only scheduler comparisons fold case, via
`Issue.normalized_state` (SPEC 4.2, 11.3).

`issue.state.type` — one of `triage`, `backlog`, `unstarted`, `started`,
`completed`, `canceled` — is **not** what `active_states` / `terminal_states`
compare against. It is used for exactly two things:

1. deriving `dispatchable` (§9);
2. deciding whether a *blocking* issue is still open (§9).

It is preserved as `native_ref.state_type` for prompt and tool context.

---

## 7. Priority mapping (documented divergence)

Linear's priority scale is closed at `0..4`:

| Linear | Meaning | Normalized `priority` |
|---|---|---|
| `1` | Urgent | `1` |
| `2` | High | `2` |
| `3` | Medium | `3` |
| `4` | Low | `4` |
| `0` | **No priority** | **`null`** |
| anything else / non-integer / absent | — | `null` |

`1..4` already agree with SPEC 4.1.1 ("lower numbers are higher priority") and
pass through untouched.

**`0` normalizes to `null`.** SPEC 11.3 says the scheduler ranks `1..4` ahead of
null/unknown and sorts other integers with null "unless an implementation
documents a different mapping" — this is that documented mapping, and the
reasoning is:

- Linear's `0` means *the absence of a priority*, which is semantically the null
  bucket, not a rank.
- Emitting a literal `0` makes correct ordering depend on every consumer
  bucketing "other integers" exactly as SPEC 8.2 describes. Any reader applying
  SPEC 4.1.1 directly — a prompt template, the JSON API, an operator, a model —
  reads `0` as *more urgent than Urgent*. That is precisely backwards.
- The mapping is lossless: `native_ref.priority_raw` keeps the original integer
  (including `0`) and `native_ref.priority_label` keeps Linear's own label
  (`"No priority"`, `"Urgent"`, …).

Net effect on SPEC 8.2 sorting: unprioritized Linear issues sort after every
prioritized one, then by `created_at` oldest-first, then by `identifier` — which
is the intended behavior, reached explicitly rather than by accident.

Values outside `0..4` are unusable provider metadata for a nullable field and
normalize to `null` (SPEC 11.3), with the raw value still in `priority_raw`.
`true`/`false` are rejected as priorities.

---

## 8. Labels, timestamps, and optional-field normalization

| Field | Source | Rule |
|---|---|---|
| `labels` | `labels.nodes[].name` | Trimmed, lowercased, blanks dropped, duplicates removed, order preserved. |
| `created_at` / `updated_at` | `createdAt` / `updatedAt` | Parsed RFC 3339 (`Z` accepted); unparseable → `null`. |
| `description` | `description` | Blank or whitespace-only → `null`. |
| `branch_name` | `branchName` | Blank → `null`. |
| `url` | `url` | Blank or non-string → `null`. |
| `assignee_id` | `assignee.id` | Missing assignee → `null`. |
| `blocked_by` | `inverseRelations` | §9. |

Every nullable field degrades to `null` and every collection field to `[]`; a
`null` `project`, a broken `labels` object, or a garbage timestamp does **not**
make a record malformed (SPEC 11.1, 11.3).

### Malformed records

A record is malformed only when `id`, `identifier`, `title`, or the workflow
state *name* is missing or blank. Then:

- **`fetch_issues_by_states`** omits the record and logs it once per page set
  (`tracker.linear.omitted_malformed_records` with `where`, `omitted`, and
  `first_reason`). The per-call `NormalizationReport` is also retained on
  `adapter.last_normalization_report` so a REPL or an RLM can inspect omissions
  without scraping logs.
- **`fetch_issues_by_ids`** raises `TrackerResponseError`. Omission is
  meaningful for a refresh — it means "no longer visible" — so silently dropping
  a malformed *requested* record would be a lie (SPEC 11.1).

---

## 9. `dispatchable` derivation

`dispatchable` is explicit and `true` only when **all** hold:

1. `archivedAt` is null;
2. `state.type` is not in `non_dispatchable_state_types` (default
   `canceled`, `triage`);
3. no **open blocker**, unless `block_on_open_blockers: false`;
4. an assignee exists, if `require_assignee: true`.

**Blocker direction.** Linear models "A blocks B" as an `IssueRelation` with
`type: "blocks"` whose `issue` is A. From B's side that relation appears in
`inverseRelations`, so every inverse `blocks` relation names an issue blocking
this one. That is the only relation shape interpreted as a blocker; `related`,
`duplicate`, and `similar` are ignored, and no blocker semantics are invented
beyond it (SPEC 11.3). A blocker counts as **open** unless its state type is
`completed` or `canceled` — an unknown or missing blocker state type counts as
open, because refusing to dispatch is the safe direction when blocker status
cannot be established.

**Why `completed` is not vetoed.** `NON_DISPATCHABLE_STATE_TYPES` deliberately
omits `completed`. SPEC 11.5 notes that workflow success is often "reached the
next handoff state" rather than tracker-terminal `Done`, and Linear teams do
configure completed-type states as handoff states. Silently refusing to dispatch
a state the operator explicitly listed in `active_states` would be worse than
honoring the configuration; `terminal_states` remains the operator's lever.

Issues with `dispatchable=false` are still **returned** by
`fetch_issues_by_states` — the scheduler owns that filter (SPEC 11.1).

---

## 10. Provider-native agent tools (SPEC 10.5, 11.5)

Four tools are advertised. They cover exactly the ticket mutations SPEC 11.5
assigns to the coding agent rather than to the orchestrator, plus one read tool
so the agent can discover state names instead of guessing.

| Tool | Mutates tracker | Input schema (required in bold) |
|---|---|---|
| `linear_set_issue_state` | **yes** | **`state_name`**: string; `issue_id`: string |
| `linear_add_comment` | **yes** | **`body`**: string (Markdown); `issue_id`: string |
| `linear_attach_link` | **yes** | **`url`**: string; `title`: string; `issue_id`: string |
| `linear_list_workflow_states` | no | *(none)* |

All schemas are `type: object` with `additionalProperties: false`.

### Execution and authorization boundary

- Tools execute **host-side in the Symphony process** with the configured
  adapter credential. The coding-agent child never reads a Linear token from
  disk or environment (SPEC 10.5, 15.3).
- `ToolContext` carries the current normalized issue and **never the
  credential**.
- The target issue is chosen by the adapter, not the model: it defaults to
  `native_ref.issue_id` (falling back to `issue.id`) for the issue this run was
  dispatched for. An explicit `issue_id` argument is accepted **only if it
  matches**; anything else returns a failure naming the allowed identifier. The
  credential's own reach is the whole Linear workspace, so this pin is the
  authorization boundary.
- `linear_list_workflow_states` is the one tool that works without an issue in
  context; it resolves the team from `native_ref.team_id`, then `team_id`, then
  a one-time lookup by `team_key` (cached for the adapter's lifetime).
- Tool specs are read from the adapter instance bound to the session, so a
  workflow reload applies to future sessions only (SPEC 10.5).

### Result and error semantics

`execute_agent_tool` **never raises.** Every path returns a `ToolResult`:

| Situation | Result |
|---|---|
| Unsupported tool name | `failure("unsupported tool: …")`, `content.supported` lists the four names |
| Non-object `arguments` | `failure("tool arguments must be a JSON object")` |
| Missing/blank required argument | `failure` naming the argument |
| `issue_id` not the dispatched issue | `failure`, `content.allowed` = issue identifier |
| No issue in context (mutating tools) | `failure("no issue in tool context")` |
| Unknown `state_name` | `failure`, `content.available` lists the team's state names |
| Mutation returned `success: false` | `failure("linear rejected …")` |
| Any `TrackerError` (§11) | `failure(message)`, `content.category` = the SPEC 11.4 category |
| Any other exception | `failure("<ExceptionType>: <message>")`, `content.category = "tracker_error"` |

Success payloads are flat JSON objects: `linear_set_issue_state` returns
`{issue_id, identifier, state, state_type, updated_at}`; `linear_add_comment`
returns `{comment_id, url, issue_id}`; `linear_attach_link` returns
`{attachment_id, url, title, issue_id}`; `linear_list_workflow_states` returns
`{states: [{id, name, type}]}`.

### Idempotency and rate-limit expectations

- `linear_attach_link` uses Linear's `attachmentLinkURL`, which is idempotent on
  `(issueId, url)` — re-attaching the same PR URL updates the existing
  attachment rather than creating a duplicate. Agents may safely retry it.
- `linear_set_issue_state` is idempotent in effect: setting the state an issue
  already has succeeds and returns the same state.
- `linear_add_comment` is **not** idempotent. A retried call posts a second
  comment; the agent is responsible for not retrying blindly.
- Tool calls share the same Linear rate-limit budget as polling, and count
  against Linear's per-hour request and complexity limits. A rate-limited tool
  call surfaces as `content.category == "tracker_rate_limited"` rather than
  stalling the session.

---

## 11. Error mapping (SPEC 11.4)

Public error forms are Python exceptions from `symphony.errors`, every one
carrying `.category`, `.message`, `.details`, and `.to_dict()`, plus the optional
`.retryable` / `.retry_after_ms` enrichments. The orchestrator relies only on
success versus failure; the categories are for operators, logs, and the RLM
surface.

| Linear / transport failure shape | Exception | `category` | `retryable` |
|---|---|---|---|
| `httpx.TimeoutException` | `TrackerRequestError` | `tracker_request` | `true` |
| `httpx.HTTPError` (connect, DNS, TLS, read) | `TrackerRequestError` | `tracker_request` | `true` |
| HTTP `429` | `TrackerRateLimited` | `tracker_rate_limited` | `true` |
| HTTP `401` / `403` | `TrackerStatusError` | `tracker_status` | `false` |
| Any other HTTP `>= 400` | `TrackerStatusError` | `tracker_status` | `true` iff `>= 500` |
| GraphQL code `RATELIMITED`, `RATE_LIMITED`, `USAGE_LIMIT_EXCEEDED` | `TrackerRateLimited` | `tracker_rate_limited` | `true` |
| GraphQL code `AUTHENTICATION_ERROR`, `UNAUTHENTICATED`, `FORBIDDEN`, `FEATURE_NOT_ACCESSIBLE`, `USER_ERROR` | `TrackerStatusError` | `tracker_status` | `false` |
| GraphQL code `INTERNAL_SERVER_ERROR`, `NETWORK_ERROR`, `SHUTDOWN`, `TIMEOUT` | `TrackerRequestError` | `tracker_request` | `true` |
| Any other GraphQL error (`INVALID_INPUT`, validation, unknown) | `TrackerResponseError` | `tracker_response` | `false` |
| Non-JSON body, missing `data`, missing `issues` connection, malformed required record | `TrackerResponseError` | `tracker_response` | — |
| Cursor loop, `hasNextPage` without cursor, `max_pages` exceeded | `TrackerPaginationError` | `tracker_pagination` | — |
| Missing/empty credential at construction | `MissingTrackerSecret` | `missing_tracker_secret` | — |
| Bad provider config at construction | `InvalidTrackerConfig` | `invalid_tracker_config` | — |

Because Linear returns HTTP `200` with a populated `errors` array for many
failures — and HTTP `400` with one for others — the GraphQL `errors` array is
classified **first**, and the HTTP status is the fallback. A rejected API key
maps to `tracker_status`, not `missing_tracker_secret`: the secret was present,
the provider refused it.

Messages are human-readable and prefixed with `linear ` (for example
`linear rate limited: …`, `linear returned HTTP 503`,
`linear pagination exceeded max_pages=100`). `details` carries only non-secret
context — `kind`, `endpoint`, `status`, `graphql_code`, `where`, `field`.

`retry_after_ms` on `TrackerRateLimited` is derived best-effort from
`Retry-After` (seconds) or `X-RateLimit-Requests-Reset` (unix epoch; Linear
emits milliseconds), and is `null` when neither header is usable. Header parsing
never raises.

Orchestrator behavior on these errors is SPEC 11.4's: candidate-fetch failure
skips dispatch for the tick, running-state refresh failure keeps workers alive,
and startup terminal cleanup failure logs and continues.

---

## 12. Example workflow

```yaml
---
tracker:
  kind: linear
  provider:
    api_key: $LINEAR_API_KEY
    team_key: ENG
    require_assignee: true
  required_labels: ["agent"]
  active_states: ["Todo", "In Progress"]
  terminal_states: ["Done", "Canceled"]
agent:
  max_concurrent_agents: 4
codex:
  command: codex app-server
---

You are working on {{ issue.identifier }}: {{ issue.title }}.

When the change is ready, attach the pull request with `linear_attach_link`
and move the issue to Human Review with `linear_set_issue_state`.
```

---

## 13. Conformance

`tests/test_tracker_linear.py` covers this profile against synthetic GraphQL
payloads through `httpx.MockTransport` — no network in the default suite. The
SPEC 17.8 real-integration check is marked `@pytest.mark.integration` and skips
unless `LINEAR_API_KEY` and `LINEAR_TEAM_KEY` are set.
