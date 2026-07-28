"""Typed configuration layer — SPEC 5.3, 6.1, 6.3, 6.4.

This module turns a parsed :class:`~symphony.models.WorkflowDefinition` into the
frozen, typed :class:`ServiceConfig` the rest of the runtime reads. It owns the
whole of the SPEC 6.4 cheat sheet: every default in that table is a named
module-level constant here so a conformance reader (or an RLM at a REPL) can
diff the table against the code without running anything.

Two spec rules drive most of the surprising behavior:

* **Environment variables never globally override YAML** (SPEC 6.1). They apply
  only where a value explicitly references ``$VAR_NAME``, or where an adapter
  documents a host-side fallback for an omitted provider field. A ``$VAR`` that
  resolves to the empty string is *missing*, not empty (SPEC 5.3.1).
* **``~`` and ``$VAR`` expansion apply only to local filesystem paths**
  (SPEC 6.1). URIs and shell command strings are never rewritten, which is why
  ``codex.command`` and every ``hooks.*`` script survive verbatim.

Validity asymmetry is deliberate and comes straight from the spec: an invalid
``agent.max_turns`` or ``hooks.timeout_ms`` fails configuration validation
(SPEC 5.3.4, 5.3.5), while an invalid ``agent.max_concurrent_agents_by_state``
entry is *ignored* (SPEC 5.3.5) and every other malformed optional value falls
back to its documented default.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from symphony.errors import ConfigValidationError, SymphonyError
from symphony.models import WorkflowDefinition, normalize_label, normalize_state
from symphony.trackers import base as _tracker_base
from symphony.trackers.base import adapter_kinds, build_adapter

__all__ = [
    "DEFAULT_APPROVAL_POLICY",
    "DEFAULT_CODEX_COMMAND",
    "DEFAULT_HOOK_TIMEOUT_MS",
    "DEFAULT_MAX_CONCURRENT_AGENTS",
    "DEFAULT_MAX_RETRY_BACKOFF_MS",
    "DEFAULT_MAX_TURNS",
    # SPEC 6.4 cheat-sheet defaults, exported so conformance checks can read them.
    "DEFAULT_POLL_INTERVAL_MS",
    "DEFAULT_READ_TIMEOUT_MS",
    "DEFAULT_STALL_TIMEOUT_MS",
    "DEFAULT_THREAD_SANDBOX",
    "DEFAULT_TURN_SANDBOX_POLICY",
    "DEFAULT_TURN_TIMEOUT_MS",
    "DEFAULT_WORKSPACE_DIRNAME",
    "CodexConfig",
    "HookConfig",
    "ServiceConfig",
    "build_config",
    "default_workspace_root",
    "expand_value",
    "validate_dispatch_config",
]


# --------------------------------------------------------------------------
# SPEC 6.4 — Core config field defaults (cheat sheet, verbatim)
# --------------------------------------------------------------------------

#: ``polling.interval_ms`` (SPEC 5.3.2 / 6.4).
DEFAULT_POLL_INTERVAL_MS = 30_000

#: ``workspace.root`` default is ``<system-temp>/symphony_workspaces`` (SPEC 5.3.3).
DEFAULT_WORKSPACE_DIRNAME = "symphony_workspaces"

#: ``hooks.timeout_ms`` (SPEC 5.3.4 / 6.4).
DEFAULT_HOOK_TIMEOUT_MS = 60_000

#: ``agent.max_concurrent_agents`` (SPEC 5.3.5 / 6.4).
DEFAULT_MAX_CONCURRENT_AGENTS = 10

#: ``agent.max_turns`` (SPEC 5.3.5 / 6.4).
DEFAULT_MAX_TURNS = 20

#: ``agent.max_retry_backoff_ms`` — 5 minutes (SPEC 5.3.5 / 6.4).
DEFAULT_MAX_RETRY_BACKOFF_MS = 300_000

#: ``codex.command`` (SPEC 5.3.6 / 6.4).
DEFAULT_CODEX_COMMAND = "codex app-server"

#: ``codex.approval_policy`` — spec says "implementation-defined" (SPEC 5.3.6).
#: CONTRACTS.md §5 documents this implementation's policy: trusted environments,
#: auto-approve command-execution and file-change approvals for the session. The
#: Codex ``AskForApproval`` value that expresses "do not ask" is ``never``.
DEFAULT_APPROVAL_POLICY = "never"

#: ``codex.thread_sandbox`` / ``codex.turn_sandbox_policy`` — implementation-defined
#: (SPEC 5.3.6). CONTRACTS.md §5 documents pass-through to Codex under a trusted
#: -environment assumption (SPEC 15.1), so the defaults do not add a second,
#: weaker sandbox on top of the operator's own controls.
DEFAULT_THREAD_SANDBOX = "danger-full-access"
DEFAULT_TURN_SANDBOX_POLICY = "danger-full-access"

#: ``codex.turn_timeout_ms`` — 1 hour (SPEC 5.3.6 / 6.4).
DEFAULT_TURN_TIMEOUT_MS = 3_600_000

#: ``codex.read_timeout_ms`` (SPEC 5.3.6 / 6.4).
DEFAULT_READ_TIMEOUT_MS = 5_000

#: ``codex.stall_timeout_ms`` — 5 minutes; ``<= 0`` disables stall detection
#: (SPEC 5.3.6), which is why this is the one duration field where a
#: non-positive value is *valid* rather than a fallback to the default.
DEFAULT_STALL_TIMEOUT_MS = 300_000

#: Default coding-agent backend when ``agent.kind`` is absent. Codex is the
#: default because it is the specification's worked example; selecting Claude
#: Code is an explicit choice.
DEFAULT_AGENT_KIND = "codex"

_MAX_TCP_PORT = 65_535


def default_workspace_root() -> Path:
    """``<system-temp>/symphony_workspaces`` (SPEC 5.3.3 / 6.4).

    A function rather than a constant because the system temp directory is
    environment-dependent and tests relocate it.
    """
    return Path(tempfile.gettempdir()) / DEFAULT_WORKSPACE_DIRNAME


# --------------------------------------------------------------------------
# SPEC 6.1 — ``~`` and ``$VAR`` expansion for local filesystem path values
# --------------------------------------------------------------------------

# ``$NAME`` and ``${NAME}``. Names follow the POSIX portable character set, which
# is what the spec's ``$VAR_NAME`` notation implies.
_VAR_PATTERN = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def expand_value(value: str, *, base_dir: Path | None = None) -> str:
    """Expand ``~`` and ``$VAR`` in a value intended as a local filesystem path.

    SPEC 6.1 value-coercion semantics. Apply this **only** to values that are
    meant to be local filesystem paths (or to an adapter's own documented
    ``$VAR_NAME`` secret keys). Never apply it to URIs or to arbitrary shell
    command strings — that is why ``codex.command`` and the ``hooks.*`` scripts
    are copied through untouched.

    Order of operations:

    1. ``~`` / ``~user`` expansion, applied to the *authored* value only. A
       leading ``~`` that arrives from an environment variable's value stays
       literal, so a secret is never reinterpreted as a home directory.
    2. A single, non-recursive ``$VAR`` / ``${VAR}`` substitution pass. The
       result of one substitution is never rescanned for further variables.
    3. When ``base_dir`` is given and the result is still relative, join it onto
       ``base_dir`` (SPEC 5.3.3 resolves relative ``workspace.root`` against the
       directory containing ``WORKFLOW.md``).

    A referenced variable that is unset **or resolves to the empty string** is
    *missing* (SPEC 5.3.1) and raises :class:`ConfigValidationError`. The error
    names the variable but never its value (SPEC 15.3).
    """
    if not isinstance(value, str):
        raise ConfigValidationError(
            "expand_value expects a string config value",
            got_type=type(value).__name__,
        )

    expanded = os.path.expanduser(value) if value.startswith("~") else value

    def _substitute(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare")
        resolved = os.environ.get(name, "")
        if not resolved:
            raise ConfigValidationError(
                f"environment variable ${name} referenced by workflow config "
                f"is unset or empty; an empty $VAR is treated as missing",
                variable=name,
            )
        return resolved

    expanded = _VAR_PATTERN.sub(_substitute, expanded)

    if base_dir is not None and expanded and not os.path.isabs(expanded):
        expanded = str(Path(base_dir) / expanded)
    return expanded


# --------------------------------------------------------------------------
# Typed config records (CONTRACTS.md §3 signatures)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookConfig:
    """Workspace hook scripts and their shared timeout (SPEC 5.3.4).

    Scripts are stored verbatim: they are shell source, not paths, so neither
    ``~`` nor ``$VAR`` is expanded here (SPEC 6.1). ``$PWD`` inside a hook must
    still mean "the workspace at run time" (SPEC 15.4).
    """

    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = DEFAULT_HOOK_TIMEOUT_MS


@dataclass(frozen=True, slots=True)
class CodexConfig:
    """Coding-agent launch and timeout settings (SPEC 5.3.6).

    ``approval_policy``, ``thread_sandbox``, and ``turn_sandbox_policy`` are
    pass-through Codex values; SPEC 5.3.6 explicitly declines to enumerate them,
    so this module does not validate them against a hand-maintained enum.
    """

    command: str = DEFAULT_CODEX_COMMAND
    approval_policy: str = DEFAULT_APPROVAL_POLICY
    thread_sandbox: str = DEFAULT_THREAD_SANDBOX
    turn_sandbox_policy: str = DEFAULT_TURN_SANDBOX_POLICY
    turn_timeout_ms: int = DEFAULT_TURN_TIMEOUT_MS
    read_timeout_ms: int = DEFAULT_READ_TIMEOUT_MS
    stall_timeout_ms: int = DEFAULT_STALL_TIMEOUT_MS

    @property
    def stall_detection_enabled(self) -> bool:
        """SPEC 5.3.6: ``stall_timeout_ms <= 0`` disables stall detection."""
        return self.stall_timeout_ms > 0


@lru_cache(maxsize=512)
def _folded(values: tuple[str, ...]) -> frozenset[str]:
    """Case-insensitive comparison set for provider-native state names.

    Cached on the tuple itself so ``is_active``/``is_terminal`` stay O(1) in the
    dispatch loop without storing derived fields on the frozen dataclass.
    """
    return frozenset(normalize_state(value) for value in values)


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Effective, typed configuration for one workflow (SPEC 6.1, 6.4).

    ``active_states`` and ``terminal_states`` keep their **provider-native
    spelling** because adapters query with them (SPEC 11.1); only comparison is
    case-insensitive (SPEC 5.3.1).
    """

    tracker_kind: str
    tracker_provider: dict[str, Any]
    required_labels: tuple[str, ...]
    active_states: tuple[str, ...]
    terminal_states: tuple[str, ...]
    poll_interval_ms: int
    workspace_root: Path
    hooks: HookConfig
    max_concurrent_agents: int
    max_turns: int
    max_retry_backoff_ms: int
    max_concurrent_agents_by_state: dict[str, int]
    codex: CodexConfig
    server_port: int | None
    ssh_hosts: tuple[str, ...]
    max_concurrent_agents_per_host: int | None
    #: Which coding-agent backend runs the work (``codex`` or ``claude``).
    #: SPEC 10 describes the agent boundary using Codex as its worked example
    #: but fixes nothing Codex-specific on Symphony's side, so the backend is
    #: selectable. See ``symphony.agent.base``.
    agent_kind: str = DEFAULT_AGENT_KIND
    #: Typed settings for the *selected* backend, read from the front-matter
    #: block that backend owns (``codex:`` or ``claude:``). Kept separate so one
    #: workflow can carry both and switch with a single line.
    agent_config: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def agent_timeouts(self) -> tuple[int, int, int]:
        """``(turn, read, stall)`` timeouts for the selected backend (SPEC 10.6).

        The orchestrator enforces stall detection and must read it from
        whichever backend is active, not from ``codex`` unconditionally.
        """
        cfg = self.agent_config if self.agent_config is not None else self.codex
        return (
            int(getattr(cfg, "turn_timeout_ms", self.codex.turn_timeout_ms)),
            int(getattr(cfg, "read_timeout_ms", self.codex.read_timeout_ms)),
            int(getattr(cfg, "stall_timeout_ms", self.codex.stall_timeout_ms)),
        )

    def is_active(self, state: str) -> bool:
        """SPEC 5.3.1/8.2: state membership, compared case-insensitively."""
        return normalize_state(state) in _folded(self.active_states)

    def is_terminal(self, state: str) -> bool:
        """SPEC 5.3.1/8.2: state membership, compared case-insensitively."""
        return normalize_state(state) in _folded(self.terminal_states)

    def slot_limit_for_state(self, state: str) -> int:
        """SPEC 8.3: per-state override if present, else the global limit."""
        return self.max_concurrent_agents_by_state.get(
            normalize_state(state), self.max_concurrent_agents
        )


# --------------------------------------------------------------------------
# Coercion helpers (SPEC 6.1 step 5)
# --------------------------------------------------------------------------


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """Read a top-level front-matter section, treating non-maps as absent.

    SPEC 5.3 says unknown keys are ignored for forward compatibility; a
    known key holding the wrong shape is handled the same way rather than
    crashing the whole config.
    """
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _coerce_int(value: Any) -> int | None:
    """Best-effort integer coercion; ``None`` when the value is unusable.

    ``bool`` is rejected explicitly — it is an ``int`` subclass in Python and
    would otherwise become 0/1 silently, matching the treatment in
    ``symphony.trackers.base.coerce_priority``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _brief(value: Any) -> str:
    """Short, non-secret rendering of an offending scalar for error details."""
    text = repr(value)
    return text if len(text) <= 48 else text[:45] + "..."


def _lenient_int(section: dict[str, Any], key: str, default: int, *, minimum: int) -> int:
    """Optional integer that falls back to its documented default when unusable.

    Used for every field SPEC 5.3 gives a default but does *not* mark as failing
    configuration validation.
    """
    value = _coerce_int(section.get(key))
    if value is None or value < minimum:
        return default
    return value


def _fatal_positive_int(section: dict[str, Any], key: str, default: int, *, field_name: str) -> int:
    """Optional integer whose *invalid* values fail configuration validation.

    SPEC 5.3.4 (``hooks.timeout_ms``) and SPEC 5.3.5 (``agent.max_turns``) both
    say invalid values fail validation, unlike every other optional numeric
    field. Absent (or explicit ``null``) still means "use the default".
    """
    if key not in section or section[key] is None:
        return default
    value = _coerce_int(section[key])
    if value is None or value <= 0:
        raise ConfigValidationError(
            f"{field_name} must be a positive integer",
            field=field_name,
            value=_brief(section[key]),
        )
    return value


def _string_list(value: Any) -> tuple[str, ...]:
    """Trimmed, non-empty strings from a YAML list; non-strings are dropped."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _required_labels(value: Any) -> tuple[str, ...]:
    """SPEC 5.3.1 required labels: normalized, blanks **preserved**.

    Matching ignores case and surrounding whitespace, and a blank configured
    label matches no issue. Dropping blanks would silently widen the gate from
    "nothing dispatches" to "everything dispatches", so a blank entry is kept as
    ``""`` and ``Issue.has_labels`` fails on it.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(normalize_label(item) for item in value if isinstance(item, str))


def _optional_script(value: Any) -> str | None:
    """A hook script, kept verbatim; blank or non-string means "no hook"."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _by_state_limits(value: Any) -> dict[str, int]:
    """SPEC 5.3.5 per-state concurrency overrides.

    Keys normalize via ``trim + lowercase``; entries that are non-numeric or
    non-positive are *ignored* rather than fatal. A key that normalizes to the
    empty string is dropped too, since no issue state can ever match it.
    """
    if not isinstance(value, dict):
        return {}
    limits: dict[str, int] = {}
    for raw_key, raw_limit in value.items():
        if not isinstance(raw_key, str):
            continue
        key = normalize_state(raw_key)
        if not key:
            continue
        limit = _coerce_int(raw_limit)
        if limit is None or limit <= 0:
            continue
        limits[key] = limit
    return limits


def _server_port(value: Any) -> int | None:
    """SPEC 13.7 ``server.port``: ``0`` requests an ephemeral port.

    Absent or unusable means the HTTP server extension stays disabled; an
    invalid value must not enable a listener on a port nobody asked for.
    """
    port = _coerce_int(value)
    if port is None or port < 0 or port > _MAX_TCP_PORT:
        return None
    return port


def _workflow_dir(defn: WorkflowDefinition) -> Path:
    """Directory containing the selected ``WORKFLOW.md`` (SPEC 5.3.3, 6.1).

    Falls back to the process working directory when the definition carries no
    source path (in-memory definitions built by tests and the RLM surface).
    """
    if defn.source_path:
        return Path(defn.source_path).expanduser().resolve().parent
    return Path.cwd()


def _workspace_root(section: dict[str, Any], base_dir: Path) -> Path:
    """SPEC 5.3.3: expand, resolve relative to the workflow dir, absolutize."""
    raw = section.get("root")
    if not isinstance(raw, str) or not raw.strip():
        return default_workspace_root().resolve()
    return Path(expand_value(raw.strip(), base_dir=base_dir)).resolve()


def _adapter_state_defaults(kind: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Adapter-documented default active/terminal states (SPEC 5.3.1).

    ``tracker.active_states``/``terminal_states`` are REQUIRED "unless the
    selected adapter profile documents a default", so the defaults are read off
    the registered adapter *class* — never by constructing one, which would run
    adapter side effects during a pure config build. The registry lookup is
    defensive: an adapter module that has not been imported yet simply
    contributes no defaults.
    """
    registry = getattr(_tracker_base, "_REGISTRY", None)
    if not isinstance(registry, dict):
        return (), ()
    cls = registry.get(kind)
    if cls is None:
        return (), ()
    return (
        tuple(getattr(cls, "default_active_states", ()) or ()),
        tuple(getattr(cls, "default_terminal_states", ()) or ()),
    )


# --------------------------------------------------------------------------
# SPEC 6.1 — Configuration resolution pipeline
# --------------------------------------------------------------------------


def build_config(defn: WorkflowDefinition) -> ServiceConfig:
    """Resolve a parsed workflow into effective typed configuration (SPEC 6.1).

    Steps 2-5 of the SPEC 6.1 pipeline (step 1, path selection, belongs to
    ``symphony.workflow.loader``): apply built-in defaults for missing OPTIONAL
    fields, resolve ``$VAR_NAME`` indirection *only* where a value explicitly
    references it, then coerce and validate typed values.

    ``tracker.provider`` is preserved verbatim, including unknown keys
    (SPEC 5.3.1). Core Symphony deliberately does not expand ``$VAR`` inside it:
    only the adapter knows which of its keys are secrets, which are URIs that
    must not be rewritten, and which have documented host-side fallbacks, so the
    adapter performs that resolution when it is constructed (SPEC 6.3).

    Raises :class:`ConfigValidationError` for the two fields SPEC 5.3 marks as
    failing configuration validation (``agent.max_turns``,
    ``hooks.timeout_ms``) and for an unresolvable ``$VAR`` in ``workspace.root``.
    """
    raw: dict[str, Any] = copy.deepcopy(defn.config) if isinstance(defn.config, dict) else {}
    base_dir = _workflow_dir(defn)

    tracker = _section(raw, "tracker")
    polling = _section(raw, "polling")
    workspace = _section(raw, "workspace")
    hooks = _section(raw, "hooks")
    agent = _section(raw, "agent")
    codex = _section(raw, "codex")
    server = _section(raw, "server")
    worker = _section(raw, "worker")

    kind_raw = tracker.get("kind")
    tracker_kind = kind_raw.strip() if isinstance(kind_raw, str) else ""

    provider_raw = tracker.get("provider")
    tracker_provider: dict[str, Any] = (
        copy.deepcopy(provider_raw) if isinstance(provider_raw, dict) else {}
    )

    active_states = _string_list(tracker.get("active_states"))
    terminal_states = _string_list(tracker.get("terminal_states"))
    if not active_states or not terminal_states:
        default_active, default_terminal = _adapter_state_defaults(tracker_kind)
        active_states = active_states or default_active
        terminal_states = terminal_states or default_terminal

    hook_config = HookConfig(
        after_create=_optional_script(hooks.get("after_create")),
        before_run=_optional_script(hooks.get("before_run")),
        after_run=_optional_script(hooks.get("after_run")),
        before_remove=_optional_script(hooks.get("before_remove")),
        timeout_ms=_fatal_positive_int(
            hooks, "timeout_ms", DEFAULT_HOOK_TIMEOUT_MS, field_name="hooks.timeout_ms"
        ),
    )

    # An absent codex.command gets the default; a *present but blank* one is kept
    # blank so the SPEC 6.3 "codex.command is present and non-empty" preflight
    # check stays reachable instead of being silently healed here.
    if "command" not in codex or codex["command"] is None:
        codex_command = DEFAULT_CODEX_COMMAND
    elif isinstance(codex["command"], str):
        codex_command = codex["command"].strip()
    else:
        codex_command = ""

    # The one duration field where a non-positive value is meaningful:
    # SPEC 5.3.6 defines ``<= 0`` as "stall detection disabled".
    stall_timeout_ms = _coerce_int(codex.get("stall_timeout_ms"))
    if stall_timeout_ms is None:
        stall_timeout_ms = DEFAULT_STALL_TIMEOUT_MS

    codex_config = CodexConfig(
        command=codex_command,
        approval_policy=_passthrough(codex.get("approval_policy"), DEFAULT_APPROVAL_POLICY),
        thread_sandbox=_passthrough(codex.get("thread_sandbox"), DEFAULT_THREAD_SANDBOX),
        turn_sandbox_policy=_passthrough(
            codex.get("turn_sandbox_policy"), DEFAULT_TURN_SANDBOX_POLICY
        ),
        turn_timeout_ms=_lenient_int(codex, "turn_timeout_ms", DEFAULT_TURN_TIMEOUT_MS, minimum=1),
        read_timeout_ms=_lenient_int(codex, "read_timeout_ms", DEFAULT_READ_TIMEOUT_MS, minimum=1),
        stall_timeout_ms=stall_timeout_ms,
    )

    # SPEC 5.3 permits extension keys. `agent.kind` selects the coding-agent
    # backend; an unrecognized value is left as-is here so SPEC 6.3 preflight
    # reports it with the supported list rather than silently falling back.
    agent_kind = _passthrough(agent.get("kind"), DEFAULT_AGENT_KIND)

    per_host = _coerce_int(worker.get("max_concurrent_agents_per_host"))
    if per_host is not None and per_host <= 0:
        per_host = None

    return ServiceConfig(
        tracker_kind=tracker_kind,
        tracker_provider=tracker_provider,
        required_labels=_required_labels(tracker.get("required_labels")),
        active_states=active_states,
        terminal_states=terminal_states,
        poll_interval_ms=_lenient_int(polling, "interval_ms", DEFAULT_POLL_INTERVAL_MS, minimum=1),
        workspace_root=_workspace_root(workspace, base_dir),
        hooks=hook_config,
        # SPEC 5.3.5 calls this an "integer", not a positive one, so 0 is honored
        # as an intentional drain switch; only negatives fall back.
        max_concurrent_agents=_lenient_int(
            agent, "max_concurrent_agents", DEFAULT_MAX_CONCURRENT_AGENTS, minimum=0
        ),
        max_turns=_fatal_positive_int(
            agent, "max_turns", DEFAULT_MAX_TURNS, field_name="agent.max_turns"
        ),
        max_retry_backoff_ms=_lenient_int(
            agent, "max_retry_backoff_ms", DEFAULT_MAX_RETRY_BACKOFF_MS, minimum=1
        ),
        max_concurrent_agents_by_state=_by_state_limits(
            agent.get("max_concurrent_agents_by_state")
        ),
        codex=codex_config,
        server_port=_server_port(server.get("port")),
        ssh_hosts=_string_list(worker.get("ssh_hosts")),
        max_concurrent_agents_per_host=per_host,
        agent_kind=agent_kind,
        agent_config=_agent_backend_config(agent_kind, raw, codex_config),
        raw=raw,
    )


def _passthrough(value: Any, default: str) -> str:
    """Codex-owned enum value kept as an opaque string (SPEC 5.3.6)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


# --------------------------------------------------------------------------
# SPEC 6.3 — Dispatch preflight validation
# --------------------------------------------------------------------------


def validate_dispatch_config(cfg: ServiceConfig) -> None:
    """Scheduler preflight run before dispatching new work (SPEC 6.3).

    This is not a full audit of workflow behavior — only what is needed to poll
    and launch workers. SPEC 6.3 lists four checks:

    1. The workflow file can be loaded and parsed. Enforced upstream by
       ``symphony.workflow.loader``; holding a built :class:`ServiceConfig`
       already proves it.
    2. ``tracker.kind`` is present and supported.
    3. The selected adapter accepts ``tracker.provider`` after documented
       defaults and ``$VAR`` resolution — verified by constructing it.
    4. ``codex.command`` is present and non-empty.

    Always raises :class:`ConfigValidationError` (CONTRACTS.md §3), with the
    underlying error's category preserved in ``details`` and the original
    exception kept as ``__cause__`` so no typed information is lost. Nothing
    here logs or embeds a credential value (SPEC 15.3).
    """
    if not cfg.tracker_kind:
        raise ConfigValidationError("tracker.kind is required for dispatch", field="tracker.kind")

    supported = adapter_kinds()
    if cfg.tracker_kind not in supported:
        raise ConfigValidationError(
            f"unsupported tracker.kind {cfg.tracker_kind!r}",
            field="tracker.kind",
            kind=cfg.tracker_kind,
            supported=supported,
        )

    # SPEC 6.3 check 4 generalized: whichever backend is selected must be
    # supported and must have a launchable command. Checked before adapter
    # construction because both are pure, and there is no reason to run adapter
    # setup for a configuration that cannot launch an agent anyway.
    from symphony.agent.base import backend_kinds

    if cfg.agent_kind not in backend_kinds():
        raise ConfigValidationError(
            f"unsupported agent.kind {cfg.agent_kind!r}",
            field="agent.kind",
            kind=cfg.agent_kind,
            supported=backend_kinds(),
        )

    if cfg.agent_kind == "codex":
        if not cfg.codex.command.strip():
            raise ConfigValidationError(
                "codex.command must be a non-empty shell command", field="codex.command"
            )
    else:
        command = str(getattr(cfg.agent_config, "command", "") or "").strip()
        if not command:
            raise ConfigValidationError(
                f"{cfg.agent_kind}.command must be a non-empty command",
                field=f"{cfg.agent_kind}.command",
            )

    try:
        build_adapter(cfg.tracker_kind, copy.deepcopy(cfg.tracker_provider))
    except ConfigValidationError:
        raise
    except SymphonyError as exc:
        raise ConfigValidationError(
            f"tracker.provider rejected by the {cfg.tracker_kind!r} adapter: {exc.message}",
            field="tracker.provider",
            kind=cfg.tracker_kind,
            cause_category=exc.category,
        ) from exc
    except Exception as exc:
        # SPEC 6.2: an invalid config must not take the service down; a per-tick
        # preflight converts any adapter construction failure into a typed,
        # operator-visible error.
        raise ConfigValidationError(
            f"tracker.provider rejected by the {cfg.tracker_kind!r} adapter",
            field="tracker.provider",
            kind=cfg.tracker_kind,
            cause_type=type(exc).__name__,
        ) from exc


def _agent_backend_config(kind: str, raw: Mapping[str, Any], codex_config: CodexConfig) -> Any:
    """Typed settings for the selected coding-agent backend.

    Each backend owns its own front-matter block and its own schema, so a
    third-party backend can ship settings without editing this module. Codex is
    special-cased only because its typed view predates the abstraction and is
    still exposed as ``ServiceConfig.codex`` for compatibility.

    An unknown kind yields ``None``; SPEC 6.3 preflight is what reports it.
    """
    if kind == "codex":
        return codex_config
    try:
        from symphony.agent.base import backend_spec
    except Exception:  # pragma: no cover - agent package always ships
        return None
    try:
        spec = backend_spec(kind)
    except ConfigValidationError:
        return None

    block = raw.get(spec.config_key)
    block = block if isinstance(block, Mapping) else {}
    if kind == "claude":
        from symphony.agent.claude import ClaudeConfig

        return ClaudeConfig.from_mapping(block)
    return dict(block)
