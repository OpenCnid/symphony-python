# The RLM Addressability Surface

`src/symphony/rlm/` — how a Recursive Language Model drives a live Symphony
system from a Python REPL.

This module is **not** required by [`SPEC.md`](../SPEC.md). It is this
implementation's own extension, and it changes no spec-mandated behavior: it
observes and drives, it does not reimplement.

---

## 1. What an RLM is, in one minute

A normal language model receives its context as a flat prompt. Everything it
might need is pasted in up front, it reads all of it, and the cost of a question
scales with the size of the haystack rather than the size of the needle.

A **Recursive Language Model**
([Zhang & Khattab, 2025](https://arxiv.org/abs/2510.04871)) does not do that. The
context is stored as a *variable in a Python REPL*, and the root model's action
space is **Python code**. It emits code to measure the variable, slice it, chunk
it, grep it, and recurse — calling sub-model queries on the pieces and composing
the answers. The root model may never read most of the context at all. It reads
the parts its own code proved were relevant.

That changes what a good interface looks like. The consumer is not a person
scanning a screen; it is a program spending a token budget. So:

| A human-facing surface | A model-facing surface |
|---|---|
| Renders everything, lets the eye filter | Returns a small root view and lets code descend |
| Prose documentation, read once | Runtime introspection, queried per call |
| Errors when a thing is missing | Reports absence as data |
| Pretty-prints | Emits JSON that survives `json.dumps` |
| Size is incidental | Size is a declared, enforced parameter |

## 2. Why the design follows

Three constraints drove every decision here.

**A model pays for what it reads, so it must be able to price a query before
running it.** Every introspection result carries its own exact serialized size:

```python
result = system_map(depth=0)
assert measure_json(result) == result["_meta"]["chars"]   # exact, not an estimate
```

And every result is bounded by a `max_chars` the caller supplies. When a result
would exceed it, it is *shrunk* — long strings are cut, long lists elided with an
`{"omitted": n}` marker — and `_meta.truncated` becomes `True`. Shrinking never
touches keys that carry addressability (`module`, `name`, `kind`, `present`), so
a clamped result is still something you can descend into.

**A model descends; it does not re-read.** Everything is a depth ladder, and
every level is a strict superset of the one below. You start at depth 0 for a few
kilobytes, and pay for depth 2 on the one component you care about. Every result
also carries a `next_calls` list — the surface documents its own descent at
runtime, so nothing here needs this file to be usable.

**The parallel build means half the system may not exist.** Roughly 22 modules
are written concurrently by separate authors. Asking about one that has not
landed returns data, never an exception:

```python
{"present": false, "importable": false, "reason": "not_present",
 "expected_path": "src/symphony/orchestrator/core.py", "spec_sections": ["7", "8.1"]}
```

A module that exists but raises on import returns `reason: "import_failed"` with
the exception type and message. Both are queryable; neither is fatal. Use
`refresh()` when a sibling lands mid-session.

## 3. The three pieces

```
symphony.rlm.introspect   what is this system made of?
symphony.rlm.repl         hold it as a variable; slice, chunk, grep, execute
symphony.rlm.recursive    decompose it, query the pieces, combine
```

### `introspect` — bounded structural queries

| Call | Answers |
|---|---|
| `system_map(depth=…, state=…, group=…)` | What components exist, which are on disk |
| `describe_component(name, depth=…)` | One module: spec sections, symbols, signatures, class internals |
| `describe_object(obj, depth=…)` | Any live object once you hold one |
| `describe_state(state, depth=…)` | A cheap index of orchestrator runtime state |
| `components_for_spec("13.3")` | Which module owns a SPEC section |
| `find_symbol("backoff")` | Which module exposes a name |
| `resolve_component("loader")` | Loose name → full module path |
| `jsonable(v)` / `measure_json(p)` | Coerce anything JSON-safe; measure it exactly |
| `refresh()` | Re-scan the filesystem for modules that landed |

Names resolve loosely: `"loader"`, `"workflow.loader"`, and
`"symphony.workflow.loader"` all work. An ambiguous query returns its candidates
rather than guessing.

### `repl` — the addressable variable

`ReplEnv` binds a payload as a variable (default name `ctx`) and pre-loads the
primitives a model reaches for before deciding whether to recurse:

| Primitive | Use |
|---|---|
| `size(x)` | chars, estimated tokens, lines, item count |
| `peek(x, n, tail=…)` | first or last `n` characters |
| `window(x, start, end)` | slice text, sequences, or mapping key order |
| `chunk(x, max_chars=…, overlap=…)` | split on natural boundaries |
| `grep(x, pattern, context=…)` | line-numbered hits |
| `keys_of(x)` | addressable names inside a payload |
| `as_text(x)` | render any payload as line-addressable text |

`chunk` descends a boundary ladder — paragraph, then line, then sentence, then a
hard slice — dropping a level only when a piece is still too large. Two
invariants hold and are tested:

```python
all(len(c.text) <= max_chars for c in chunks)        # the budget is real
all(original[c.start:c.end] == c.text for c in chunks)  # chunks are addresses
```

Execution is bounded and auditable. `env.run(code)` captures stdout, evaluates a
trailing expression into `result.value` exactly as a real REPL would, records an
`ExecResult` in `env.history`, and **contains every failure** — including
`SystemExit`, so a stray `exit()` in generated code cannot kill the host. Only
`KeyboardInterrupt` propagates. `env.replay()` re-runs the recorded history in a
fresh environment, optionally against a different payload.

> `ReplEnv` is a containment boundary, not a security boundary. Executed code has
> full process privileges, exactly like `exec`. Do not point it at untrusted
> input.

### `recursive` — decompose, query, combine

```python
recursive_query(payload, query, *, sub_model=None, budget=None, combine=None)
```

**The sub-model is injected, not built in.** This package holds no credentials
and opens no sockets. `sub_model` is any `(query, context) -> answer` callable.
The default, `local_sub_model`, is a deterministic offline stand-in — it extracts
content terms from the query, scans the context line by line, and returns a
compact digest. It is not an approximation of a language model. It is a pure
function with the right signature, so the whole machinery is exercised by the
test suite with no network, and a failing test points at the machinery rather
than at model variance. In deployment:

```python
def my_sub_model(query: str, context: str) -> str:
    return my_client.complete(f"{query}\n\n---\n{context}").text

recursive_query(payload, "which issues stalled?", sub_model=my_sub_model)
```

**The budget is enforced, not documented.** `RecursionBudget` bounds four things:

| Bound | Meaning | Where it bites |
|---|---|---|
| `max_chunk_tokens` | Leaf size — no sub-model call ever receives more | Checked in `leaf()` before every call; oversized text is truncated and recorded |
| `max_depth` | Tree height | Checked in `solve()`; hitting it converts a node into a truncated leaf |
| `max_calls` | Total sub-model calls | Checked in `BudgetLedger.blocked_by` before every call and every split |
| `max_total_tokens` | Total tokens sent to the sub-model | Same check, same place |

`fanout` (default 8) gives depth meaning: each level splits into at most `fanout`
pieces, so a payload of ratio *R* over the leaf size needs about `log_fanout(R)`
levels.

When a bound binds, one of two things happens, and never a third:

- `on_exceeded="stop"` (default) — the run degrades to a partial answer.
  `result.partial` is `True` and `result.stops` names every bound that bound.
- `on_exceeded="raise"` — `BudgetExceeded` is raised. It subclasses
  `SymphonyError`, so it carries `.category` (`rlm_budget_exceeded`) and
  `.to_dict()` like every other error in this codebase.

Silence is never an option. The budget bounds what is *sent* to the sub-model,
not what comes back: `result.answer` can exceed the leaf budget if a reduction
pass was unaffordable — and that case always sets `partial` and appends a stop
reason.

`plan_recursion(payload, budget=…)` prices a query **without calling anything**.
It walks the same split logic and reports the tree it would build. Leaves are a
lower bound on calls and leaves + internal nodes an upper bound, because
reduction passes are data-dependent.

## 4. Relationship to SPEC 13.3 / 13.5

These complement the snapshot surface; they do not duplicate it.

SPEC 13.3 defines a *dashboard* payload: running rows with turn counts, retry
rows, aggregate token totals, live runtime seconds, latest rate limits. That is
owned by `symphony.observability.snapshot.build_snapshot(state)` and is the right
call when you want the whole picture rendered.

`describe_state(state)` answers a cheaper, earlier question: *how many things
exist, what are they called, and which one deserves my tokens?* Depth 0 is counts
and configuration only. Depth 1 adds one compact row per running and retrying
issue. Depth 2 adds recent events and last errors. The result carries a `note`
pointing at `build_snapshot` for the full 13.3 shape.

Token accounting (13.5) is not reimplemented here at all. `describe_state` reads
`state.codex_totals` through the same `CodexTotals.to_dict()` the snapshot uses.

## 5. A worked session

Against this repository, in a REPL. Every number below is a real observed value.

**Start at the root.** One call, no arguments, no prior knowledge:

```python
>>> from symphony.rlm import *
>>> m = system_map(depth=0)
>>> m["counts"]
{'components': 33, 'present': 33, 'absent': 0, 'import_failed': 0}
>>> m["_meta"]["chars"]
3717
```

3.7 KB buys the entire component inventory with a presence verdict on each. If a
sibling had not landed yet it would show `present: False` with the path it will
occupy — the map is complete whether or not the code is.

**Ask who owns a spec section** rather than guessing at file names:

```python
>>> components_for_spec("8.4")
['symphony.orchestrator.retry']
```

**Descend, paying per level.** Four calls, four prices:

```python
>>> [describe_component("retry", depth=d)["_meta"]["chars"] for d in (0, 1, 2, 3)]
[566, 1339, 2753, 6438]
```

Depth 0 is presence. Depth 1 adds symbol *names*. Depth 2 adds signatures and
one-line docs. Depth 3 adds class internals. Stop at the level that answers your
question:

```python
>>> [s for s in describe_component("retry", depth=2)["symbols"]
...  if s["symbol"] == "backoff_delay_ms"]
[{'symbol': 'backoff_delay_ms',
  'kind': 'function',
  'signature': "(attempt: 'int', max_backoff_ms: 'int') -> 'int'",
  'doc': 'Failure-retry delay ``min(10000 * 2^(attempt - 1), max_backoff_ms)`` (SPEC 8.4).'}]
```

Total spent to go from *nothing* to a callable signature with its spec citation:
about 6.6 KB across four calls. Reading `retry.py` would have cost several times
that, and reading the repository to find it, orders of magnitude more.

**Search when you do not know the owner:**

```python
>>> find_symbol("backoff")["matches"]
[{'module': 'symphony.workflow.config', 'symbol': 'DEFAULT_MAX_RETRY_BACKOFF_MS',
  'kind': 'constant', 'type': 'int', 'value': 300000},
 {'module': 'symphony.orchestrator.retry', 'symbol': 'DEFAULT_MAX_RETRY_BACKOFF_MS',
  'kind': 'constant', 'type': 'int', 'value': 300000},
 {'module': 'symphony.orchestrator.retry', 'symbol': 'backoff_delay_ms',
  'kind': 'function', 'signature': "(attempt: 'int', max_backoff_ms: 'int') -> 'int'",
  'doc': 'Failure-retry delay ``min(10000 * 2^(attempt - 1), max_backoff_ms)`` (SPEC 8.4).'}]
```

**Now the other axis — an oversized payload.** `SPEC.md` is 92 KB, roughly 23 k
tokens. Bind it and measure before reading:

```python
>>> spec = pathlib.Path("SPEC.md").read_text(encoding="utf-8")
>>> env = open_repl(context=spec)
>>> env.run("size(ctx)").value
{'type': 'str', 'chars': 91923, 'tokens': 22981, 'lines': 2312, 'items': 91923}
```

Locate before you read:

```python
>>> env.run(r"grep(ctx, r'^### 8\.4')").value
[{'line': 790, 'text': '### 8.4 Retry and Backoff'}]
```

One grep, ~40 characters of result, and you know exactly where to slice. Chunks
are addresses, so you can widen around a hit without re-chunking:

```python
>>> env.run("[c.to_dict() for c in chunk(ctx, max_chars=20000)][:3]").value
[{'index': 0, 'start': 0,     'end': 19958, 'chars': 19958, 'tokens': 4990},
 {'index': 1, 'start': 19958, 'end': 39780, 'chars': 19822, 'tokens': 4956},
 {'index': 2, 'start': 39780, 'end': 59762, 'chars': 19982, 'tokens': 4996}]
```

**Price a recursive query before paying for it:**

```python
>>> plan_recursion(spec, budget=RecursionBudget(max_chunk_tokens=2000))
{'kind': 'recursion_plan', 'chars': 91923, 'tokens': 22981,
 'estimated_calls': 16, 'max_calls_upper_bound': 25, 'internal_nodes': 9,
 'estimated_tokens_charged': 22983, 'estimated_depth': 2,
 'truncated_leaves': 0, 'fits_budget': True, 'would_stop_on': [], ...}
```

16 to 25 sub-model calls, ~23 k tokens, two levels deep, nothing truncated. Run
it:

```python
>>> res = recursive_query(spec, "retry backoff",
...                       budget=RecursionBudget(max_chunk_tokens=2000))
>>> res.calls, res.depth_reached, res.partial, res.stops
(17, 2, False, ())
```

17 calls — the 16 predicted leaves plus one reduction pass, inside the predicted
upper bound. Not partial, nothing dropped.

**Watch the budget actually bind.** Same query, six calls allowed:

```python
>>> res = recursive_query(spec, "retry backoff",
...                       budget=RecursionBudget(max_chunk_tokens=2000, max_calls=6))
>>> res.calls, res.partial, res.stops
(6, True, ('max_calls',))
```

Exactly six calls, `partial=True`, and the reason named. Not "about six", not a
warning in a log — the number, the flag, and the cause, all in the return value.

Set `on_exceeded="raise"` and the same run raises `BudgetExceeded` with
`.to_dict()["details"]["reason"] == "max_calls"` instead.

## 6. Driving live state

```python
>>> env = open_repl(state=orchestrator.state)
>>> env.run("describe_state(state)['counts']").value
{'running': 1, 'claimed': 2, 'retry_queued': 1, 'completed': 1}
>>> env.run("describe_state(state, depth=1)['running'][0]").value
{'issue_id': 'i-1', 'identifier': 'ENG-101', 'phase': 'StreamingTurn',
 'started_at': '2026-07-28T12:00:00+00:00', 'turn_count': 3,
 'workspace_path': '/tmp/ws/ENG-101'}
>>> env.run("describe_object(state.running['i-1'].issue, depth=2)").value["values"]["title"]
'Wire up retry backoff'
```

Counts first, then one row, then the object. Each step costs what it needs to and
nothing more.

## 7. Quick reference

```python
from symphony.rlm import (
    # introspection
    system_map, describe_component, describe_object, describe_state,
    components_for_spec, find_symbol, resolve_component, refresh,
    jsonable, measure_json, REGISTRY, ComponentSpec,
    # repl
    ReplEnv, ExecResult, Chunk, open_repl,
    size, peek, window, chunk, grep, keys_of, as_text, estimate_tokens,
    # recursion
    recursive_query, plan_recursion, RecursionBudget, BudgetLedger,
    RecursiveResult, BudgetExceeded, RlmError, SubModel,
    local_sub_model, default_combine,
)
```

Tests: `tests/test_rlm.py` (`pytest tests/test_rlm.py -q`).
