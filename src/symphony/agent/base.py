"""Coding-agent backend abstraction.

SPEC 3.1 names the ``Agent Runner`` as one component and SPEC 10 describes its
obligations in terms of *a* coding agent, using Codex as the worked example.
Nothing in SPEC 10's Symphony-side requirements — launch in the per-issue
workspace, render the first turn from the workflow template, continue on the
same live session, forward structured events, never stall — is Codex-specific.

This module is that seam. A backend implements :class:`CodingAgentClient`;
``agent/runner.py`` drives it without knowing which one it holds, exactly as
``trackers/base.py`` does for issue trackers.

Two backends ship:

``codex``
    :mod:`symphony.agent.app_server` — JSON-RPC over stdio to a Codex
    app-server. Protocol strings are unverified against a real binary.

``claude``
    :mod:`symphony.agent.claude` — Claude Code in headless ``stream-json``
    mode. Protocol verified against ``claude 2.1.214``; see
    ``docs/claude-protocol.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from symphony.errors import ConfigValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from symphony.agent.events import AgentEvent
    from symphony.trackers.base import ToolSpec

__all__ = [
    "CodingAgentClient",
    "CodingAgentSession",
    "AgentBackendSpec",
    "register_backend",
    "backend_kinds",
    "build_agent_client",
    "DEFAULT_BACKEND",
]

#: The backend used when ``agent.kind`` is absent. Codex is the default because
#: it is what the specification's worked example describes; a workflow selects
#: Claude Code explicitly.
DEFAULT_BACKEND = "codex"


@runtime_checkable
class CodingAgentSession(Protocol):
    """One live coding-agent conversation, reused across continuation turns.

    ``thread_id`` is the backend's stable conversation identity. SPEC 4.2
    composes the orchestrator-visible ``session_id`` from it and a per-turn id,
    so it MUST stay constant for the lifetime of the session.
    """

    thread_id: str

    async def stop(self) -> None:
        """Release the session. MUST be idempotent (SPEC 16.5 calls it on
        every exit path, including ones that already failed)."""


@runtime_checkable
class CodingAgentClient(Protocol):
    """A coding-agent backend bound to one per-issue workspace.

    Constructed per attempt, because SPEC 9.5 Invariant 1 requires the working
    directory to be the per-issue workspace and both backends bind that at
    construction time.
    """

    async def start_session(self) -> CodingAgentSession:
        """Begin a conversation. Raises a SPEC 10.6 error on failure."""

    async def run_turn(
        self, session: CodingAgentSession, prompt: str, *, title: str | None = None
    ) -> None:
        """Run one turn to termination.

        Returns on success. On failure raises a SPEC 10.6 error — the runner
        converts that into a worker failure and the orchestrator into a retry.
        A turn MUST NOT return while the agent is still working, and MUST NOT
        block indefinitely (SPEC 10.5).
        """

    async def stop(self) -> None:
        """Tear down transport and subprocesses. MUST be idempotent."""


class AgentBackendSpec:
    """Registry entry describing one backend.

    ``config_key`` names the ``WORKFLOW.md`` front-matter block the backend
    owns, so ``agent.kind: claude`` reads its settings from ``claude:`` and
    ``agent.kind: codex`` from ``codex:``. Keeping them in separate blocks means
    a workflow can carry both and switch with one line.
    """

    __slots__ = ("config_key", "description", "factory", "kind")

    def __init__(
        self, kind: str, config_key: str, factory: Any, description: str = ""
    ) -> None:
        self.kind = kind
        self.config_key = config_key
        self.factory = factory
        self.description = description

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AgentBackendSpec(kind={self.kind!r}, config_key={self.config_key!r})"


_BACKENDS: dict[str, AgentBackendSpec] = {}


def register_backend(spec: AgentBackendSpec) -> AgentBackendSpec:
    """Register a backend under its ``kind``."""
    if not spec.kind:
        raise ValueError("agent backend must declare a non-empty kind")
    _BACKENDS[spec.kind] = spec
    return spec


def backend_kinds() -> list[str]:
    """Every supported ``agent.kind``, sorted. Used by validation errors."""
    _ensure_loaded()
    return sorted(_BACKENDS)


def backend_spec(kind: str) -> AgentBackendSpec:
    """Look up a backend, or raise a typed configuration error."""
    _ensure_loaded()
    spec = _BACKENDS.get(kind)
    if spec is None:
        raise ConfigValidationError(
            f"unsupported agent.kind {kind!r}; supported: {', '.join(backend_kinds())}",
            kind=kind,
            supported=backend_kinds(),
        )
    return spec


def build_agent_client(
    kind: str,
    agent_config: Any,
    *,
    workspace: Path,
    tool_specs: Sequence[ToolSpec] = (),
    tool_executor: Any = None,
    on_event: Callable[[AgentEvent], None] | None = None,
    secret_env_names: Sequence[str] = (),
    approval_decider: Any = None,
    **extra: Any,
) -> CodingAgentClient:
    """Construct the backend named by ``kind``.

    Every backend receives the same call, so ``agent/runner.py`` composes one
    way regardless of which is selected. ``agent_config`` is the typed settings
    object for that backend's own front-matter block.
    """
    spec = backend_spec(kind)
    return spec.factory(  # type: ignore[no-any-return]
        agent_config,
        workspace=workspace,
        tool_specs=list(tool_specs),
        tool_executor=tool_executor,
        on_event=on_event,
        secret_env_names=tuple(secret_env_names),
        approval_decider=approval_decider,
        **extra,
    )


def _ensure_loaded() -> None:
    """Import bundled backends for their registration side effect.

    Done lazily rather than at module import so that importing the abstraction
    does not drag in every backend's dependencies, and so a backend that fails
    to import degrades to "unsupported kind" instead of breaking the package.
    """
    if _BACKENDS:
        return
    import importlib

    for module in ("symphony.agent.app_server", "symphony.agent.claude"):
        try:
            importlib.import_module(module)
        except Exception:  # pragma: no cover - a broken backend must not spread
            continue
