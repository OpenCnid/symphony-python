"""Budgeted decompose / query / combine recursion over an oversized payload.

This is the pattern the rest of :mod:`symphony.rlm` exists to serve. A payload
that does not fit a model's context is split into pieces that do, a sub-model
query runs per piece, and the partial answers are combined — recursively, until
the result fits (Zhang & Khattab, arXiv:2510.04871).

Two design commitments make this usable and testable here:

**The sub-model is an injected parameter, not a client.** This package holds no
API credentials and opens no sockets. :func:`recursive_query` takes
``sub_model: Callable[[str, str], str]`` — ``(query, context) -> answer``. The
default, :func:`local_sub_model`, is a deterministic offline stand-in, so the
whole machinery is exercised by the test suite with no network. A real
deployment passes its own closure over whatever client it already has.

**The budget is enforced, not documented.** :class:`RecursionBudget` bounds
depth, per-call size, call count, and total tokens; :class:`BudgetLedger` is
checked immediately before every sub-model call and at every split. When a bound
binds, the run either degrades to a *partial* answer with a recorded stop reason
(``on_exceeded="stop"``, the default) or raises :class:`BudgetExceeded`
(``on_exceeded="raise"``). Silence is never an option: ``result.partial`` and
``result.stops`` always say what was dropped.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from symphony.errors import SymphonyError
from symphony.rlm.introspect import jsonable
from symphony.rlm.repl import CHARS_PER_TOKEN, as_text, chunk, estimate_tokens

__all__ = [
    "BudgetExceeded",
    "BudgetLedger",
    "RecursionBudget",
    "RecursiveResult",
    "RlmError",
    "SubModel",
    "default_combine",
    "local_sub_model",
    "plan_recursion",
    "recursive_query",
]


class RlmError(SymphonyError):
    """Base error for the RLM extension surface.

    Defined here rather than in :mod:`symphony.errors` because that module is
    the spec-mandated taxonomy (SPEC 5.5, 10.6, 11.4) and this surface is not
    spec-mandated. Subclassing :class:`~symphony.errors.SymphonyError` keeps the
    house rule intact: every raised error carries a ``category`` and
    ``to_dict()``.
    """

    category = "rlm_error"


class BudgetExceeded(RlmError):
    """A recursion bound was hit while ``on_exceeded="raise"``."""

    category = "rlm_budget_exceeded"


SubModel = Callable[[str, str], str]
"""``(query, context) -> answer``. The one function a deployment must supply."""

Combiner = Callable[[str, "Sequence[str]", int], str]
"""``(query, answers, depth) -> merged``. Pure; must not call a model."""


# --------------------------------------------------------------------------
# Offline default sub-model
# --------------------------------------------------------------------------

_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,}")
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "what", "which", "from", "does",
        "are", "was", "were", "has", "have", "how", "why", "who", "where", "when",
        "all", "any", "list", "find", "show", "give", "into", "about", "answer",
    }
)


def _terms(query: str) -> list[str]:
    seen: list[str] = []
    for match in _TERM_RE.findall(query.lower()):
        if match in _STOPWORDS or match in seen:
            continue
        seen.append(match)
    return seen[:8]


def local_sub_model(query: str, context: str) -> str:
    """Deterministic offline stand-in for a real sub-model call.

    Extracts content terms from ``query``, scans ``context`` line by line, and
    returns a compact digest: how many lines matched and the first few, trimmed.
    It is *not* an approximation of a language model — it is a pure function
    with the same signature, so the recursion machinery can be tested end to end
    with no credentials and no network, and so a failing test points at the
    machinery rather than at model variance.

    The output is itself line-oriented, which means it recombines through
    :func:`default_combine` and survives a second reduction pass unchanged in
    shape.
    """
    terms = _terms(query)
    lines = [ln.strip() for ln in context.splitlines() if ln.strip()]
    hits = lines if not terms else [ln for ln in lines if any(t in ln.lower() for t in terms)]
    head = " | ".join(ln[:120] for ln in hits[:3])
    return f"[{len(hits)}/{len(lines)} lines] {head}".rstrip()


def default_combine(query: str, answers: Sequence[str], depth: int) -> str:
    """Merge child answers into one text block, dropping empties.

    Deliberately lossless and model-free: reduction — if the merged text is
    still too large — is the recursion's job, not the combiner's, so that every
    model call is charged to the ledger.
    """
    kept = [a.strip() for a in answers if a and a.strip()]
    if not kept:
        return ""
    return "\n".join(f"[d{depth}.{i}] {a}" for i, a in enumerate(kept))


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecursionBudget:
    """Hard bounds on a recursive query.

    ``max_chunk_tokens`` is the leaf size: no sub-model call ever receives more
    than ``max_chunk_tokens * CHARS_PER_TOKEN`` characters, at any depth. It is
    the bound that makes truncation visible rather than silent.

    ``fanout`` gives depth meaning. Each level splits into at most ``fanout``
    pieces (or leaf-sized pieces, whichever is larger), so a payload of ratio
    *R* over the leaf size needs about ``log_fanout(R)`` levels. When
    ``max_depth`` cuts the tree short, oversized leaves are truncated to the
    leaf size and ``max_depth_truncated`` is recorded.
    """

    max_depth: int = 4
    max_chunk_tokens: int = 1_024
    max_calls: int = 128
    max_total_tokens: int = 500_000
    fanout: int = 8
    overlap_tokens: int = 0
    on_exceeded: str = "stop"

    def __post_init__(self) -> None:
        if self.max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        if self.max_chunk_tokens <= 0:
            raise ValueError("max_chunk_tokens must be positive")
        if self.max_calls <= 0:
            raise ValueError("max_calls must be positive")
        if self.max_total_tokens <= 0:
            raise ValueError("max_total_tokens must be positive")
        if self.fanout < 2:
            raise ValueError("fanout must be >= 2")
        if self.overlap_tokens < 0:
            raise ValueError("overlap_tokens must be >= 0")
        if self.on_exceeded not in {"stop", "raise"}:
            raise ValueError("on_exceeded must be 'stop' or 'raise'")

    @property
    def max_chunk_chars(self) -> int:
        return self.max_chunk_tokens * CHARS_PER_TOKEN

    @property
    def overlap_chars(self) -> int:
        return min(self.overlap_tokens * CHARS_PER_TOKEN, self.max_chunk_chars - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_depth": self.max_depth,
            "max_chunk_tokens": self.max_chunk_tokens,
            "max_chunk_chars": self.max_chunk_chars,
            "max_calls": self.max_calls,
            "max_total_tokens": self.max_total_tokens,
            "fanout": self.fanout,
            "overlap_tokens": self.overlap_tokens,
            "on_exceeded": self.on_exceeded,
        }


@dataclass(slots=True)
class BudgetLedger:
    """Mutable accounting for one :func:`recursive_query` run.

    Separated from :class:`RecursionBudget` so the budget stays a frozen,
    reusable value while spend is per-run and inspectable afterwards.
    """

    budget: RecursionBudget
    calls: int = 0
    tokens: int = 0
    depth_reached: int = 0
    chunks: int = 0
    stops: list[str] = field(default_factory=list)

    def note(self, reason: str) -> None:
        """Record a bound that bound, or raise if the budget says to."""
        if reason not in self.stops:
            self.stops.append(reason)
        if self.budget.on_exceeded == "raise":
            raise BudgetExceeded(
                f"recursion budget exceeded: {reason}",
                reason=reason,
                calls=self.calls,
                tokens=self.tokens,
                depth_reached=self.depth_reached,
            )

    def blocked_by(self, tokens: int) -> str | None:
        """Reason the next ``tokens``-sized call cannot proceed, if any."""
        if self.calls >= self.budget.max_calls:
            return "max_calls"
        if self.tokens + tokens > self.budget.max_total_tokens:
            return "max_total_tokens"
        return None

    def spend(self, tokens: int, depth: int) -> None:
        self.calls += 1
        self.tokens += tokens
        self.depth_reached = max(self.depth_reached, depth)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "tokens": self.tokens,
            "depth_reached": self.depth_reached,
            "chunks": self.chunks,
            "stops": list(self.stops),
        }


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecursiveResult:
    """The answer plus the full accounting of what it cost and what was lost."""

    answer: str
    query: str
    calls: int
    tokens: int
    depth_reached: int
    chunks: int
    partial: bool
    stops: tuple[str, ...]
    budget: RecursionBudget
    trace: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "recursive_result",
            "query": self.query,
            "answer": self.answer,
            "calls": self.calls,
            "tokens": self.tokens,
            "depth_reached": self.depth_reached,
            "chunks": self.chunks,
            "partial": self.partial,
            "stops": list(self.stops),
            "budget": self.budget.to_dict(),
            "trace": [jsonable(t) for t in self.trace],
        }


# --------------------------------------------------------------------------
# Planning and execution
# --------------------------------------------------------------------------


_MAX_REDUCE_ROUNDS = 3
"""Reduction passes allowed per level before the answer is left oversized."""


def _level_chars(text_len: int, budget: RecursionBudget) -> int:
    """Chunk size for one level: divide by fanout, but never below leaf size."""
    return max(budget.max_chunk_chars, -(-text_len // budget.fanout))


def plan_recursion(payload: Any, *, budget: RecursionBudget | None = None) -> dict[str, Any]:
    """Price a recursive query *before* running it — no sub-model calls.

    Walks the same splitting logic :func:`recursive_query` would and reports the
    tree it would build: number of leaves (== sub-model calls), depth, tokens
    that would be charged, and whether the budget would bind. This is the call a
    context-constrained model makes to decide whether it can afford a query at
    all.
    """
    budget = budget or RecursionBudget()
    text = as_text(payload)
    leaves: list[int] = []
    depth_reached = 0
    truncated_leaves = 0
    internal_nodes = 0

    def walk(length: int, depth: int) -> None:
        nonlocal depth_reached, truncated_leaves, internal_nodes
        depth_reached = max(depth_reached, depth)
        tokens = -(-length // CHARS_PER_TOKEN) if length else 0
        if tokens <= budget.max_chunk_tokens or depth >= budget.max_depth:
            if length > budget.max_chunk_chars:
                truncated_leaves += 1
                length = budget.max_chunk_chars
            leaves.append(-(-length // CHARS_PER_TOKEN) if length else 0)
            return
        internal_nodes += 1
        step = _level_chars(length, budget)
        full, rest = divmod(length, step)
        for _ in range(full):
            walk(step, depth + 1)
        if rest:
            walk(rest, depth + 1)

    if text:
        walk(len(text), 0)

    total_tokens = sum(leaves)
    # Each internal node may need one reduction pass, and reduction is
    # data-dependent (it fires only if the merged child answers overflow the
    # leaf budget). Leaves are therefore a lower bound and leaves + internal
    # nodes an upper bound on sub-model calls.
    upper_calls = len(leaves) + internal_nodes
    over_calls = upper_calls > budget.max_calls
    over_tokens = total_tokens > budget.max_total_tokens
    return {
        "kind": "recursion_plan",
        "chars": len(text),
        "tokens": estimate_tokens(text),
        "estimated_calls": len(leaves),
        "max_calls_upper_bound": upper_calls,
        "internal_nodes": internal_nodes,
        "estimated_tokens_charged": total_tokens,
        "estimated_depth": depth_reached,
        "truncated_leaves": truncated_leaves,
        "fits_budget": not (over_calls or over_tokens or truncated_leaves),
        "would_stop_on": [
            r
            for r, hit in (
                ("max_calls", over_calls),
                ("max_total_tokens", over_tokens),
                ("max_depth_truncated", bool(truncated_leaves)),
            )
            if hit
        ],
        "note": "estimated_calls counts leaves only; reduction passes are data-dependent",
        "budget": budget.to_dict(),
    }


def recursive_query(
    payload: Any,
    query: str,
    *,
    sub_model: SubModel | None = None,
    budget: RecursionBudget | None = None,
    combine: Combiner | None = None,
    trace_limit: int = 200,
) -> RecursiveResult:
    """Answer ``query`` over a payload too large to read in one pass.

    Decomposes ``payload`` into a ``fanout``-wide tree whose leaves fit
    ``budget.max_chunk_tokens``, calls ``sub_model(query, leaf_text)`` on each
    leaf, and combines child answers upward. When a merged answer is still
    oversized, reduction passes re-query it chunk by chunk — each charged to the
    ledger like any other call.

    The budget bounds what is *sent to the sub-model*, not what comes back:
    ``result.answer`` can exceed the leaf budget when reduction was unaffordable
    or stopped converging. That case always sets ``result.partial`` and appends
    a reason to ``result.stops``, so an oversized answer is never silent.

    ``sub_model`` defaults to :func:`local_sub_model`, which is deterministic
    and offline. Pass your own ``(query, context) -> answer`` closure in
    deployment; nothing here inspects it beyond calling it.

    Exceptions raised by ``sub_model`` propagate unchanged — a broken model
    client is a bug to surface, not a partial answer to paper over. Budget
    limits are the only degradation path, and they always show up in
    ``result.partial`` and ``result.stops``.
    """
    budget = budget or RecursionBudget()
    model = sub_model or local_sub_model
    merge = combine or default_combine
    ledger = BudgetLedger(budget)
    trace: list[dict[str, Any]] = []
    text = as_text(payload)

    def record(entry: dict[str, Any]) -> None:
        if len(trace) < trace_limit:
            trace.append(entry)

    def leaf(body: str, depth: int, kind: str) -> str:
        dropped = 0
        if len(body) > budget.max_chunk_chars:
            dropped = len(body) - budget.max_chunk_chars
            body = body[: budget.max_chunk_chars]
            ledger.note("max_depth_truncated")
        tokens = estimate_tokens(body)
        blocked = ledger.blocked_by(tokens)
        if blocked is not None:
            ledger.note(blocked)
            record({"kind": "skipped", "depth": depth, "reason": blocked, "chars": len(body)})
            return ""
        ledger.spend(tokens, depth)
        answer = model(query, body)
        if not isinstance(answer, str):
            answer = str(answer)
        record(
            {
                "kind": kind,
                "depth": depth,
                "chars": len(body),
                "tokens": tokens,
                "dropped_chars": dropped,
                "answer_chars": len(answer),
            }
        )
        return answer

    def reduce_to_budget(body: str, depth: int) -> str:
        """Shrink an oversized merge by re-querying it, chunk by chunk.

        Returns ``body`` unchanged when the budget cannot afford another pass or
        when a pass stops shrinking it — an oversized honest answer beats a
        truncated one, and ``result.partial`` records that it happened.
        """
        for _ in range(_MAX_REDUCE_ROUNDS):
            if estimate_tokens(body) <= budget.max_chunk_tokens:
                return body
            pieces = chunk(body, max_chars=budget.max_chunk_chars)
            outs: list[str] = []
            stopped = False
            for piece in pieces:
                blocked = ledger.blocked_by(piece.tokens)
                if blocked is not None:
                    ledger.note(blocked)
                    stopped = True
                    break
                outs.append(leaf(piece.text, depth, "reduce"))
            if stopped:
                return body
            candidate = merge(query, outs, depth)
            if not candidate or len(candidate) >= len(body):
                ledger.note("reduce_not_converging")
                return candidate or body
            body = candidate
        if estimate_tokens(body) > budget.max_chunk_tokens:
            ledger.note("reduce_rounds_exhausted")
        return body

    def solve(body: str, depth: int) -> str:
        ledger.depth_reached = max(ledger.depth_reached, depth)
        if estimate_tokens(body) <= budget.max_chunk_tokens or depth >= budget.max_depth:
            return leaf(body, depth, "leaf")
        pieces = chunk(
            body,
            max_chars=_level_chars(len(body), budget),
            overlap=budget.overlap_chars,
        )
        ledger.chunks += len(pieces)
        record({"kind": "split", "depth": depth, "chars": len(body), "chunks": len(pieces)})
        answers: list[str] = []
        for piece in pieces:
            blocked = ledger.blocked_by(0)
            if blocked is not None:
                ledger.note(blocked)
                break
            answers.append(solve(piece.text, depth + 1))
        return reduce_to_budget(merge(query, answers, depth), depth)

    answer = solve(text, 0) if text else ""
    return RecursiveResult(
        answer=answer,
        query=query,
        calls=ledger.calls,
        tokens=ledger.tokens,
        depth_reached=ledger.depth_reached,
        chunks=ledger.chunks,
        partial=bool(ledger.stops),
        stops=tuple(ledger.stops),
        budget=budget,
        trace=tuple(trace),
    )
