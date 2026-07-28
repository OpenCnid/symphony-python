"""RLM addressability surface — make Symphony a first-class object in a REPL.

This package is **not** required by ``SPEC.md``. It is this implementation's
extension, and it changes no spec-mandated behavior: everything here observes
and drives; nothing here reimplements.

It exists because Python was chosen so a *Recursive Language Model* could drive
Symphony. An RLM (Zhang & Khattab, arXiv:2510.04871) does not consume context as
a flat prompt — it holds context as a variable in a Python REPL and emits code
to slice, chunk, filter, and recurse over it, calling sub-model queries on the
pieces and composing the results. Its action space is Python code. This package
is the surface that makes that cheap.

Three capabilities, in dependency order:

``introspect``
    What is this system made of? Depth- and size-bounded structural queries that
    return JSON-safe dicts carrying their own exact serialized size. Components
    not yet on disk are reported as data, never as an ``ImportError``.

``repl``
    A namespace holding the system (or any oversized payload) as an addressable
    variable, with the primitives a model uses before recursing — ``size``,
    ``peek``, ``window``, ``chunk``, ``grep`` — and bounded, auditable,
    non-fatal execution.

``recursive``
    Decompose an oversized payload, query each piece, combine. The sub-model is
    an injected parameter with a deterministic offline default; depth and token
    budgets are enforced at every call site, not merely documented.

Start here::

    from symphony.rlm import open_repl, system_map

    system_map(depth=0)                      # root view, a few KB
    env = open_repl(context=big_payload)     # bind context as `ctx`
    env.run("size(ctx)").value               # price it
    env.run("recursive_query(ctx, 'which modules own retry?').to_dict()").value

The relationship to SPEC 13.3/13.5 is complementary, not duplicative: the
dashboard-shaped snapshot stays in ``symphony.observability.snapshot``;
:func:`~symphony.rlm.introspect.describe_state` gives the cheap index a model
uses to decide which row deserves the tokens.
"""

from __future__ import annotations

from typing import Any

from symphony.rlm.introspect import (
    REGISTRY,
    ComponentSpec,
    components_for_spec,
    describe_component,
    describe_object,
    describe_state,
    find_symbol,
    groups,
    jsonable,
    measure_json,
    refresh,
    resolve_component,
    system_map,
)
from symphony.rlm.recursive import (
    BudgetExceeded,
    BudgetLedger,
    RecursionBudget,
    RecursiveResult,
    RlmError,
    SubModel,
    default_combine,
    local_sub_model,
    plan_recursion,
    recursive_query,
)
from symphony.rlm.repl import (
    CHARS_PER_TOKEN,
    Chunk,
    ExecResult,
    ReplEnv,
    as_text,
    chunk,
    estimate_tokens,
    grep,
    keys_of,
    peek,
    size,
    window,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "REGISTRY",
    "BudgetExceeded",
    "BudgetLedger",
    "Chunk",
    "ComponentSpec",
    "ExecResult",
    "RecursionBudget",
    "RecursiveResult",
    "ReplEnv",
    "RlmError",
    "SubModel",
    "as_text",
    "chunk",
    "components_for_spec",
    "default_combine",
    "describe_component",
    "describe_object",
    "describe_state",
    "estimate_tokens",
    "find_symbol",
    "grep",
    "groups",
    "jsonable",
    "keys_of",
    "local_sub_model",
    "measure_json",
    "open_repl",
    "peek",
    "plan_recursion",
    "recursive_query",
    "refresh",
    "resolve_component",
    "size",
    "system_map",
    "window",
]


def open_repl(
    *,
    context: Any = None,
    state: Any = None,
    name: str = "ctx",
    extra: dict[str, Any] | None = None,
) -> ReplEnv:
    """Build a :class:`~symphony.rlm.repl.ReplEnv` wired for driving Symphony.

    Binds ``context`` as ``ctx`` (or ``name``), optionally binds live
    orchestrator state as ``state``, and pre-loads every primitive, every
    introspection function, and ``recursive_query``. One call gets a model from
    nothing to a full action space.
    """
    bindings: dict[str, Any] = dict(extra or {})
    if state is not None:
        bindings.setdefault("state", state)
    return ReplEnv(context=context, name=name, extra=bindings)
