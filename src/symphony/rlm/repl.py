"""An addressable REPL environment for driving Symphony from a model.

The canonical RLM setup (Zhang & Khattab, arXiv:2510.04871) binds the context
to a *variable* in a Python REPL and lets the root model emit code against it,
rather than pasting the context into a prompt. :class:`ReplEnv` is that
environment, plus the small set of primitives a model actually reaches for
before it decides whether to recurse: measure, peek, window, chunk, grep.

Three properties matter for a machine consumer:

* **Bounded output.** ``stdout`` capture is capped, so one ``print(huge)`` cannot
  blow a context budget.
* **Auditable history.** Every execution is recorded as an :class:`ExecResult`
  with its code, output, last-expression value, and timing; :meth:`ReplEnv.replay`
  re-runs that history in a fresh environment.
* **Containment, not sandboxing.** A failing snippet is captured and returned as
  data — it never propagates out of :meth:`ReplEnv.run`. This is *not* a security
  boundary: executed code has full process privileges, exactly like ``exec``.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from symphony.rlm.introspect import jsonable

__all__ = [
    "BOUNDARIES",
    "CHARS_PER_TOKEN",
    "Chunk",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "ExecResult",
    "ReplEnv",
    "as_text",
    "chunk",
    "estimate_tokens",
    "grep",
    "keys_of",
    "peek",
    "size",
    "window",
]

CHARS_PER_TOKEN = 4
"""Heuristic used to price text in tokens without a tokenizer dependency.

Deliberately crude and deliberately *stable*: budgets must be reproducible
offline, and a factor-of-four estimate is accurate enough to decide whether to
recurse. Real deployments that care about exactness can convert budgets
themselves before constructing a :class:`~symphony.rlm.recursive.RecursionBudget`.
"""

DEFAULT_MAX_OUTPUT_CHARS = 8_000
"""Captured stdout/stderr ceiling per execution."""

BOUNDARIES: tuple[str, ...] = ("paragraph", "line", "sentence", "char")
"""Split levels tried, in order, by :func:`chunk` when ``boundary="auto"``."""

_TRUNCATION_MARK = "\n...[truncated]"


def estimate_tokens(text: str) -> int:
    """Approximate token count of ``text`` (ceiling division by 4 characters)."""
    if not text:
        return 0
    return -(-len(text) // CHARS_PER_TOKEN)


# --------------------------------------------------------------------------
# Primitives: the operations a model performs before deciding to recurse
# --------------------------------------------------------------------------


def as_text(payload: Any) -> str:
    """Render any payload as the text a chunker/grepper can address.

    Strings pass through untouched (so offsets stay meaningful), bytes are
    decoded leniently, and structured data is pretty-printed as JSON via
    :func:`~symphony.rlm.introspect.jsonable` so it is both readable and
    line-addressable.
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes | bytearray):
        return payload.decode("utf-8", errors="replace")
    if payload is None:
        return ""
    coerced = jsonable(payload)
    if isinstance(coerced, str):
        return coerced
    return json.dumps(coerced, ensure_ascii=False, indent=2, sort_keys=False)


def size(payload: Any) -> dict[str, Any]:
    """Price a payload before spending context on it.

    Returns ``type``, ``chars``, ``tokens`` (estimated), ``lines``, and ``items``
    (``len()`` when the object has one). This is the first call a budget-aware
    model makes against anything it has not seen.
    """
    text = as_text(payload)
    items: int | None
    try:
        items = len(payload)
    except TypeError:
        items = None
    return {
        "type": type(payload).__name__,
        "chars": len(text),
        "tokens": estimate_tokens(text),
        "lines": text.count("\n") + 1 if text else 0,
        "items": items,
    }


def peek(payload: Any, n: int = 400, *, tail: bool = False) -> str:
    """Return the first (or, with ``tail=True``, last) ``n`` characters."""
    text = as_text(payload)
    if n <= 0 or len(text) <= n:
        return text
    return text[-n:] if tail else text[:n]


def window(payload: Any, start: int, end: int | None = None) -> Any:
    """Slice a payload by position.

    Mappings slice their *key order*; every other payload is sliced as text.
    Sequences that are not strings keep their element type, so
    ``window(state.running_list, 0, 5)`` stays a list of live objects.
    """
    if isinstance(payload, Mapping):
        keys = list(payload)[start:end]
        return {k: payload[k] for k in keys}
    if isinstance(payload, Sequence) and not isinstance(payload, str | bytes | bytearray):
        return payload[start:end]
    return as_text(payload)[start:end]


def keys_of(payload: Any) -> list[str]:
    """Addressable names inside a payload: mapping keys, or public attributes."""
    if isinstance(payload, Mapping):
        return [str(k) for k in payload]
    slots = getattr(type(payload), "__slots__", None)
    if slots:
        return [s for s in slots if not s.startswith("_")]
    data = getattr(payload, "__dict__", None)
    if isinstance(data, Mapping):
        return [str(k) for k in data if not str(k).startswith("_")]
    return [a for a in dir(payload) if not a.startswith("_")]


@dataclass(frozen=True, slots=True)
class Chunk:
    """One addressable slice of a payload.

    Invariant: ``original_text[chunk.start:chunk.end] == chunk.text``. The slice
    is a real address into the source, so a model can widen or re-read a region
    without re-chunking.
    """

    index: int
    start: int
    end: int
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "chars": self.chars,
            "tokens": self.tokens,
        }


def _units(text: str, level: str) -> list[str]:
    """Split ``text`` into pieces that re-concatenate to it exactly."""
    if not text:
        return []
    if level == "paragraph":
        return [p for p in re.split(r"(?<=\n\n)", text) if p]
    if level == "line":
        return text.splitlines(keepends=True)
    if level == "sentence":
        return [p for p in re.split(r"(?<=[.!?])(?=\s)", text) if p]
    return [text]


def _atoms(text: str, levels: Sequence[str], budget: int) -> list[str]:
    """Split until every piece fits ``budget``, descending the level ladder."""
    if len(text) <= budget:
        return [text]
    for i, level in enumerate(levels):
        pieces = _units(text, level)
        if len(pieces) <= 1:
            continue
        out: list[str] = []
        for piece in pieces:
            if len(piece) <= budget:
                out.append(piece)
            else:
                out.extend(_atoms(piece, levels[i + 1 :], budget))
        return out
    return [text[i : i + budget] for i in range(0, len(text), budget)]


def chunk(
    payload: Any,
    *,
    max_chars: int = 4_000,
    overlap: int = 0,
    boundary: str = "auto",
) -> list[Chunk]:
    """Split a payload on natural boundaries into budget-sized :class:`Chunk` s.

    Tries paragraph breaks, then line breaks, then sentence breaks, then a hard
    slice — descending only when a piece is still too large. Guarantees
    ``len(c.text) <= max_chars`` for every chunk, including the overlap prefix,
    so a caller can size chunks directly against a model's context budget.

    ``boundary`` may name a specific level from :data:`BOUNDARIES` to skip the
    ladder (``"line"`` for logs, ``"char"`` for opaque blobs).
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap < 0:
        raise ValueError("overlap must be non-negative")
    text = as_text(payload)
    if not text:
        return []
    overlap = min(overlap, max_chars - 1)
    body_budget = max(1, max_chars - overlap)
    levels = list(BOUNDARIES) if boundary == "auto" else [boundary, "char"]

    chunks: list[Chunk] = []
    cursor = 0
    buffer: list[str] = []
    buffer_len = 0
    body_start = 0

    def flush() -> None:
        nonlocal buffer, buffer_len, body_start
        if not buffer:
            return
        body_end = body_start + buffer_len
        start = max(0, body_start - overlap) if chunks and overlap else body_start
        chunks.append(Chunk(len(chunks), start, body_end, text[start:body_end]))
        buffer = []
        buffer_len = 0
        body_start = body_end

    for atom in _atoms(text, levels, body_budget):
        if buffer and buffer_len + len(atom) > body_budget:
            flush()
        if not buffer:
            body_start = cursor
        buffer.append(atom)
        buffer_len += len(atom)
        cursor += len(atom)
    flush()
    return chunks


def grep(
    payload: Any,
    pattern: str,
    *,
    ignore_case: bool = True,
    context: int = 0,
    max_hits: int = 50,
    regex: bool = True,
    max_line_chars: int = 400,
) -> list[dict[str, Any]]:
    """Find matching lines with 1-based line numbers and optional context.

    Returns JSON-safe hit records rather than raw text so a model can address a
    result (``window(payload, ...)`` around a hit) instead of re-scanning. A
    result of exactly ``max_hits`` entries means the scan stopped early.
    """
    text = as_text(payload)
    lines = text.splitlines()
    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(pattern if regex else re.escape(pattern), flags)
    hits: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if not rx.search(line):
            continue
        hit: dict[str, Any] = {"line": i + 1, "text": line[:max_line_chars]}
        if context > 0:
            hit["before"] = [x[:max_line_chars] for x in lines[max(0, i - context) : i]]
            hit["after"] = [x[:max_line_chars] for x in lines[i + 1 : i + 1 + context]]
        hits.append(hit)
        if len(hits) >= max_hits:
            break
    return hits


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecResult:
    """One bounded, audited execution.

    ``value`` is the live Python object produced by the snippet's final
    expression (``None`` when the snippet ends in a statement). ``to_dict`` never
    includes it; it returns a JSON-safe ``value_preview`` instead, so an
    execution result can be handed to a sub-model without serialization risk.
    """

    code: str
    ok: bool
    stdout: str = ""
    stderr: str = ""
    value: Any = None
    value_repr: str | None = None
    value_type: str | None = None
    error: dict[str, Any] | None = None
    duration_ms: float = 0.0
    output_truncated: bool = False

    def to_dict(self, *, preview_depth: int = 3) -> dict[str, Any]:
        return {
            "kind": "exec_result",
            "ok": self.ok,
            "code": self.code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "value_type": self.value_type,
            "value_repr": self.value_repr,
            "value_preview": jsonable(self.value, depth=preview_depth),
            "error": self.error,
            "duration_ms": round(self.duration_ms, 3),
            "output_truncated": self.output_truncated,
        }


def _split_trailing_expression(code: str) -> tuple[ast.Module, ast.Expression | None]:
    tree = ast.parse(code, mode="exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tail = ast.Expression(body=tree.body[-1].value)
        ast.copy_location(tail, tree.body[-1])
        head = ast.Module(body=tree.body[:-1], type_ignores=list(tree.type_ignores))
        return head, tail
    return tree, None


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + _TRUNCATION_MARK, True


class ReplEnv:
    """A live namespace holding the system (or any payload) as a variable.

    ``ReplEnv(context=big_payload)`` binds it as ``ctx`` and pre-loads the
    primitives (:func:`size`, :func:`peek`, :func:`window`, :func:`chunk`,
    :func:`grep`, :func:`keys_of`, :func:`as_text`, :func:`estimate_tokens`),
    the introspection functions, and ``recursive_query``. That is the whole
    action space the root model needs::

        env = ReplEnv(context=transcript)
        env.run("size(ctx)").value
        env.run("[c.to_dict() for c in chunk(ctx, max_chars=4000)]").value
    """

    def __init__(
        self,
        *,
        context: Any = None,
        name: str = "ctx",
        extra: Mapping[str, Any] | None = None,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        if not name.isidentifier():
            raise ValueError(f"context variable name must be an identifier, got {name!r}")
        self.context_name = name
        self.max_output_chars = max_output_chars
        self.history: list[ExecResult] = []
        self.namespace: dict[str, Any] = {}
        self._seed(extra or {})
        self.namespace[name] = context

    def _seed(self, extra: Mapping[str, Any]) -> None:
        from symphony.rlm import introspect

        for fn in (
            as_text,
            chunk,
            estimate_tokens,
            grep,
            keys_of,
            peek,
            size,
            window,
        ):
            self.namespace[fn.__name__] = fn
        for fn_name in (
            "components_for_spec",
            "describe_component",
            "describe_object",
            "describe_state",
            "find_symbol",
            "jsonable",
            "measure_json",
            "refresh",
            "system_map",
        ):
            self.namespace[fn_name] = getattr(introspect, fn_name)
        # Deferred: recursive imports this module, so it must be resolved at
        # construction time rather than at import time.
        try:
            from symphony.rlm import recursive
        except ImportError:  # pragma: no cover - only if the file is removed
            pass
        else:
            for fn_name in ("RecursionBudget", "plan_recursion", "recursive_query"):
                self.namespace[fn_name] = getattr(recursive, fn_name)
        self.namespace.update(extra)

    # -- binding ---------------------------------------------------------

    def bind(self, name: str, value: Any) -> None:
        """Add or replace a variable in the environment."""
        if not name.isidentifier():
            raise ValueError(f"binding name must be an identifier, got {name!r}")
        self.namespace[name] = value

    @property
    def context(self) -> Any:
        """The payload bound to the context variable."""
        return self.namespace.get(self.context_name)

    def bindings(self) -> list[str]:
        """Names currently addressable in the environment, sorted."""
        return sorted(k for k in self.namespace if not k.startswith("__"))

    # -- execution -------------------------------------------------------

    def run(self, code: str, *, max_output_chars: int | None = None) -> ExecResult:
        """Execute ``code``, capturing output, the last expression, and failures.

        The snippet's trailing expression (if any) is evaluated separately and
        returned as ``result.value`` — the same affordance a real REPL gives.
        Failures become ``result.error`` with a bounded traceback; they are never
        raised, so a driving model can inspect and retry without the host dying.
        ``KeyboardInterrupt`` is the sole exception: it always propagates.
        """
        limit = self.max_output_chars if max_output_chars is None else max_output_chars
        out, err = io.StringIO(), io.StringIO()
        value: Any = None
        error: dict[str, Any] | None = None
        ok = True
        started = time.perf_counter()
        try:
            head, tail = _split_trailing_expression(code)
            head_obj = compile(head, "<rlm>", "exec")
            tail_obj = compile(tail, "<rlm>", "eval") if tail is not None else None
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                # Same dict for globals and locals gives module-level scoping,
                # so comprehensions can see names defined by earlier statements.
                exec(head_obj, self.namespace, self.namespace)
                if tail_obj is not None:
                    value = eval(tail_obj, self.namespace, self.namespace)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # SystemExit from a snippet must not exit
            ok = False
            error = {
                "type": type(exc).__name__,
                "message": str(exc)[:1_000],
                "traceback": "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )[-2_000:],
            }
        duration_ms = (time.perf_counter() - started) * 1_000.0

        stdout, cut_out = _cap(out.getvalue(), limit)
        stderr, cut_err = _cap(err.getvalue(), limit)
        result = ExecResult(
            code=code,
            ok=ok,
            stdout=stdout,
            stderr=stderr,
            value=value,
            value_repr=None if value is None else _short_repr(value),
            value_type=None if value is None else type(value).__name__,
            error=error,
            duration_ms=duration_ms,
            output_truncated=cut_out or cut_err,
        )
        self.history.append(result)
        return result

    # -- audit -----------------------------------------------------------

    @property
    def last(self) -> ExecResult | None:
        return self.history[-1] if self.history else None

    def codes(self) -> list[str]:
        """The snippets executed so far, in order."""
        return [r.code for r in self.history]

    def replay(self, *, context: Any = None, extra: Mapping[str, Any] | None = None) -> ReplEnv:
        """Re-run this environment's history in a fresh :class:`ReplEnv`.

        Returns the new environment. Use it to reproduce a session against a
        different payload (pass ``context``) or to verify that a derived result
        is a pure function of the recorded code.
        """
        clone = ReplEnv(
            context=self.context if context is None else context,
            name=self.context_name,
            extra=extra,
            max_output_chars=self.max_output_chars,
        )
        for code in self.codes():
            clone.run(code)
        return clone

    def transcript(self, *, max_chars: int = 4_000) -> str:
        """A compact, human- and model-readable log of the session."""
        parts: list[str] = []
        for i, r in enumerate(self.history):
            marker = "ok" if r.ok else "ERR"
            parts.append(f">>> [{i}] {marker} {r.code}")
            if r.stdout:
                parts.append(r.stdout.rstrip("\n"))
            if r.value_repr:
                parts.append(r.value_repr)
            if r.error:
                parts.append(f"{r.error['type']}: {r.error['message']}")
        text = "\n".join(parts)
        return _cap(text, max_chars)[0]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe summary of the environment for a model to read."""
        return {
            "kind": "repl_env",
            "context_name": self.context_name,
            "context": size(self.context),
            "bindings": self.bindings(),
            "history_len": len(self.history),
            "last": self.last.to_dict() if self.last else None,
        }


def _short_repr(value: Any, limit: int = 400) -> str:
    try:
        text = repr(value)
    except Exception as exc:  # pragma: no cover - broken __repr__
        return f"<unreprable {type(value).__name__}: {type(exc).__name__}>"
    return text if len(text) <= limit else text[: limit - 3] + "..."
