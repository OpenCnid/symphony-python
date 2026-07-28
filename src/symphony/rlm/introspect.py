"""Depth- and size-bounded introspection of a Symphony system.

This module is **not** SPEC-mandated. It is this implementation's extension for
Recursive Language Model (RLM) drivers: a model whose action space is Python
code (Zhang & Khattab, arXiv:2510.04871) needs to learn what the system is made
of *without* reading the source, and it needs to control how many tokens that
answer costs.

Two rules shape everything here.

**Every result is JSON-safe and self-measuring.** Results are plain ``dict``
objects that survive ``json.dumps`` without a ``default=`` hook, and every one
carries ``_meta.chars`` equal to the exact serialized length. A caller can
therefore budget a query before spending it and audit it afterwards.

**Absence is data, not an exception.** Sibling modules are written in parallel
and most are not on disk yet. Asking about a missing component returns
``{"present": false, "reason": "not_present", ...}`` — never an ``ImportError``.

This surface *complements* SPEC 13.3/13.5 rather than duplicating them:
:func:`describe_state` returns a cheap structural index of orchestrator state
(counts, identifiers, phases) for descent, while the dashboard-shaped payload
required by SPEC 13.3 stays the property of
``symphony.observability.snapshot.build_snapshot``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import importlib
import importlib.util
import inspect
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import PurePath
from types import ModuleType
from typing import Any

__all__ = [
    "DEFAULT_MAX_CHARS",
    "MAX_DOC_CHARS",
    "REGISTRY",
    "ComponentSpec",
    "components_for_spec",
    "describe_component",
    "describe_object",
    "describe_state",
    "find_symbol",
    "groups",
    "jsonable",
    "measure_json",
    "refresh",
    "resolve_component",
    "system_map",
]

DEFAULT_MAX_CHARS = 20_000
"""Default serialized-character ceiling for a single introspection result."""

MAX_DOC_CHARS = 200
"""One-line docstring summaries are cut to this many characters."""

MAX_REPR_CHARS = 240
"""Fallback ``repr()`` renderings are cut to this many characters."""

_ROOT_PACKAGE = "symphony"


# --------------------------------------------------------------------------
# Component registry — the CONTRACTS.md ownership map, expressed as data.
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class ComponentSpec:
    """One owned module in the build, with its spec sections expanded.

    ``spec_sections`` is fully enumerated (``16.1``, ``16.2``, ... rather than
    ``16.1-16.4``) so :func:`components_for_spec` is an exact lookup instead of
    a range parse.
    """

    module: str
    group: str
    role: str
    spec_sections: tuple[str, ...] = ()
    immutable: bool = False

    @property
    def expected_path(self) -> str:
        """Repository-relative path this module will occupy when written."""
        return "src/" + self.module.replace(".", "/") + ".py"

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "group": self.group,
            "role": self.role,
            "spec_sections": list(self.spec_sections),
            "immutable": self.immutable,
            "expected_path": self.expected_path,
        }


REGISTRY: tuple[ComponentSpec, ...] = (
    ComponentSpec(
        "symphony.models", "core", "Shared domain vocabulary", ("4.1", "4.2"), immutable=True
    ),
    ComponentSpec(
        "symphony.errors",
        "core",
        "Typed error taxonomy with stable category slugs",
        ("5.5", "10.6", "11.4"),
        immutable=True,
    ),
    ComponentSpec(
        "symphony.workflow.loader", "workflow", "WORKFLOW.md discovery and parsing", ("5.1", "5.2")
    ),
    ComponentSpec(
        "symphony.workflow.config",
        "workflow",
        "Front-matter schema, defaults, dispatch preflight",
        ("5.3", "6.1", "6.3", "6.4"),
    ),
    ComponentSpec("symphony.workflow.watcher", "workflow", "Hot reload of WORKFLOW.md", ("6.2",)),
    ComponentSpec(
        "symphony.workflow.template", "workflow", "Strict prompt rendering", ("5.4", "12")
    ),
    ComponentSpec(
        "symphony.orchestrator.core",
        "orchestrator",
        "Poll loop, claim/dispatch, state ownership",
        ("7", "8.1", "16.1", "16.2", "16.3", "16.4", "16.6"),
    ),
    ComponentSpec(
        "symphony.orchestrator.scheduling",
        "orchestrator",
        "Dispatch ordering, routability, slot limits",
        ("8.2", "8.3"),
    ),
    ComponentSpec(
        "symphony.orchestrator.retry", "orchestrator", "Retry backoff schedule", ("8.4",)
    ),
    ComponentSpec(
        "symphony.orchestrator.reconcile",
        "orchestrator",
        "Tracker reconciliation and cancellation",
        ("8.5", "8.6", "16.3"),
    ),
    ComponentSpec(
        "symphony.workspace.manager",
        "workspace",
        "Per-issue workspace lifecycle",
        ("9.1", "9.2", "9.3"),
    ),
    ComponentSpec(
        "symphony.workspace.safety", "workspace", "Path containment invariants", ("9.5", "15.2")
    ),
    ComponentSpec(
        "symphony.workspace.hooks", "workspace", "Lifecycle hook execution", ("9.4", "15.4")
    ),
    ComponentSpec(
        "symphony.agent.app_server",
        "agent",
        "Codex app-server stdio client",
        ("10.1", "10.2", "10.3", "10.6"),
    ),
    ComponentSpec(
        "symphony.agent.events",
        "agent",
        "Agent event parsing and token accounting",
        ("10.4", "13.5"),
    ),
    ComponentSpec("symphony.agent.approvals", "agent", "Approval policy handling", ("10.5",)),
    ComponentSpec(
        "symphony.agent.runner", "agent", "Per-issue attempt worker and turn loop", ("10.7", "16.5")
    ),
    ComponentSpec(
        "symphony.trackers.base",
        "trackers",
        "TrackerAdapter ABC, registry, coercion helpers",
        ("11.1", "11.2", "11.3", "11.4"),
        immutable=True,
    ),
    ComponentSpec("symphony.trackers.memory", "trackers", "In-process adapter for tests", ("11",)),
    ComponentSpec("symphony.trackers.github", "trackers", "GitHub Projects v2 adapter", ("11",)),
    ComponentSpec("symphony.trackers.linear", "trackers", "Linear adapter", ("11",)),
    ComponentSpec(
        "symphony.observability.logging",
        "observability",
        "Structured key=value logging",
        ("13.1", "13.2"),
    ),
    ComponentSpec(
        "symphony.observability.snapshot",
        "observability",
        "Dashboard-shaped runtime snapshot",
        ("13.3", "13.5"),
    ),
    ComponentSpec(
        "symphony.observability.humanize", "observability", "Humanized event summaries", ("13.6",)
    ),
    ComponentSpec(
        "symphony.observability.status", "observability", "Human-readable status surface", ("13.4",)
    ),
    ComponentSpec("symphony.http.server", "http", "Optional HTTP server", ("13.7",)),
    ComponentSpec("symphony.http.api", "http", "JSON REST API", ("13.7",)),
    ComponentSpec("symphony.http.dashboard", "http", "HTML dashboard", ("13.7",)),
    ComponentSpec("symphony.cli", "cli", "Command-line entry point", ("17.7",)),
    ComponentSpec("symphony.ssh.worker", "ssh", "Remote worker extension", ("A",)),
    ComponentSpec("symphony.rlm.introspect", "rlm", "This module: bounded system introspection"),
    ComponentSpec("symphony.rlm.repl", "rlm", "Addressable REPL environment and primitives"),
    ComponentSpec("symphony.rlm.recursive", "rlm", "Budgeted decompose/query/combine recursion"),
)

_BY_MODULE: dict[str, ComponentSpec] = {c.module: c for c in REGISTRY}


# --------------------------------------------------------------------------
# JSON safety and size measurement
# --------------------------------------------------------------------------


def _repr_of(value: Any) -> str:
    try:
        text = repr(value)
    except Exception as exc:  # a broken __repr__ must not break introspection
        text = f"<unreprable {type(value).__name__}: {type(exc).__name__}>"
    return text if len(text) <= MAX_REPR_CHARS else text[: MAX_REPR_CHARS - 3] + "..."


def jsonable(value: Any, *, depth: int = 8) -> Any:
    """Coerce any Python value into something ``json.dumps`` accepts verbatim.

    Dataclasses become dicts, enums their values, datetimes ISO strings, paths
    strings, sets sorted lists, and anything else a bounded ``repr``. ``depth``
    bounds the walk, so self-referential structures terminate.
    """
    if depth <= 0:
        return _repr_of(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, enum.Enum):
        return jsonable(value.value, depth=depth - 1)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return {"__bytes__": len(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: jsonable(getattr(value, f.name, None), depth=depth - 1)
            for f in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(k): jsonable(v, depth=depth - 1) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return [jsonable(v, depth=depth - 1) for v in sorted(value, key=repr)]
    if isinstance(value, list | tuple):
        return [jsonable(v, depth=depth - 1) for v in value]
    if isinstance(value, ModuleType):
        return f"<module {value.__name__}>"
    if inspect.isclass(value):
        return f"<class {value.__module__}.{value.__qualname__}>"
    if callable(value):
        return f"<callable {getattr(value, '__qualname__', type(value).__name__)}>"
    return _repr_of(value)


def measure_json(payload: Any) -> int:
    """Exact serialized character count of an already-JSON-safe payload."""
    return len(json.dumps(payload, ensure_ascii=False))


# Keys whose values carry addressability and must never be clamped: shortening
# a module name would make the result impossible to descend into.
_PRESERVE_KEYS = frozenset(
    {
        "_meta",
        "counts",
        "depth",
        "group",
        "importable",
        "kind",
        "module",
        "name",
        "present",
        "reason",
        "symbol",
        "type",
    }
)

# Progressively harsher (string cap, list cap) pairs applied until a result fits.
_SHRINK_LADDER: tuple[tuple[int, int], ...] = (
    (400, 40),
    (200, 20),
    (120, 10),
    (80, 5),
    (40, 2),
    (24, 1),
)


def _clamp(value: Any, str_cap: int, list_cap: int) -> Any:
    if isinstance(value, str):
        if len(value) <= str_cap:
            return value
        return value[: max(0, str_cap - 3)] + "..."
    if isinstance(value, list):
        head = [_clamp(v, str_cap, list_cap) for v in value[:list_cap]]
        if len(value) > list_cap:
            head.append({"omitted": len(value) - list_cap})
        return head
    if isinstance(value, dict):
        return {
            k: (v if k in _PRESERVE_KEYS else _clamp(v, str_cap, list_cap))
            for k, v in value.items()
        }
    return value


def _finalize(payload: dict[str, Any], max_chars: int | None) -> dict[str, Any]:
    """JSON-normalize, shrink to ``max_chars``, and stamp an exact ``_meta``.

    The invariant a caller can rely on::

        measure_json(result) == result["_meta"]["chars"]
    """
    result = jsonable(payload)
    if not isinstance(result, dict):  # pragma: no cover - callers always pass dicts
        result = {"kind": "value", "value": result}
    truncated = False
    if max_chars is not None and measure_json(result) > max_chars:
        truncated = True
        for str_cap, list_cap in _SHRINK_LADDER:
            candidate = _clamp(result, str_cap, list_cap)
            result = candidate
            if measure_json(candidate) <= max_chars:
                break
    meta: dict[str, Any] = {"chars": 0, "truncated": truncated, "max_chars": max_chars}
    result["_meta"] = meta
    for _ in range(4):  # converges: only the digit count of `chars` can move
        size = measure_json(result)
        if meta["chars"] == size:
            break
        meta["chars"] = size
    return result


# --------------------------------------------------------------------------
# Import discovery — absence and import failure are both returned as data
# --------------------------------------------------------------------------

_IMPORT_CACHE: dict[str, ModuleType] = {}


def refresh() -> None:
    """Forget cached imports and re-scan the filesystem.

    Sibling modules land while a session is live; call this before re-querying
    a component that was reported absent. Negative results are never cached,
    so this only matters for modules already imported successfully.
    """
    _IMPORT_CACHE.clear()
    importlib.invalidate_caches()


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _load(name: str) -> tuple[ModuleType | None, dict[str, Any] | None]:
    """Return ``(module, problem)`` — exactly one of the two is ``None``."""
    cached = _IMPORT_CACHE.get(name)
    if cached is not None:
        return cached, None
    if not _module_present(name):
        return None, {"present": False, "importable": False, "reason": "not_present"}
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        return None, {
            "present": True,
            "importable": False,
            "reason": "import_failed",
            "error": {"type": type(exc).__name__, "message": _repr_of(str(exc))},
        }
    _IMPORT_CACHE[name] = module
    return module, None


# --------------------------------------------------------------------------
# Symbol reflection
# --------------------------------------------------------------------------


def _summary(obj: Any) -> str | None:
    try:
        doc = inspect.getdoc(obj)
    except Exception:  # pragma: no cover - exotic descriptors
        return None
    if not doc:
        return None
    line = doc.strip().splitlines()[0].strip()
    if not line:
        return None
    return line if len(line) <= MAX_DOC_CHARS else line[: MAX_DOC_CHARS - 3] + "..."


def _signature(obj: Any) -> str | None:
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return None


def _kind(obj: Any) -> str:
    if inspect.isclass(obj):
        if issubclass(obj, enum.Enum):
            return "enum"
        if issubclass(obj, BaseException):
            return "exception"
        if dataclasses.is_dataclass(obj):
            return "dataclass"
        if inspect.isabstract(obj):
            return "abstract_class"
        return "class"
    if inspect.iscoroutinefunction(obj):
        return "async_function"
    if inspect.isgeneratorfunction(obj):
        return "generator_function"
    if inspect.isfunction(obj) or inspect.isbuiltin(obj) or inspect.ismethod(obj):
        return "function"
    if callable(obj):
        return "callable"
    return "constant"


def _public_names(module: ModuleType) -> list[str]:
    declared = getattr(module, "__all__", None)
    if isinstance(declared, list | tuple) and all(isinstance(n, str) for n in declared):
        return [n for n in declared if hasattr(module, n)]
    names: list[str] = []
    for name, obj in vars(module).items():
        if name.startswith("_") or isinstance(obj, ModuleType):
            continue
        owner = getattr(obj, "__module__", None)
        if owner is not None and owner != module.__name__:
            continue
        names.append(name)
    return sorted(names)


def _class_internals(obj: type) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "bases": [b.__name__ for b in obj.__bases__ if b is not object],
    }
    if issubclass(obj, enum.Enum):
        detail["members"] = {m.name: jsonable(m.value, depth=2) for m in obj}
        return detail
    if dataclasses.is_dataclass(obj):
        detail["fields"] = [
            {
                "name": f.name,
                "type": f.type if isinstance(f.type, str) else _repr_of(f.type),
                "required": f.default is dataclasses.MISSING
                and f.default_factory is dataclasses.MISSING,
            }
            for f in dataclasses.fields(obj)
        ]
    methods: list[dict[str, Any]] = []
    for name, member in inspect.getmembers(obj):
        if name.startswith("_"):
            continue
        if not (inspect.isfunction(member) or isinstance(member, property)):
            continue
        if isinstance(member, property):
            methods.append({"symbol": name, "kind": "property", "doc": _summary(member.fget)})
            continue
        if getattr(member, "__qualname__", "").split(".")[0] not in {obj.__name__, ""}:
            continue
        methods.append(
            {
                "symbol": name,
                "kind": _kind(member),
                "signature": _signature(member),
                "doc": _summary(member),
            }
        )
    if methods:
        detail["methods"] = methods
    return detail


def _symbol_entry(name: str, obj: Any, depth: int) -> dict[str, Any]:
    entry: dict[str, Any] = {"symbol": name, "kind": _kind(obj)}
    if depth < 2:
        return entry
    if entry["kind"] == "constant":
        # A module-level constant has no docstring of its own at runtime; its
        # ``__doc__`` belongs to its *type*, which is noise. Show the value.
        entry["type"] = type(obj).__name__
        entry["value"] = jsonable(obj, depth=2)
        return entry
    sig = _signature(obj)
    if sig is not None:
        entry["signature"] = sig
    entry["doc"] = _summary(obj)
    if depth >= 3 and inspect.isclass(obj):
        entry.update(_class_internals(obj))
    return entry


# --------------------------------------------------------------------------
# Public queries
# --------------------------------------------------------------------------


def groups() -> list[str]:
    """Distinct component groups, in registry order."""
    seen: list[str] = []
    for comp in REGISTRY:
        if comp.group not in seen:
            seen.append(comp.group)
    return seen


def resolve_component(name: str) -> ComponentSpec | list[str]:
    """Resolve a loose component name to its :class:`ComponentSpec`.

    Accepts ``"symphony.workflow.loader"``, ``"workflow.loader"``, or
    ``"loader"``. Returns the list of candidate module names when the query is
    ambiguous, so the caller can disambiguate without a second round trip.
    """
    key = name.strip().strip(".")
    if key in _BY_MODULE:
        return _BY_MODULE[key]
    lowered = key.lower()
    suffix = [m for m in _BY_MODULE if m.lower().endswith("." + lowered)]
    if len(suffix) == 1:
        return _BY_MODULE[suffix[0]]
    if suffix:
        return sorted(suffix)
    fuzzy = sorted(m for m in _BY_MODULE if lowered in m.lower())
    if len(fuzzy) == 1:
        return _BY_MODULE[fuzzy[0]]
    return fuzzy


def components_for_spec(section: str) -> list[str]:
    """Module names owning a SPEC section, e.g. ``"13.3"`` or ``"8"``.

    A bare major number matches every subsection: ``"16"`` returns the modules
    owning ``16.1`` through ``16.6``.
    """
    want = section.strip().lstrip("§").rstrip(".")
    if not want:
        return []
    out: list[str] = []
    for comp in REGISTRY:
        for owned in comp.spec_sections:
            if owned == want or owned.startswith(want + "."):
                out.append(comp.module)
                break
    return out


def system_map(
    *,
    depth: int = 0,
    max_chars: int | None = DEFAULT_MAX_CHARS,
    state: Any = None,
    group: str | None = None,
) -> dict[str, Any]:
    """The root view: what this system is made of and what is on disk.

    Depth ladder — each level is a strict superset of the one below, so a caller
    pays only for what it descends into:

    * ``0`` — module name, group, presence flag. A few thousand characters for
      the whole system.
    * ``1`` — adds spec sections, role, module summary line, and public symbol
      *names*.
    * ``2`` — adds every public symbol's kind, signature, and one-line doc.
      Expect this to be clamped by ``max_chars`` for the whole system; prefer
      :func:`describe_component` at depth 2 for a single module.

    ``state`` is optional; when given, a compact runtime summary is included
    (see :func:`describe_state`).
    """
    selected = [c for c in REGISTRY if group is None or c.group == group]
    present = absent = failed = 0
    components: list[dict[str, Any]] = []
    for comp in selected:
        module, problem = _load(comp.module)
        entry: dict[str, Any] = {"module": comp.module, "group": comp.group}
        if module is None:
            assert problem is not None
            entry.update(problem)
            if problem["reason"] == "not_present":
                absent += 1
                entry["expected_path"] = comp.expected_path
            else:
                failed += 1
        else:
            present += 1
            entry["present"] = True
            entry["importable"] = True
            if depth >= 1:
                entry["summary"] = _summary(module)
                names = _public_names(module)
                entry["symbol_count"] = len(names)
                if depth == 1:
                    entry["symbols"] = names
                else:
                    entry["symbols"] = [
                        _symbol_entry(n, getattr(module, n), depth) for n in names
                    ]
        if depth >= 1:
            entry["spec_sections"] = list(comp.spec_sections)
            entry["role"] = comp.role
            if comp.immutable:
                entry["immutable"] = True
        components.append(entry)

    payload: dict[str, Any] = {
        "kind": "system_map",
        "depth": depth,
        "spec": "openai/symphony SPEC.md",
        "version": getattr(importlib.import_module(_ROOT_PACKAGE), "__version__", None),
        "counts": {
            "components": len(components),
            "present": present,
            "absent": absent,
            "import_failed": failed,
        },
        "groups": groups() if group is None else [group],
        "components": components,
    }
    if state is not None:
        payload["runtime"] = describe_state(state, depth=0, max_chars=None)
    payload["next_calls"] = [
        "system_map(depth=1)",
        "describe_component('<module>', depth=2)",
        "components_for_spec('13.3')",
        "find_symbol('token')",
    ]
    return _finalize(payload, max_chars)


def describe_component(
    name: str, *, depth: int = 2, max_chars: int | None = DEFAULT_MAX_CHARS
) -> dict[str, Any]:
    """Describe one component: its spec sections, symbols, and runtime status.

    Depth ladder: ``0`` presence only; ``1`` adds the module summary and symbol
    names; ``2`` adds signatures and one-line docs; ``3`` adds the full module
    docstring plus class internals (dataclass fields, enum members, public
    methods with signatures).

    A component that is not yet on disk returns ``present: false`` with the
    path it will occupy and the spec sections it owes — never an exception.
    """
    resolved = resolve_component(name)
    if isinstance(resolved, list):
        if not resolved:
            # Not a registry name; still allow arbitrary importable modules.
            if _module_present(name):
                resolved = ComponentSpec(name, "external", "not an owned Symphony component")
            else:
                return _finalize(
                    {
                        "kind": "component",
                        "name": name,
                        "present": False,
                        "importable": False,
                        "reason": "unknown_component",
                        "hint": "call system_map(depth=0) for the list of known modules",
                    },
                    max_chars,
                )
        else:
            return _finalize(
                {
                    "kind": "component",
                    "name": name,
                    "reason": "ambiguous",
                    "candidates": resolved,
                },
                max_chars,
            )

    comp = resolved
    payload: dict[str, Any] = {
        "kind": "component",
        "name": comp.module,
        "group": comp.group,
        "role": comp.role,
        "spec_sections": list(comp.spec_sections),
        "immutable": comp.immutable,
        "expected_path": comp.expected_path,
    }
    module, problem = _load(comp.module)
    if module is None:
        assert problem is not None
        payload.update(problem)
        payload["contract"] = "CONTRACTS.md section 3 defines the signatures to expect"
        payload["next_calls"] = [f"refresh(); describe_component({comp.module!r})"]
        return _finalize(payload, max_chars)

    payload["present"] = True
    payload["importable"] = True
    payload["file"] = getattr(module, "__file__", None)
    payload["summary"] = _summary(module)
    if depth >= 3:
        payload["doc"] = inspect.getdoc(module)
    if depth >= 1:
        names = _public_names(module)
        payload["symbol_count"] = len(names)
        payload["symbols"] = [_symbol_entry(n, getattr(module, n), depth) for n in names]
    payload["next_calls"] = [
        f"describe_component({comp.module!r}, depth={min(depth + 1, 3)})",
        f"describe_object({comp.module.rsplit('.', 1)[-1]}.<symbol>)",
    ]
    return _finalize(payload, max_chars)


def describe_object(
    obj: Any, *, depth: int = 2, max_chars: int | None = DEFAULT_MAX_CHARS
) -> dict[str, Any]:
    """Describe an arbitrary live object — class, function, instance, container.

    This is the leaf of a descent: once a caller holds a real object from the
    REPL, this reports its shape without importing anything else.
    """
    payload: dict[str, Any] = {
        "kind": "object",
        "type": type(obj).__name__,
        "module": getattr(obj, "__module__", type(obj).__module__),
        "callable": callable(obj),
        "object_kind": _kind(obj),
    }
    name = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)
    if name:
        payload["name"] = name
    if depth >= 1:
        payload["doc"] = _summary(obj)
        sig = _signature(obj)
        if sig is not None:
            payload["signature"] = sig
    is_container = isinstance(obj, Sequence | Mapping | set | frozenset) and not isinstance(
        obj, str | bytes
    )
    if is_container:
        with contextlib.suppress(TypeError):
            payload["len"] = len(obj)
        if isinstance(obj, Mapping):
            payload["keys"] = [str(k) for k in list(obj)[:50]]
    if depth >= 2:
        if inspect.isclass(obj):
            payload.update(_class_internals(obj))
        elif dataclasses.is_dataclass(obj):
            payload["values"] = jsonable(obj, depth=4)
        elif is_container:
            sample = obj if isinstance(obj, Mapping) else list(obj)[:10]
            payload["sample"] = jsonable(sample, depth=3)
        else:
            payload["repr"] = _repr_of(obj)
    if depth >= 3 and not inspect.isclass(obj):
        payload.update(_class_internals(type(obj)))
    return _finalize(payload, max_chars)


def describe_state(
    state: Any, *, depth: int = 1, max_chars: int | None = DEFAULT_MAX_CHARS
) -> dict[str, Any]:
    """Index live orchestrator state for descent.

    This is deliberately **not** the SPEC 13.3 snapshot. 13.3 defines a
    dashboard payload (running rows with turn counts, retry rows, aggregate
    totals, rate limits) and it is owned by
    ``symphony.observability.snapshot.build_snapshot``. What a recursive model
    needs first is cheaper: how many things exist, what their identifiers are,
    and which phase each is in — enough to decide *which* row to spend context
    on. ``note`` in the result points at the 13.3 surface for the full shape.

    Depth ladder: ``0`` counts and configuration only; ``1`` adds one compact
    row per running and retrying issue; ``2`` adds recent events and last
    errors. Any non-``OrchestratorState`` object falls through to
    :func:`describe_object`.
    """
    if not _looks_like_orchestrator_state(state):
        return describe_object(state, depth=depth, max_chars=max_chars)

    totals = getattr(state, "codex_totals", None)
    payload: dict[str, Any] = {
        "kind": "runtime_state",
        "type": type(state).__name__,
        "counts": {
            "running": len(getattr(state, "running", ())),
            "claimed": len(getattr(state, "claimed", ())),
            "retry_queued": len(getattr(state, "retry_attempts", ())),
            "completed": len(getattr(state, "completed", ())),
        },
        "config": {
            "poll_interval_ms": getattr(state, "poll_interval_ms", None),
            "max_concurrent_agents": getattr(state, "max_concurrent_agents", None),
        },
        "codex_totals": totals.to_dict() if hasattr(totals, "to_dict") else jsonable(totals),
        "rate_limits_present": getattr(state, "codex_rate_limits", None) is not None,
        "note": "SPEC 13.3 dashboard shape: symphony.observability.snapshot.build_snapshot(state)",
    }
    if depth >= 1:
        running = []
        for issue_id, entry in getattr(state, "running", {}).items():
            row: dict[str, Any] = {
                "issue_id": issue_id,
                "identifier": getattr(entry, "identifier", None),
                "phase": jsonable(getattr(entry, "phase", None), depth=2),
                "started_at": jsonable(getattr(entry, "started_at", None), depth=2),
                "turn_count": getattr(getattr(entry, "session", None), "turn_count", None),
                "workspace_path": getattr(entry, "workspace_path", None),
            }
            if depth >= 2:
                row["recent_events"] = jsonable(getattr(entry, "recent_events", []), depth=4)
                row["last_error"] = getattr(entry, "last_error", None)
                row["session"] = jsonable(getattr(entry, "session", None), depth=3)
            running.append(row)
        payload["running"] = running
        payload["retrying"] = [
            {
                "issue_id": issue_id,
                "identifier": getattr(entry, "identifier", None),
                "attempt": getattr(entry, "attempt", None),
                "due_at_ms": getattr(entry, "due_at_ms", None),
                "error": getattr(entry, "error", None) if depth >= 2 else None,
            }
            for issue_id, entry in getattr(state, "retry_attempts", {}).items()
        ]
    payload["next_calls"] = [
        "describe_state(state, depth=2)",
        "describe_object(state.running['<issue_id>'], depth=2)",
    ]
    return _finalize(payload, max_chars)


def _looks_like_orchestrator_state(state: Any) -> bool:
    return all(
        hasattr(state, attr) for attr in ("running", "claimed", "retry_attempts", "completed")
    )


def find_symbol(
    pattern: str,
    *,
    max_results: int = 25,
    regex: bool = True,
    max_chars: int | None = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """Locate a public symbol across every component currently on disk.

    Answers "who exposes something called *token*?" in one call, without the
    caller importing modules or knowing the ownership map. Only present modules
    are scanned; absent ones are counted, not opened.
    """
    try:
        rx = re.compile(pattern if regex else re.escape(pattern), re.IGNORECASE)
    except re.error as exc:
        return _finalize(
            {"kind": "symbol_search", "pattern": pattern, "reason": "bad_pattern",
             "error": str(exc)},
            max_chars,
        )
    matches: list[dict[str, Any]] = []
    scanned = skipped = 0
    for comp in REGISTRY:
        module, _problem = _load(comp.module)
        if module is None:
            skipped += 1
            continue
        scanned += 1
        for name in _public_names(module):
            if not rx.search(name):
                continue
            obj = getattr(module, name)
            matches.append(
                {
                    "module": comp.module,
                    "symbol": name,
                    "kind": _kind(obj),
                    "signature": _signature(obj),
                    "doc": _summary(obj),
                }
            )
    truncated = len(matches) > max_results
    payload = {
        "kind": "symbol_search",
        "pattern": pattern,
        "counts": {
            "matches": len(matches),
            "returned": min(len(matches), max_results),
            "modules_scanned": scanned,
            "modules_absent": skipped,
        },
        "more_available": truncated,
        "matches": matches[:max_results],
    }
    return _finalize(payload, max_chars)
