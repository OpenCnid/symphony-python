"""Claude Code backend — SPEC 10 via headless ``stream-json``.

Claude Code has no app-server, so this driver speaks the CLI's newline-delimited
JSON event stream directly rather than JSON-RPC. The wire contract is recorded
in ``docs/claude-protocol.md`` and was **verified by running the binary**
(``claude 2.1.214``), not read off documentation — which is the material
difference between this backend and the Codex one.

Two structural differences from the Codex path follow from the CLI's design and
are load-bearing:

**One process per turn.** ``--print`` runs a turn and exits. Continuation turns
therefore start a new process with ``--resume <session_id>`` rather than writing
to a live stdin. SPEC 10.3 says the subprocess *SHOULD* stay alive across
continuation turns; that is a recommendation about not resending the task
prompt and not losing thread state, and resuming a persisted session satisfies
both. It also buys something the live-thread model cannot: a session survives an
orchestrator restart, so a continuation retry after a crash resumes the
conversation instead of starting cold (compare SPEC 14.3).

**No login shell.** The Codex path is required by SPEC 10.1 to launch via
``bash -lc``, which sources a login profile and can restore a stripped
credential (see ``docs/SECURITY.md`` §12.2). Claude Code is executed directly,
so the environment this process builds is the environment the child gets.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from symphony.agent.base import AgentBackendSpec, register_backend
from symphony.agent.events import AgentEvent
from symphony.errors import (
    CodexNotFound,
    InvalidWorkspaceCwd,
    PortExit,
    ResponseTimeout,
    TurnCancelled,
    TurnFailed,
    TurnTimeout,
)
from symphony.models import session_id as compose_session_id

__all__ = [
    "ClaudeConfig",
    "ClaudeSession",
    "ClaudeCodeClient",
    "PERMISSION_MODES",
    "DEFAULT_PERMISSION_MODE",
    "resolve_claude",
]

#: Accepted ``--permission-mode`` values, from ``claude --help`` on 2.1.214.
#: ``manual`` is deliberately excluded from the *documented* postures: how an
#: interactive prompt surfaces in stream-json is unverified, and SPEC 10.5
#: forbids a run that stalls waiting for one. It is still accepted here so an
#: operator who has verified the behavior can select it knowingly.
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")

#: SPEC 15.1 requires a stated posture. This implementation targets trusted
#: environments, matching the Codex backend's documented auto-approval.
DEFAULT_PERMISSION_MODE = "bypassPermissions"

#: Claude Code returns this when the orchestrator terminates it (SIGTERM).
_SIGTERM_EXIT = 143

_MAX_LINE_BYTES = 10 * 1024 * 1024


# --------------------------------------------------------------------------
# Configuration — the backend owns its own front-matter block (``claude:``)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaudeConfig:
    """Typed view of the ``claude:`` front-matter block.

    Mirrors ``CodexConfig`` for the three timeout fields the orchestrator
    depends on (SPEC 10.6), and adds the controls the CLI exposes that Codex
    has no equivalent for.
    """

    command: str = "claude"
    model: str | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    max_turns: int | None = None
    max_budget_usd: float | None = None
    append_system_prompt: str | None = None
    system_prompt: str | None = None
    add_dirs: tuple[str, ...] = ()
    mcp_config: tuple[str, ...] = ()
    settings: str | None = None
    agents: str | None = None
    effort: str | None = None
    bare: bool = False
    session_persistence: bool = True
    fork_session: bool = False
    deterministic_session_id: bool = True
    extra_args: tuple[str, ...] = ()
    # -- shared with CodexConfig (SPEC 10.6) --------------------------------
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 60_000
    stall_timeout_ms: int = 300_000

    @property
    def stall_detection_enabled(self) -> bool:
        """SPEC 5.3.6: a non-positive stall timeout disables the check."""
        return self.stall_timeout_ms > 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> ClaudeConfig:
        """Build from the raw ``claude:`` block, applying defaults.

        Unknown keys are ignored for forward compatibility (SPEC 5.3), and
        invalid values fall back to the default rather than failing the whole
        workflow — except where SPEC 6.3 preflight is expected to catch them.
        """
        raw = raw or {}
        return cls(
            command=_str(raw.get("command"), "claude") or "claude",
            model=_str(raw.get("model"), None),
            permission_mode=_permission_mode(raw.get("permission_mode")),
            allowed_tools=_str_tuple(raw.get("allowed_tools")),
            disallowed_tools=_str_tuple(raw.get("disallowed_tools")),
            max_turns=_pos_int(raw.get("max_turns")),
            max_budget_usd=_pos_float(raw.get("max_budget_usd")),
            append_system_prompt=_str(raw.get("append_system_prompt"), None),
            system_prompt=_str(raw.get("system_prompt"), None),
            add_dirs=_str_tuple(raw.get("add_dirs")),
            mcp_config=_str_tuple(raw.get("mcp_config")),
            settings=_str(raw.get("settings"), None),
            agents=_str(raw.get("agents"), None),
            effort=_str(raw.get("effort"), None),
            bare=bool(raw.get("bare", False)),
            session_persistence=bool(raw.get("session_persistence", True)),
            fork_session=bool(raw.get("fork_session", False)),
            deterministic_session_id=bool(raw.get("deterministic_session_id", True)),
            extra_args=_str_tuple(raw.get("extra_args")),
            turn_timeout_ms=_int(raw.get("turn_timeout_ms"), 3_600_000),
            read_timeout_ms=_int(raw.get("read_timeout_ms"), 60_000),
            stall_timeout_ms=_int(raw.get("stall_timeout_ms"), 300_000),
        )


def _str(value: Any, default: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable):
        return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())
    return ()


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _pos_int(value: Any) -> int | None:
    n = _int(value, 0)
    return n if n > 0 else None


def _pos_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if float(value) > 0 else None


def _permission_mode(value: Any) -> str:
    if isinstance(value, str) and value in PERMISSION_MODES:
        return value
    return DEFAULT_PERMISSION_MODE


# --------------------------------------------------------------------------
# Executable resolution
# --------------------------------------------------------------------------


def resolve_claude(command: str = "claude") -> str:
    """Resolve the Claude Code executable through PATH.

    A bare name is resolved to an absolute path for the same reason the Codex
    backend resolves ``bash``: on Windows ``CreateProcess`` searches system
    directories before PATH, and ``shutil.which`` honours PATHEXT so a ``.cmd``
    shim is found correctly.
    """
    head = command.split()[0] if command.strip() else "claude"
    if os.path.isabs(head) and os.path.exists(head):
        return head
    found = shutil.which(head)
    if found is None:
        raise CodexNotFound(
            f"coding agent executable {head!r} not found on PATH",
            command=command,
        )
    return found


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ClaudeSession:
    """One Claude Code conversation, resumed across continuation turns.

    ``thread_id`` is the Claude session id — pre-assigned when
    ``deterministic_session_id`` is set, otherwise learned from the first
    ``system/init`` event.

    Token and cost counters accumulate across turns. Claude reports **per-turn**
    usage on each ``result``, while SPEC 13.5 wants absolute thread totals with
    delta tracking upstream; summing here is what converts one into the other.
    """

    thread_id: str
    started: bool = False
    turn_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    last_rate_limits: dict[str, Any] | None = None
    permission_denials: list[Any] = field(default_factory=list)
    _stopped: bool = False

    @property
    def total_tokens(self) -> int:
        """Billable-context total. Cache reads are counted because they are
        input the model processed, and excluding them understates the thread."""
        return self.input_tokens + self.output_tokens + self.cache_read_tokens

    def absolute_usage(self) -> dict[str, int]:
        """Cumulative totals in the shape ``events.extract_token_totals`` accepts."""
        return {
            "input_tokens": self.input_tokens + self.cache_read_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }

    def credit(self, usage: Mapping[str, Any], cost: float | None) -> None:
        """Add one turn's reported usage to the session totals."""
        self.input_tokens += _safe_int(usage.get("input_tokens"))
        self.output_tokens += _safe_int(usage.get("output_tokens"))
        self.cache_read_tokens += _safe_int(usage.get("cache_read_input_tokens"))
        self.cache_creation_tokens += _safe_int(usage.get("cache_creation_input_tokens"))
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
            self.total_cost_usd += float(cost)

    async def stop(self) -> None:
        """Idempotent. The process already exited; nothing to tear down."""
        self._stopped = True


def _safe_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def deterministic_session_uuid(identifier: str) -> str:
    """Stable UUID for an issue identifier.

    ``--session-id`` requires a valid UUID. Deriving it from the issue
    identifier means the same issue resumes the same conversation across worker
    restarts, which is strictly more than SPEC 14.3 promises.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"symphony:claude:{identifier}"))


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class ClaudeCodeClient:
    """Drives Claude Code in headless ``stream-json`` mode for one workspace."""

    def __init__(
        self,
        cfg: ClaudeConfig,
        *,
        workspace: Path,
        tool_specs: Sequence[Any] = (),
        tool_executor: Any = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        secret_env_names: Sequence[str] = (),
        approval_decider: Any = None,
        issue_identifier: str | None = None,
        spawn: Any = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.workspace = Path(workspace)
        self.tool_specs = list(tool_specs)
        self._tool_executor = tool_executor
        self._on_event = on_event
        self._secret_env_names = tuple(secret_env_names)
        self._approval_decider = approval_decider
        self._issue_identifier = issue_identifier
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._now = now or (lambda: datetime.now(UTC))
        self._session: ClaudeSession | None = None
        self._proc: Any = None
        self._stderr_tail: list[str] = []
        self._bridge: Any = None
        self._mcp_config_path: Path | None = None

    # -- introspection (kept flat for the RLM surface) ---------------------

    @property
    def session(self) -> ClaudeSession | None:
        return self._session

    @property
    def pid(self) -> int | None:
        return None if self._proc is None else self._proc.pid

    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)

    # -- lifecycle ---------------------------------------------------------

    async def start_session(self) -> ClaudeSession:
        """Create the session record. No process starts until the first turn.

        ``--print`` couples process lifetime to a single turn, so there is no
        handshake to perform up front. Deferring the launch means a workspace or
        prompt failure costs nothing, and it keeps SPEC 16.5's ordering intact:
        the first process starts when the first turn does.
        """
        self._assert_workspace()
        await self._start_tool_bridge()
        if self.cfg.deterministic_session_id and self._issue_identifier:
            thread_id = deterministic_session_uuid(self._issue_identifier)
        else:
            thread_id = str(uuid.uuid4())
        self._session = ClaudeSession(thread_id=thread_id)
        return self._session

    async def _start_tool_bridge(self) -> None:
        """Expose the adapter's provider-native tools over MCP (SPEC 10.5, 11.5).

        Only when the adapter actually ships tools *and* an executor was
        supplied. The bridge runs in this process so the tracker credential
        never enters the agent's process tree; see
        :mod:`symphony.agent.mcp_bridge` for why stdio would not do.

        A bridge that fails to start is logged as a degraded capability rather
        than a failed attempt: the agent can still do the work, it just cannot
        update the ticket itself, and failing the run would be worse than
        running without the extension (SPEC 10.5 makes it OPTIONAL).
        """
        if self._bridge is not None or not self.tool_specs or self._tool_executor is None:
            return
        from symphony.agent.mcp_bridge import TrackerToolBridge

        bridge = TrackerToolBridge(self.tool_specs, self._tool_executor)
        try:
            await bridge.start()
        except Exception as exc:
            self._emit(
                "notification",
                {"summary": "tracker tool bridge unavailable", "error": f"{type(exc).__name__}"},
            )
            return

        config_path = self.workspace / ".symphony-mcp.json"
        config_path.write_text(json.dumps(bridge.mcp_config()), encoding="utf-8")
        self._bridge = bridge
        self._mcp_config_path = config_path

    async def _stop_tool_bridge(self) -> None:
        bridge, path = self._bridge, self._mcp_config_path
        self._bridge = self._mcp_config_path = None
        if bridge is not None:
            await bridge.stop()
        if path is not None:
            # The file carries the bearer token, so it does not outlive the run.
            with suppress(OSError):
                path.unlink()

    async def run_turn(
        self, session: ClaudeSession, prompt: str, *, title: str | None = None
    ) -> None:
        """Run one turn to termination (SPEC 10.3).

        A cold client holding a *deterministic* session id cannot know whether
        that conversation already exists — the id is derived from the issue
        identifier, so an orchestrator restart produces the same id as the
        original run. Claude Code rejects a reused ``--session-id`` outright::

            Error: Session ID <uuid> is already in use.

        so the first turn self-heals: on that specific failure it flips to
        ``--resume`` and retries once. That is what makes a session actually
        survive a restart rather than merely appearing to (SPEC 14.3 promises
        no session recovery; this exceeds it, but only if the reconnect works).
        """
        self._assert_workspace()
        session.turn_count += 1
        try:
            await self._attempt_turn(session, prompt, title)
        except PortExit:
            if session.started or not self._session_id_in_use():
                raise
            session.started = True
            self._stderr_tail.clear()
            await self._attempt_turn(session, prompt, title)

    def _session_id_in_use(self) -> bool:
        """Did the child reject a pre-assigned session id as already existing?

        Matched on the message text because Claude Code emits it as plain
        stderr rather than a protocol event, so there is no code to key on.
        A wording change makes this return False, which degrades to the old
        behavior (a failed attempt and an ordinary retry) rather than to
        something unsafe.
        """
        return any("already in use" in line.lower() for line in self._stderr_tail)

    async def _attempt_turn(
        self, session: ClaudeSession, prompt: str, title: str | None
    ) -> None:
        argv = self.build_argv(session, prompt, title=title)
        env = self._child_env()

        try:
            proc = await self._spawn(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                env=env,
                limit=_MAX_LINE_BYTES,
            )
        except FileNotFoundError as exc:
            raise CodexNotFound(
                f"could not launch coding agent: {argv[0]!r}", command=self.cfg.command
            ) from exc
        self._proc = proc

        stderr_task = asyncio.ensure_future(self._drain_stderr(proc))
        try:
            await self._consume(proc, session, title)
        finally:
            # Let stderr finish draining before tearing down. Cancelling it
            # immediately races the diagnostics against the failure that needs
            # them -- `_session_id_in_use` reads this buffer, and an empty
            # buffer would silently disable the restart recovery above.
            with suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(stderr_task), timeout=2)
            stderr_task.cancel()
            await self._reap(proc)
            self._proc = None

    async def stop(self) -> None:
        """Terminate any live process and the tool bridge. Idempotent (SPEC 16.5)."""
        await self._stop_tool_bridge()
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        with _suppress():
            proc.terminate()
        with _suppress():
            await asyncio.wait_for(proc.wait(), timeout=5)
        if proc.returncode is None:  # pragma: no cover - stubborn child
            with _suppress():
                proc.kill()

    # -- argv --------------------------------------------------------------

    def build_argv(
        self, session: ClaudeSession, prompt: str, *, title: str | None = None
    ) -> list[str]:
        """Compose the CLI invocation for one turn.

        The first turn pre-assigns the session id; later turns resume it. That
        is how a continuation turn reaches the existing conversation without
        resending the task prompt (SPEC 7.1).
        """
        cfg = self.cfg
        argv: list[str] = [resolve_claude(cfg.command)]
        argv += ["--print", prompt]
        argv += ["--output-format", "stream-json", "--verbose"]

        # SPEC 10.2: include issue-identifying metadata where the targeted
        # protocol supports a session title. Only on the opening turn — the
        # name belongs to the session, and a resume would just re-set it.
        if title and not session.started:
            argv += ["--name", title]

        if session.started:
            argv += ["--resume", session.thread_id]
            if cfg.fork_session:
                argv.append("--fork-session")
        else:
            argv += ["--session-id", session.thread_id]

        argv += ["--permission-mode", cfg.permission_mode]
        if cfg.model:
            argv += ["--model", cfg.model]

        # Provider-native tracker tools (SPEC 10.5, 11.5), when the bridge is up.
        # They are appended to the configured allow-list rather than replacing
        # it, and only when the workflow set one at all -- an empty
        # `allowed_tools` means "no restriction", and emitting the flag would
        # silently narrow the agent to *only* the tracker tools.
        bridge_tools: list[str] = []
        if self._bridge is not None and self._mcp_config_path is not None:
            argv += ["--mcp-config", str(self._mcp_config_path)]
            bridge_tools = list(self._bridge.allowed_tool_patterns())

        if cfg.allowed_tools:
            argv += ["--allowedTools", ",".join([*cfg.allowed_tools, *bridge_tools])]
        if cfg.disallowed_tools:
            argv += ["--disallowedTools", ",".join(cfg.disallowed_tools)]
        if cfg.max_turns is not None:
            argv += ["--max-turns", str(cfg.max_turns)]
        if cfg.max_budget_usd is not None:
            argv += ["--max-budget-usd", str(cfg.max_budget_usd)]
        if cfg.append_system_prompt:
            argv += ["--append-system-prompt", cfg.append_system_prompt]
        if cfg.system_prompt:
            argv += ["--system-prompt", cfg.system_prompt]
        if cfg.effort:
            argv += ["--effort", cfg.effort]
        for extra_dir in cfg.add_dirs:
            argv += ["--add-dir", extra_dir]
        for mcp in cfg.mcp_config:
            argv += ["--mcp-config", mcp]
        if cfg.settings:
            argv += ["--settings", cfg.settings]
        if cfg.agents:
            argv += ["--agents", cfg.agents]
        if cfg.bare:
            argv.append("--bare")
        if not cfg.session_persistence:
            argv.append("--no-session-persistence")
        argv += list(cfg.extra_args)
        return argv

    # -- safety ------------------------------------------------------------

    def _assert_workspace(self) -> None:
        """SPEC 9.5 Invariant 1 / 15.2: the agent runs in the issue workspace."""
        path = self.workspace
        if not path.is_absolute() or not path.is_dir():
            raise InvalidWorkspaceCwd(
                "coding-agent working directory must be an existing absolute path",
                workspace=str(path),
            )

    def _child_env(self) -> dict[str, str]:
        """SPEC 15.3: declared tracker credentials are not inherited.

        Claude Code is executed directly rather than through a login shell, so
        unlike the Codex path there is no profile sourcing to undo this.
        """
        env = dict(os.environ)
        for name in self._secret_env_names:
            env.pop(name, None)
        return env

    # -- stream consumption ------------------------------------------------

    async def _consume(self, proc: Any, session: ClaudeSession, title: str | None) -> None:
        """Read the NDJSON stream until the ``result`` event or a timeout."""
        stdout = proc.stdout
        assert stdout is not None
        # Two budgets, and the switch between them is keyed on `session.started`
        # — the same condition the error branch reports on. Switching on "any
        # line received" instead would desynchronize the two: `claude` can print
        # an update banner or a login warning before `system/init`, and a hang
        # after such a line would then wait the full turn budget (an hour by
        # default) while raising ResponseTimeout blaming read_timeout_ms.
        # Startup is not over until init arrives.
        finished = False

        while True:
            deadline_ms = (
                self.cfg.turn_timeout_ms if session.started else self.cfg.read_timeout_ms
            )
            try:
                raw = await asyncio.wait_for(stdout.readline(), timeout=deadline_ms / 1000)
            except TimeoutError as exc:
                await self.stop()
                if not session.started:
                    raise ResponseTimeout(
                        "coding agent did not start a session before read_timeout_ms",
                        timeout_ms=self.cfg.read_timeout_ms,
                    ) from exc
                raise TurnTimeout(
                    "coding agent stream was silent past turn_timeout_ms",
                    timeout_ms=self.cfg.turn_timeout_ms,
                ) from exc

            if not raw:
                break

            event = self._decode(raw)
            if event is None:
                continue
            if self._handle(event, session, title):
                finished = True
                break

        if not finished:
            await self._reap(proc)
            code = proc.returncode
            if code == _SIGTERM_EXIT:
                raise TurnCancelled("coding agent was terminated", returncode=code)
            raise PortExit(
                "coding agent exited before completing the turn",
                returncode=code,
                stderr_tail=self.stderr_tail()[-5:],
            )

    def _decode(self, raw: bytes) -> dict[str, Any] | None:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return None
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            # `claude` can print non-protocol lines (warnings, startup errors).
            # Surface and resync rather than failing the turn -- and keep the
            # text in the diagnostics buffer, because fatal messages such as
            # "Session ID ... is already in use." arrive this way rather than as
            # a protocol event, and the restart recovery needs to read them.
            self._stderr_tail.append(text)
            del self._stderr_tail[:-50]
            self._emit("malformed", {"line": text[:400]})
            return None
        return decoded if isinstance(decoded, dict) else None

    def _handle(self, event: Mapping[str, Any], session: ClaudeSession, title: str | None) -> bool:
        """Translate one CLI event. Returns True when the turn has terminated."""
        kind = event.get("type")

        if kind == "system":
            return self._handle_system(event, session, title)
        if kind == "assistant":
            self._handle_assistant(event)
            return False
        if kind == "user":
            self._emit("notification", {"summary": "tool result", **_tool_result(event)})
            return False
        if kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, Mapping):
                session.last_rate_limits = dict(info)
                self._emit("notification", {"rate_limits": dict(info)})
            return False
        if kind == "result":
            self._handle_result(event, session)
            return True

        self._emit("other_message", {"type": kind})
        return False

    def _handle_system(
        self, event: Mapping[str, Any], session: ClaudeSession, title: str | None
    ) -> bool:
        subtype = event.get("subtype")
        if subtype == "init":
            reported = event.get("session_id")
            if isinstance(reported, str) and reported:
                session.thread_id = reported
            session.started = True
            turn_id = str(event.get("uuid") or f"turn-{session.turn_count}")
            self._emit(
                "session_started",
                {
                    "thread_id": session.thread_id,
                    "turn_id": turn_id,
                    "session_id": compose_session_id(session.thread_id, turn_id),
                    "turn_number": session.turn_count,
                    "title": title,
                    "model": event.get("model"),
                    "permission_mode": event.get("permissionMode"),
                    "agent_version": event.get("claude_code_version"),
                    "tools": event.get("tools"),
                },
            )
            return False
        if subtype == "post_turn_summary":
            self._emit(
                "notification",
                {
                    "summary": event.get("status_detail") or "",
                    "status": event.get("status_category"),
                    "needs_action": event.get("needs_action") or "",
                },
            )
            return False
        if subtype == "thinking_tokens":
            return False
        self._emit("notification", {"subtype": subtype})
        return False

    def _handle_assistant(self, event: Mapping[str, Any]) -> None:
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            btype = block.get("type")
            if btype == "text":
                self._emit("notification", {"summary": _clip(block.get("text"))})
            elif btype == "tool_use":
                self._emit(
                    "notification",
                    {"summary": f"tool: {block.get('name')}", "tool": block.get("name")},
                )
        if isinstance(message, Mapping) and event.get("error"):
            self._emit("turn_ended_with_error", {"error": str(event.get("error"))})

    def _handle_result(self, event: Mapping[str, Any], session: ClaudeSession) -> None:
        usage = event.get("usage") if isinstance(event.get("usage"), Mapping) else {}
        session.credit(usage, event.get("total_cost_usd"))
        denials = event.get("permission_denials")
        if isinstance(denials, list) and denials:
            session.permission_denials.extend(denials)

        payload: dict[str, Any] = {
            "result": _clip(event.get("result")),
            "stop_reason": event.get("stop_reason"),
            "num_turns": event.get("num_turns"),
            "duration_ms": event.get("duration_ms"),
            # Cumulative, in the absolute-total shape SPEC 13.5 requires.
            "total_token_usage": session.absolute_usage(),
            "cost": {
                "turn_usd": event.get("total_cost_usd"),
                "session_usd": round(session.total_cost_usd, 6),
            },
            "cache": {
                "read_input_tokens": session.cache_read_tokens,
                "creation_input_tokens": session.cache_creation_tokens,
            },
            "permission_denials": len(session.permission_denials),
        }
        if session.last_rate_limits is not None:
            payload["rate_limits"] = dict(session.last_rate_limits)

        # SPEC 10.3 / docs/claude-protocol.md §3.1: `subtype` reads "success"
        # even on a failed run. `is_error` is the authority; treating subtype as
        # the signal marks an auth failure successful, and the orchestrator then
        # schedules a 1-second continuation retry against a permanently broken
        # credential instead of backing off.
        if bool(event.get("is_error")):
            self._emit("turn_failed", payload)
            raise TurnFailed(
                str(event.get("result") or "coding agent reported an error"),
                terminal_reason=event.get("terminal_reason"),
                api_error_status=event.get("api_error_status"),
            )
        self._emit("turn_completed", payload)

    # -- plumbing ----------------------------------------------------------

    def _emit(self, name: str, payload: Mapping[str, Any]) -> None:
        if self._on_event is None:
            return
        usage = payload.get("total_token_usage")
        event = AgentEvent(
            event=name,
            timestamp=self._now(),
            codex_app_server_pid=str(self.pid) if self.pid else None,
            usage={"total_token_usage": dict(usage)} if isinstance(usage, Mapping) else None,
            payload=dict(payload),
        )
        with suppress(Exception):  # observer isolation (SPEC 14.2)
            self._on_event(event)

    async def _drain_stderr(self, proc: Any) -> None:
        """Keep diagnostics strictly separate from the protocol stream."""
        stream = proc.stderr
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-50]

    async def _reap(self, proc: Any) -> None:
        if proc.returncode is None:
            with _suppress():
                await asyncio.wait_for(proc.wait(), timeout=5)


class _suppress:
    """Tiny context manager; ``contextlib.suppress`` with a narrower import."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)


def _clip(value: Any, limit: int = 400) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_result(event: Mapping[str, Any]) -> dict[str, Any]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                return {"tool_use_id": block.get("tool_use_id")}
    return {}


def _build(cfg: Any, **kwargs: Any) -> ClaudeCodeClient:
    if not isinstance(cfg, ClaudeConfig):
        cfg = ClaudeConfig.from_mapping(cfg if isinstance(cfg, Mapping) else None)
    kwargs.pop("approval_decider", None)
    return ClaudeCodeClient(cfg, **kwargs)


register_backend(
    AgentBackendSpec(
        kind="claude",
        config_key="claude",
        factory=_build,
        description="Claude Code in headless stream-json mode (verified against 2.1.214)",
    )
)
