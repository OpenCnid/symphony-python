# Adapter profile: `memory`

The compact adapter profile required by **SPEC 11.2**, for the in-process
reference adapter implemented in `src/symphony/trackers/memory.py`.

The `memory` adapter keeps provider-shaped records in a Python list. It performs
no I/O, so it is the adapter to reach for when driving Symphony from a REPL, and
it is the adapter the conformance suite uses to exercise SPEC 11.4 error
handling without a network. It is also the reference implementation of the
SPEC 11.1 read kernel: the semantics below are what other adapters are checked
against.

---

## 1. Supported `tracker.kind`

```yaml
tracker:
  kind: memory
```

Exactly `memory`. Registered via `@register_adapter`, so
`symphony.trackers.base.build_adapter("memory", provider, **kwargs)` returns a
`MemoryTrackerAdapter`.

Registration happens at **module import**. `symphony.trackers.memory` must be
imported before `build_adapter("memory", ...)` or `adapter_kinds()` can resolve
the kind; importing `symphony.trackers` alone is not enough today.

### Default states (SPEC 5.3.1)

`tracker.active_states` and `tracker.terminal_states` MAY be omitted; this
profile documents the defaults:

| Setting | Default |
|---|---|
| `active_states` | `["Todo", "In Progress"]` |
| `terminal_states` | `["Done", "Canceled"]` |

Both are compared case-insensitively after trimming (SPEC 4.2). The effective
values are passed to the constructor as the `active_states` / `terminal_states`
keyword arguments; `terminal_states` also drives blocker resolution (§5).

---

## 2. `tracker.provider` keys

Every key is OPTIONAL. **Unknown keys are rejected** with
`InvalidTrackerConfig` — the adapter owns this schema (SPEC 5.3.1), and a typo
in a seed fixture is worth failing loudly.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `seed` | list of maps | `[]` | Initial provider records (§4). |
| `scope` | string \| null | `null` | Provider scope selector (§3). |
| `page_size` | int >= 0 | `0` | Records per simulated page; `0` = one page. |
| `max_ids_per_request` | int >= 0 | `0` | ID-refresh batch limit; `0` = one batch. |
| `require_assignee` | bool | `false` | Routing rule input for `dispatchable` (§5). |
| `secret_env` | string \| null | `null` | Name of the env var holding the adapter credential. |

### Secret keys and environment names

The `memory` adapter needs no credential to function. `secret_env` exists so the
launcher's secret-stripping path (SPEC 10.5 / 15.3) has something to exercise:

- `secret_env` names an environment variable, e.g. `SYMPHONY_MEMORY_TOKEN`.
- If it is set, `secret_environment_names()` returns `["SYMPHONY_MEMORY_TOKEN"]`
  and the launcher removes that name from local and remote child environments.
- If the variable is unset **or resolves to an empty/whitespace string**, the
  secret is *missing* (SPEC 5.3.1) and construction raises
  `MissingTrackerSecret`.
- The credential **value** is never read into adapter state, `native_ref`, a log
  field, or `repr()`. Presence is validated; the value is not retained.

`$VAR_NAME` expansion in `tracker.provider` is performed by
`symphony.workflow.config.expand_value` before the adapter is constructed; the
adapter sees resolved strings.

### Validation errors

| Condition | Raised | `category` |
|---|---|---|
| `tracker.provider` is not a mapping | `InvalidTrackerConfig` | `invalid_tracker_config` |
| unknown `provider` key | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `seed` is not a list, or an element is not a map | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `page_size` / `max_ids_per_request` not an int >= 0 | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `require_assignee` not a bool | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `scope` not a string | `InvalidTrackerConfig` | `invalid_tracker_config` |
| duplicate dispatch `id` within one scope | `InvalidTrackerConfig` | `invalid_tracker_config` |
| duplicate `identifier` within one scope | `InvalidTrackerConfig` | `invalid_tracker_config` |
| `secret_env` names an unset/empty variable | `MissingTrackerSecret` | `missing_tracker_secret` |

Uniqueness is enforced because SPEC 4.1.1 requires `id` to be a stable dispatch
identity and `identifier` to be unique within the configured scope — the latter
names workspace directories.

---

## 3. Scope selection, pagination, and request limits

**Scope.** Each record MAY carry a `scope` string. When `provider.scope` is set,
only records whose `scope` matches exactly are visible to `fetch_issues_by_states`,
`fetch_issues_by_ids`, the agent tools, and the record helpers. When
`provider.scope` is `null`, every seeded record is in scope. Two records may
reuse an `identifier` if they live in different scopes.

**Pagination.** `fetch_issues_by_states` walks the selected records in seed
order in pages of `page_size`. Each page is one simulated provider request, and
order is preserved across pages. `page_size: 0` means a single page. Even when
nothing matches, exactly one provider request is made — the request happened,
it just came back empty.

**Request limits.** `fetch_issues_by_ids` splits the requested ID set into
batches of `max_ids_per_request`, one simulated provider request each.
`max_ids_per_request: 0` means one batch.

**No-request short circuits (SPEC 11.1, MUST).** `fetch_issues_by_states([])`
and `fetch_issues_by_ids([])` return `[]` *before* any provider work. Armed
faults do not fire on these calls, because no request is made. Provider traffic
is observable in `adapter.calls` / `adapter.provider_calls`.

A `state_names` list containing only blank strings is *not* empty, so one
provider request is made and it returns `[]`.

---

## 4. Provider record shape and `id` / `native_ref` mapping

A seed record is a plain map. Recognized fields:

```python
{
  "id":          "TCK-9",             # underlying ticket ID
  "item_id":     "PI-1",              # OPTIONAL board/project-item ID
  "identifier":  "ABC-1",
  "title":       "Fix the login form",
  "state":       "In Progress",
  "dispatchable": True,               # OPTIONAL explicit override
  "description": "...", "priority": 2, "branch_name": "...", "url": "...",
  "assignee_id": "u-7",
  "labels":      ["Backend", {"name": "UI"}],
  "blocked_by":  [{"id": "...", "identifier": "ABC-9", "state": "Done"}],
  "created_at":  "2026-01-02T03:04:05Z", "updated_at": "...",
  "native_ref":  {"board": "roadmap"},
  "scope":       "team-a",            # scope selector
  "archived":    False,               # routing rule input
  "comments":    [],                  # written by the memory_add_comment tool
}
```

### `id` mapping

`Issue.id` is `item_id` when present and non-empty, else `id`. This models a
provider where the dispatch identity is a project-item ID rather than the
underlying ticket ID (SPEC 4.1.1). It is opaque to the orchestrator.

### `native_ref` mapping

`Issue.native_ref` is `null` or a JSON-safe map of non-secret values
(SPEC 11.3):

1. Start from the record's `native_ref` map, if it is a map.
2. Drop keys that are not strings.
3. Drop keys matching `token|secret|password|passwd|api[_-]key|authoriz|credential|cookie|session_id`
   (case-insensitive).
4. Drop values that are not JSON-safe (non-`str`/`int`/`float`/`bool`/`null`,
   NaN/inf, non-string map keys, or nesting deeper than 6 levels).
5. When `item_id` is present and differs from `id`, add
   `"ticket_id": <record id>` so the distinct underlying ticket ID survives for
   provider-native tools. This key wins over any same-named key in the record.
6. If nothing remains, `native_ref` is `null`.

Retained entries are preserved verbatim.

---

## 5. Normalization

Adapter output satisfies SPEC 4.1.1 and SPEC 11.3.

| Field | Rule |
|---|---|
| `id`, `identifier`, `title`, `state` | REQUIRED non-empty strings after trimming. Absent/blank ⇒ **malformed**. |
| `state` | Provider spelling preserved verbatim; only comparisons are trimmed + lowercased. |
| `labels` | Trimmed, lowercased, blanks dropped, duplicates removed, order preserved. `{"name": ...}` entries accepted. A non-list ⇒ `[]`. |
| `priority` | Integer or `null`. Numeric strings are parsed; `bool` is rejected (`True` ⇒ `null`, not `1`). |
| `created_at`, `updated_at` | Parsed RFC 3339 instants (trailing `Z` accepted) or `null`. A `datetime` object without `tzinfo` is assumed UTC; an offset-less *string* parses to a naive instant, per `symphony.trackers.base.parse_rfc3339`. |
| `branch_name`, `url`, `assignee_id` | Trimmed non-empty string or `null`; anything else ⇒ `null`. |
| `description` | Any string, preserved **verbatim** including surrounding whitespace and `""` — it is prompt content, so trimming would be lossy. A non-string ⇒ `null`. |
| `blocked_by` | Best-effort. Non-map entries and all-empty refs are dropped; a non-list ⇒ `[]`. No blocker semantics are invented. |
| `native_ref` | See §4. |
| `dispatchable` | Explicit boolean, see below. |

### `dispatchable` derivation (SPEC 11.2, 11.3)

If the record contains a `dispatchable` key, its value is used **verbatim** and
MUST be a boolean; a non-boolean is malformed. This is the seed's escape hatch
for fixtures that want to control eligibility directly.

Otherwise the documented `memory` routing rule applies. `dispatchable` is `true`
only when all of the following hold:

1. `archived` is falsy;
2. `provider.require_assignee` is `false`, **or** `assignee_id` is a non-empty
   string;
3. every entry in `blocked_by` has a `state` whose normalized form is in the
   **configured** `terminal_states`. A blocker with no state counts as
   unresolved.

The generic scheduler never reconstructs these checks from `native_ref`
(SPEC 11.3).

### Malformed records and the SPEC 11.1 asymmetry

A record is malformed only when `id`, `identifier`, `title`, `state`, or an
explicit `dispatchable` cannot be produced. Unusable *optional* values fall back
to `null` / `[]`; that fallback alone never makes a record malformed.

| Operation | Behavior on a malformed record |
|---|---|
| `fetch_issues_by_states` | **Omitted** and logged at `warning` with `issue_id`, `issue_identifier`, and `reason` (SPEC 13.1 `key=value`). It was never safe to dispatch. |
| `fetch_issues_by_ids` | **Fails** with `TrackerResponseError`. Omission is meaningful here — the orchestrator reads it as "no longer visible" — so silence would be a lie. |

Omissions from the last state-list read are also available structurally as
`adapter.last_normalization_report.omitted` (a `list[str]` of reasons).

`fetch_issues_by_states` selects a record whose `state` is missing or blank even
though it can match no requested state, so that its malformedness is reported
rather than silently swallowed by the state filter.

A record with neither `id` nor `item_id` has no dispatch identity at all: it can
never be *requested*, so `fetch_issues_by_ids` treats it as invisible, while
`fetch_issues_by_states` reports it as malformed.

For `fetch_issues_by_ids`, input IDs are treated as a set (blank entries and
duplicates dropped), each dispatch ID appears at most once in the result, output
order follows the deduplicated input order, and a successful result is complete
for that call.

---

## 6. Provider-native agent tools (SPEC 10.5, 11.5)

Three tools are advertised through `agent_tool_specs()`. They execute host-side
with the adapter's configuration; the coding-agent child never receives a
credential.

| Tool | Mutates tracker | Input schema | Success payload |
|---|---|---|---|
| `memory_get_issue` | no | `{issue_id?: string}` | `{issue: <normalized issue map>, comments: [...]}` |
| `memory_add_comment` | **yes** | `{issue_id?: string, body: string}` | `{issue_id, comment_index, created_at}` |
| `memory_set_state` | **yes** | `{issue_id?: string, state: string}` | `{issue_id, previous_state, state}` |

All schemas are `{"type": "object", "additionalProperties": false}`.

**Scope / authorization.** `issue_id` defaults to `context.issue.id` — the
current normalized issue (SPEC 10.5). A tool can only read or mutate records
inside `provider.scope`; a target outside it returns a failure and mutates
nothing. There is no cross-scope escape.

**Result and error semantics.** Every failure path returns a structured
`ToolResult(ok=False, error=..., content=...)` and **never raises**, so the
session continues rather than stalling (SPEC 10.5):

| Condition | `error` | `content` |
|---|---|---|
| unknown tool name | `unsupported tool: <name>` | `{tool, supported: [...]}` |
| `arguments` is not an object | `tool arguments must be a JSON object` | `{tool, got}` |
| no `issue_id` and no context issue | `issue_id is required when no current issue is in tool context` | `null` |
| target outside scope / not found | `issue <id> is not visible in the configured tracker scope` | `{issue_id, scope}` |
| `body` / `state` missing or blank | `'body' must be a non-empty string` etc. | `null` |
| target record is malformed | the `TrackerResponseError` message | `{category: "tracker_response"}` |
| an injected fault is armed | the tracker error message | `{tool, category: <SPEC 11.4 category>}` |

**Idempotency / rate limits.** `memory_add_comment` is *not* idempotent — each
call appends. `memory_set_state` is idempotent. There is no real provider rate
limit; the simulated one is opt-in (§8).

Mutations are visible immediately to both read operations, which is what makes
the "reached the next handoff state" workflow of SPEC 11.5 exercisable
end-to-end without a network.

---

## 7. Error mapping (SPEC 11.4)

The public error form is a **raised Python exception** from
`symphony.errors`. Every class carries `.category` (the SPEC slug), `.message`,
`.details`, and `.to_dict()`; `TrackerError` subclasses may also carry
`.retryable` and `.retry_after_ms`. The orchestrator relies only on success
versus failure.

| Python exception | `category` | Raised when | Message shape |
|---|---|---|---|
| `InvalidTrackerConfig` | `invalid_tracker_config` | any §2 config validation failure | `tracker.provider.<key> must be ...` / `unknown tracker.provider key(s) for 'memory': ...` |
| `MissingTrackerSecret` | `missing_tracker_secret` | `secret_env` unset or empty | `tracker secret environment variable 'NAME' is unset or empty` |
| `UnsupportedTrackerKind` | `unsupported_tracker_kind` | raised by `build_adapter` for a non-`memory` kind, not by this adapter | — |
| `TrackerRequestError` | `tracker_request` | injected transport fault, or any read after `aclose()` | `memory tracker transport failure during <op>: <detail>` (`retryable=True`) |
| `TrackerStatusError` | `tracker_status` | injected non-success response | `memory tracker returned status <code> during <op>: <detail>` (`retryable` = `code >= 500`, `details.status`) |
| `TrackerResponseError` | `tracker_response` | a malformed *requested* record in `fetch_issues_by_ids` | `malformed record from <op>: missing or empty required field '<field>'` (`details.field`) |
| `TrackerPaginationError` | `tracker_pagination` | injected paging integrity fault | `memory tracker pagination integrity failure after <n> page(s)` |
| `TrackerRateLimited` | `tracker_rate_limited` | injected rate-limit fault | `memory tracker rate limited during <op>` (`retry_after_ms`) |

Orchestrator behavior on these failures is unchanged from SPEC 11.4: candidate
fetch failure skips dispatch for the tick, running-state refresh failure keeps
active workers running, and startup terminal cleanup failure logs a warning and
continues.

---

## 8. Fault injection (implementation extension)

Because this adapter is in-process, it can misbehave on demand. The controls
below make each SPEC 11.4 category reachable from a test or a REPL without a
provider. All are no-ops until armed; arming one clears the others.

```python
adapter.fail_requests("connection reset by peer", times=None)  # -> TrackerRequestError
adapter.fail_status(503, "upstream unavailable", times=None)   # -> TrackerStatusError
adapter.rate_limit(retry_after_ms=1500, times=None)            # -> TrackerRateLimited
adapter.fail_pagination(after_pages=1, times=None)             # -> TrackerPaginationError
adapter.clear_faults()
```

- `times=N` applies the fault to the next `N` provider calls, then auto-disarms;
  `times=None` (default) means "until cleared". Useful for retry tests.
- `fail_pagination(after_pages=N)` serves `N` pages normally and then fails, so
  a partial page walk can be shown to be unobservable to the scheduler.
- Faults do **not** fire on the empty-input short circuits (§3), because those
  make no provider request.
- Faults also apply to agent tool calls, where they surface as structured
  `ToolResult` failures carrying `content["category"]` rather than as raises.

`TrackerResponseError` is reached without a fault switch, by seeding or
corrupting a record:

```python
adapter.corrupt("T-2", "title", "")      # blank a required field
adapter.add(id="T-3", identifier="ABC-3", title="x", state="Todo", dispatchable="yes")
```

### Inspection and record helpers

| Member | Purpose |
|---|---|
| `adapter.calls` | Ordered `ProviderCall(op, detail)` log of simulated requests. |
| `adapter.provider_calls` | `len(adapter.calls)`. |
| `adapter.reset_calls()` | Clear the request log. |
| `adapter.last_normalization_report` | Omissions from the last state-list read. |
| `adapter.records()` | Raw in-scope records, in seed order. |
| `adapter.add(**fields)` / `extend(records)` | Append records (uniqueness enforced). |
| `adapter.update(id, **fields)` / `remove(id)` | Patch or delete a record; `KeyError` if absent. |
| `adapter.corrupt(id, field, value="")` | Make a record malformed on purpose. |
| `adapter.aclose()` / `adapter.closed` | Idempotent close; later reads raise `TrackerRequestError`. |

---

## 9. Worked example

```python
import asyncio

import symphony.trackers.memory  # noqa: F401 — importing registers the adapter
from symphony.trackers.base import ToolContext, build_adapter

t = build_adapter("memory", {"page_size": 2}, active_states=["Todo"])
t.add(id="T-1", identifier="ABC-1", title="Fix login", state="Todo", priority=1)
t.add(id="T-2", identifier="ABC-2", title="Add tests", state="Todo",
      blocked_by=[{"identifier": "ABC-1", "state": "Todo"}])

async def main() -> None:
    todo = await t.fetch_issues_by_states(["Todo"])
    print([(i.identifier, i.dispatchable) for i in todo])
    # [('ABC-1', True), ('ABC-2', False)]   ABC-2 has an unresolved blocker

    await t.execute_agent_tool(
        "memory_set_state", {"state": "Human Review"}, ToolContext(issue=todo[0])
    )
    print([i.state for i in await t.fetch_issues_by_ids(["T-1"])])
    # ['Human Review']

    t.rate_limit(times=1)
    try:
        await t.fetch_issues_by_states(["Todo"])
    except Exception as exc:
        print(exc.category, exc.retry_after_ms)   # tracker_rate_limited 1000

asyncio.run(main())
```

---

## 10. Conformance coverage

`tests/test_tracker_memory.py` exercises the SPEC 17.3 matrix against this
profile: active-state + scope selection, both empty-input short circuits,
multi-page order preservation, label/priority/timestamp/optional-field
normalization, the malformed-record asymmetry (omit + log vs. fail),
full-snapshot ID refresh, `native_ref` ticket-ID preservation and secret
stripping, routing rules becoming explicit `dispatchable`, and every SPEC 11.4
category with its documented message mapping.
