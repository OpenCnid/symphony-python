"""Codex app-server client (SPEC 10.1, 10.2, 10.3, 10.6).

Owns subprocess launch, the JSON-RPC-over-stdio transport, and the
session/thread/turn lifecycle for one per-issue workspace.

Protocol source of truth (SPEC 10, preamble)
--------------------------------------------
The targeted Codex app-server version — not this specification and not this
module — controls protocol schemas, transport framing, and method names. Every
version-specific string therefore lives in exactly one place: the
``ProtocolNames`` block below. The state machine in this module never spells a
protocol string inline, so correcting the names against the output of
``codex app-server generate-json-schema`` is a single-dataclass edit that
touches no lifecycle logic. ``ProtocolNames`` is also injectable per client, so
a caller can pin a different Codex version without a fork.

What Symphony fixes regardless of protocol version (SPEC 10.1, 10.2):

* launch is ``bash -lc <codex.command>`` with ``cwd`` set to the workspace;
* the absolute workspace path is supplied wherever the protocol accepts a cwd;
* the first turn carries the rendered issue prompt, and continuation turns
  reuse the same live thread rather than resending it;
* ``session_id`` is ``"<thread_id>-<turn_id>"`` (via :func:`symphony.models.session_id`);
* the subprocess survives continuation turns and stops only when the run ends.

Timeouts (SPEC 10.6) — three meanings, three mechanisms
-------------------------------------------------------
* ``read_timeout_ms`` bounds one request/response exchange, enforced by
  :meth:`AppServerClient._request`;
* ``turn_timeout_ms`` bounds *silence* while a turn streams, enforced by
  :meth:`AppServerClient._await_turn`, whose deadline is recomputed from the
  last app-server output on every wake-up. It is not a total-runtime cap;
* ``stall_timeout_ms`` is enforced by the orchestrator on event inactivity and
  is deliberately never read here.

Documented policy positions
---------------------------
This implementation targets trusted environments (SPEC 15.1). Per SPEC 10.5 it
auto-approves command-execution and file-change approvals for the session, and
treats user-input-required turns as a hard failure. Unsupported dynamic tool
calls get a structured failure response and the session continues. The policy
is injectable (``approval_decider``) because :mod:`symphony.agent.approvals`
owns the canonical version; the default here is the same documented behavior so
this module is usable and testable standalone.

Windows note (CONTRACTS house rule 7): ``bash -lc`` is honored literally on
every platform. On Windows this resolves to whichever ``bash`` is on PATH
(typically Git Bash). If no ``bash`` exists the launch raises
:class:`~symphony.errors.CodexNotFound` rather than silently substituting
another shell. Because ``-l`` sources login profiles, a noisy profile can write
non-protocol text to stdout; such lines are reported as ``malformed`` events and
the stream resynchronizes at the next newline.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from symphony.agent.events import AgentEvent
from symphony.errors import (
    AgentError,
    CodexNotFound,
    InvalidWorkspaceCwd,
    PortExit,
    ResponseError,
    ResponseTimeout,
    TurnCancelled,
    TurnFailed,
    TurnInputRequired,
    TurnTimeout,
)
from symphony.models import session_id as compose_session_id
from symphony.trackers.base import ToolResult, ToolSpec

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime dependency
    from symphony.workflow.config import CodexConfig

__all__ = [
    "MAX_LINE_BYTES",
    "PROTOCOL",
    "AppServerClient",
    "AppServerSession",
    "ApprovalDecision",
    "ProtocolNames",
    "default_approval_decision",
    "resolve_bash",
]


# ==========================================================================
# PROTOCOL CONSTANTS BLOCK — VERSION-SPECIFIC, NOT VERIFIED AGAINST A BINARY
# ==========================================================================
#
# Every string below is owned by the targeted Codex app-server version
# (SPEC 10 preamble: "If this specification appears to conflict with the
# targeted Codex app-server protocol, the Codex protocol controls protocol
# shape and transport behavior").
#
# These values were written against the documented shape of the app-server
# protocol and were NOT confirmed against a running `codex` binary, which is
# unavailable in this environment. Exactly one of them appears verbatim in
# SPEC.md (`thread/tokenUsage/updated`, SPEC 13.5); the rest follow its
# `namespace/camelCase` convention and are best-effort.
#
# To correct them: run `codex app-server generate-json-schema`, diff the method
# and field names, and edit only this dataclass. Nothing outside this block
# spells a protocol string, so no lifecycle or timeout logic needs to move.
# Callers may also pass `protocol=ProtocolNames(...)` to pin a version.


@dataclass(frozen=True, slots=True)
class ProtocolNames:
    """Version-specific JSON-RPC method and field names (SPEC 10 preamble)."""

    # --- transport -------------------------------------------------------
    jsonrpc_version: str = "2.0"

    # --- client -> server requests / notifications ------------------------
    initialize: str = "initialize"
    initialized: str = "initialized"
    new_thread: str = "thread/start"
    start_turn: str = "thread/sendMessage"
    interrupt_turn: str = "thread/interrupt"

    # --- server -> client notifications ----------------------------------
    turn_started: str = "thread/turn/started"
    turn_completed: str = "thread/turn/completed"
    turn_failed: str = "thread/turn/failed"
    turn_cancelled: str = "thread/turn/cancelled"
    token_usage: str = "thread/tokenUsage/updated"

    # --- server -> client requests (a response is REQUIRED) ---------------
    exec_approval: str = "thread/execCommandApproval"
    patch_approval: str = "thread/applyPatchApproval"
    tool_call: str = "thread/toolCall"
    user_input: str = "thread/userInput"

    # --- request/response field names -------------------------------------
    f_client_info: str = "clientInfo"
    f_cwd: str = "cwd"
    f_thread_id: str = "threadId"
    f_turn_id: str = "turnId"
    f_approval_policy: str = "approvalPolicy"
    f_thread_sandbox: str = "sandbox"
    f_turn_sandbox_policy: str = "sandboxPolicy"
    f_tools: str = "tools"
    f_prompt: str = "input"
    f_title: str = "title"
    f_usage: str = "usage"
    f_rate_limits: str = "rateLimits"

    # --- tool spec advertisement fields -----------------------------------
    f_tool_name: str = "name"
    f_tool_description: str = "description"
    f_tool_schema: str = "inputSchema"
    f_tool_arguments: str = "arguments"

    # --- approval + tool response payloads ---------------------------------
    f_decision: str = "decision"
    approve_value: str = "approved_for_session"
    deny_value: str = "denied"
    f_tool_ok: str = "ok"
    f_tool_content: str = "content"
    f_tool_error: str = "error"

    def turn_terminal_outcome(self, method: str) -> str | None:
        """Map a notification method to a turn outcome, or ``None``."""
        if method == self.turn_completed:
            return "completed"
        if method == self.turn_failed:
            return "failed"
        if method == self.turn_cancelled:
            return "cancelled"
        return None


PROTOCOL = ProtocolNames()

# SPEC 10.1 RECOMMENDED process setting: max line size 10 MB for safe buffering.
MAX_LINE_BYTES = 10 * 1024 * 1024

# JSON-RPC error code used when this client declines a server-initiated request.
JSONRPC_REQUEST_FAILED = -32000

_CLIENT_NAME = "symphony-python"
_CLIENT_VERSION = "0.1.0"
_STDERR_TAIL_LINES = 50
_STOP_GRACE_SECONDS = 2.0
_MALFORMED_PREVIEW_CHARS = 200

# ==========================================================================
# END PROTOCOL CONSTANTS BLOCK
# ==========================================================================


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any] | Any]
EventSink = Callable[[AgentEvent], None]
Spawn = Callable[[list[str], Path, dict[str, str]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Outcome of the documented approval policy (SPEC 10.5).

    Deliberately protocol-agnostic: the client maps ``approved`` onto the
    version-specific decision string from :class:`ProtocolNames`.
    """

    approved: bool
    reason: str = ""


ApprovalDecider = Callable[[str, Mapping[str, Any]], ApprovalDecision]


def default_approval_decision(method: str, params: Mapping[str, Any]) -> ApprovalDecision:
    """This implementation's documented high-trust policy (SPEC 10.5).

    Auto-approves command-execution and file-change approvals for the session.
    :mod:`symphony.agent.approvals` owns the canonical policy; pass it as
    ``approval_decider`` to override this default.
    """
    del params  # the documented policy is unconditional; kept for signature parity
    del method
    return ApprovalDecision(approved=True, reason="auto_approved_for_session")


@dataclass(slots=True)
class _Turn:
    """In-flight turn state (SPEC 10.3)."""

    number: int
    turn_id: str | None = None
    outcome: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def finish(self, outcome: str, payload: Mapping[str, Any] | None = None) -> None:
        if self.outcome is None:
            self.outcome = outcome
            self.payload = dict(payload or {})
            self.done.set()


class AppServerSession:
    """One live app-server thread inside one workspace (SPEC 10.2).

    The subprocess is owned by the client and stays alive across continuation
    turns; :meth:`stop` ends the run (SPEC 10.3).
    """

    __slots__ = ("_client", "thread_id")

    def __init__(self, client: AppServerClient, thread_id: str) -> None:
        self.thread_id = thread_id
        self._client = client

    @property
    def client(self) -> AppServerClient:
        return self._client

    @property
    def turn_count(self) -> int:
        return self._client.turn_count

    async def start_turn(self, prompt: str, *, title: str | None = None) -> None:
        """Run one turn on this live thread to completion (SPEC 10.3)."""
        await self._client.run_turn(self, prompt, title=title)

    async def stop(self) -> None:
        """Stop the app-server subprocess. Idempotent (SPEC 10.3)."""
        await self._client.stop()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AppServerSession(thread_id={self.thread_id!r})"


class AppServerClient:
    """JSON-RPC-over-stdio client for one Codex app-server subprocess.

    Implements SPEC 10.1 (launch), 10.2 (session startup), 10.3 (streaming turn
    processing) and 10.6 (timeouts and error mapping). One client owns exactly
    one workspace, one subprocess and one thread.
    """

    def __init__(
        self,
        cfg: CodexConfig,
        *,
        workspace: Path,
        tool_specs: list[ToolSpec],
        tool_executor: ToolExecutor,
        on_event: EventSink,
        approval_decider: ApprovalDecider | None = None,
        secret_env_names: Sequence[str] = (),
        protocol: ProtocolNames = PROTOCOL,
        spawn: Spawn | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.workspace = Path(workspace)
        self.tool_specs = list(tool_specs)
        self.protocol = protocol
        self.turn_count = 0

        self._tool_executor = tool_executor
        self._on_event = on_event
        self._decide_approval = approval_decider or default_approval_decision
        self._secret_env_names = tuple(secret_env_names)
        self._spawn = spawn or _default_spawn
        self._now = now or (lambda: datetime.now(UTC))

        self._tool_names = {spec.name for spec in self.tool_specs}
        self._proc: Any = None
        self._session: AppServerSession | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._turn: _Turn | None = None
        self._last_output_at: float = 0.0
        self._exit_reason: BaseException | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Introspection surface (kept flat and named for the RLM driver)
    # ------------------------------------------------------------------

    @property
    def pid(self) -> str | None:
        """PID as a string, matching ``AgentEvent.codex_app_server_pid``."""
        return None if self._proc is None else str(self._proc.pid)

    @property
    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.returncode

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def session(self) -> AppServerSession | None:
        return self._session

    def launch_argv(self) -> list[str]:
        """SPEC 10.1: invocation is ``bash -lc <codex.command>``.

        ``argv[0]`` is the PATH-resolved absolute path to ``bash`` rather than
        the bare name; see :func:`resolve_bash` for why that distinction is
        load-bearing on Windows. Raises :class:`CodexNotFound` if no ``bash``
        exists on PATH.
        """
        return [resolve_bash(), "-lc", self.cfg.command]

    def stderr_tail(self) -> list[str]:
        """Recent diagnostic stderr lines, never parsed as protocol (SPEC 10.3)."""
        return list(self._stderr_tail)

    # ------------------------------------------------------------------
    # SPEC 10.1 / 10.2 — launch and session startup
    # ------------------------------------------------------------------

    async def start_session(self) -> AppServerSession:
        """Launch the subprocess and create the coding-agent thread (SPEC 10.2).

        Emits ``startup_failed`` and raises a SPEC 10.6 error on any failure.
        """
        if self._session is not None:
            raise AgentError(
                "app-server session already started for this client",
                thread_id=self._session.thread_id,
            )
        try:
            self._assert_launch_cwd()
            await self._launch()
            await self._initialize()
            thread_id = await self._create_thread()
        except Exception as exc:
            self._emit("startup_failed", {"error": _error_payload(exc)})
            await self.stop()
            raise
        self._session = AppServerSession(self, thread_id)
        return self._session

    def _assert_launch_cwd(self) -> None:
        """SPEC 9.5 Invariant 1 / 15.2: the agent runs only in its workspace."""
        path = self.workspace
        if not path.is_absolute():
            raise InvalidWorkspaceCwd(
                "agent launch cwd must be an absolute workspace path",
                workspace=str(path),
            )
        if not path.is_dir():
            raise InvalidWorkspaceCwd(
                "agent launch cwd is not an existing directory",
                workspace=str(path),
            )

    def _child_env(self) -> dict[str, str]:
        """SPEC 15.3: tracker credentials are not inherited by the child."""
        env = dict(os.environ)
        for name in self._secret_env_names:
            env.pop(name, None)
        return env

    async def _launch(self) -> None:
        argv = self.launch_argv()
        try:
            self._proc = await self._spawn(argv, self.workspace, self._child_env())
        except FileNotFoundError as exc:
            raise CodexNotFound(
                "could not launch the coding agent: 'bash' was not found on PATH",
                command=self.cfg.command,
            ) from exc
        except NotImplementedError as exc:  # pragma: no cover - platform guard
            raise CodexNotFound(
                "the running event loop does not support subprocesses",
                command=self.cfg.command,
            ) from exc
        self._last_output_at = asyncio.get_running_loop().time()
        self._spawn_task(self._read_stdout())
        self._spawn_task(self._read_stderr())
        self._spawn_task(self._watch_exit())

    async def _initialize(self) -> None:
        p = self.protocol
        await self._request(
            p.initialize,
            {
                p.f_client_info: {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
                p.f_cwd: str(self.workspace),
            },
        )
        self._notify(p.initialized, {})

    async def _create_thread(self) -> str:
        """Create the thread with workspace cwd + documented policies (SPEC 10.2)."""
        p = self.protocol
        params: dict[str, Any] = {
            p.f_cwd: str(self.workspace),
            p.f_approval_policy: self.cfg.approval_policy,
            p.f_thread_sandbox: self.cfg.thread_sandbox,
        }
        if self.tool_specs:
            params[p.f_tools] = [self._tool_spec_payload(spec) for spec in self.tool_specs]
        result = await self._request(p.new_thread, params)
        thread_id = _dig_str(result, p.f_thread_id)
        if not thread_id:
            raise ResponseError(
                "app-server thread response did not carry a thread identifier",
                method=p.new_thread,
                field=p.f_thread_id,
            )
        return thread_id

    def _tool_spec_payload(self, spec: ToolSpec) -> dict[str, Any]:
        p = self.protocol
        return {
            p.f_tool_name: spec.name,
            p.f_tool_description: spec.description,
            p.f_tool_schema: spec.input_schema,
        }

    # ------------------------------------------------------------------
    # SPEC 10.3 — streaming turn processing
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        session: AppServerSession,
        prompt: str,
        *,
        title: str | None = None,
    ) -> None:
        """Start a turn on the live thread and stream it to termination.

        Returns ``None`` on the protocol completion signal. Failure,
        cancellation, user-input-required, stream silence beyond
        ``turn_timeout_ms`` and subprocess exit all raise a SPEC 10.6 error
        (SPEC 10.3 completion conditions).
        """
        if session.client is not self or self._session is not session:
            raise AgentError("session does not belong to this app-server client")
        if self._turn is not None:
            raise AgentError("a turn is already active on this thread")

        p = self.protocol
        self.turn_count += 1
        turn = _Turn(number=self.turn_count)
        self._turn = turn
        try:
            params: dict[str, Any] = {
                p.f_thread_id: session.thread_id,
                p.f_cwd: str(self.workspace),
                p.f_prompt: prompt,
                p.f_turn_sandbox_policy: self.cfg.turn_sandbox_policy,
            }
            if title is not None:
                params[p.f_title] = title
            self._touch()
            result = await self._request(p.start_turn, params)
            turn_id = _dig_str(result, p.f_turn_id) or turn.turn_id or ""
            turn.turn_id = turn_id
            self._emit(
                "session_started",
                {
                    "session_id": compose_session_id(session.thread_id, turn_id),
                    "thread_id": session.thread_id,
                    "turn_id": turn_id,
                    "turn_number": turn.number,
                    "title": title,
                },
            )
            await self._await_turn(turn)
        finally:
            self._turn = None

        self._resolve_turn_outcome(turn)

    def _resolve_turn_outcome(self, turn: _Turn) -> None:
        """Map the terminal turn signal onto SPEC 10.6 error categories."""
        outcome = turn.outcome
        payload = dict(turn.payload)
        payload.setdefault("turn_id", turn.turn_id)
        if outcome == "completed":
            self._emit("turn_completed", payload)
            return
        if outcome == "failed":
            self._emit("turn_failed", payload)
            raise TurnFailed("app-server reported turn failure", **_safe_details(payload))
        if outcome == "cancelled":
            self._emit("turn_cancelled", payload)
            raise TurnCancelled("app-server reported turn cancellation", **_safe_details(payload))
        if outcome == "input_required":
            self._emit("turn_input_required", payload)
            raise TurnInputRequired(
                "app-server requested user input; documented policy fails the turn",
                **_safe_details(payload),
            )
        if outcome == "exited":
            self._emit("turn_ended_with_error", payload)
            raise PortExit(
                "app-server subprocess exited while a turn was active",
                **_safe_details(payload),
            )
        self._emit("turn_ended_with_error", payload)  # pragma: no cover - defensive
        raise TurnFailed("turn ended without a recognized signal", **_safe_details(payload))

    async def _await_turn(self, turn: _Turn) -> None:
        """Enforce ``turn_timeout_ms`` as a *silence* bound (SPEC 10.6).

        The deadline is recomputed from the last app-server output on every
        wake-up, so any output resets it. This is deliberately not a total turn
        runtime cap.
        """
        loop = asyncio.get_running_loop()
        window = max(self.cfg.turn_timeout_ms, 0) / 1000.0
        while True:
            remaining = (self._last_output_at + window) - loop.time()
            if remaining <= 0:
                await self._interrupt_turn(turn)
                turn.finish("timeout")
                self._emit(
                    "turn_failed",
                    {"turn_id": turn.turn_id, "reason": "turn_timeout"},
                )
                raise TurnTimeout(
                    "no app-server output within codex.turn_timeout_ms",
                    turn_timeout_ms=self.cfg.turn_timeout_ms,
                    turn_id=turn.turn_id,
                )
            try:
                await asyncio.wait_for(turn.done.wait(), remaining)
            except TimeoutError:
                continue  # silence window may have moved; recompute
            return

    async def _interrupt_turn(self, turn: _Turn) -> None:
        """Best-effort turn interrupt; the subprocess teardown is the caller's."""
        params: dict[str, Any] = {}
        if self._session is not None:
            params[self.protocol.f_thread_id] = self._session.thread_id
        if turn.turn_id:
            params[self.protocol.f_turn_id] = turn.turn_id
        with suppress(Exception):  # teardown must not mask the timeout
            self._notify(self.protocol.interrupt_turn, params)

    # ------------------------------------------------------------------
    # Transport: framing, dispatch, request/response
    # ------------------------------------------------------------------

    async def _read_stdout(self) -> None:
        """Protocol stream only. Diagnostics live on stderr (SPEC 10.3)."""
        stream = self._proc.stdout
        if stream is None:  # pragma: no cover - defensive
            return
        while True:
            try:
                raw = await stream.readline()
            except ValueError:
                # asyncio drops the oversize buffer; report and resynchronize.
                self._touch()
                self._emit(
                    "malformed",
                    {"reason": "line_exceeds_max_line_size", "max_line_bytes": MAX_LINE_BYTES},
                )
                continue
            except (asyncio.IncompleteReadError, ConnectionResetError):  # pragma: no cover
                return
            if not raw:
                return
            self._touch()
            self._dispatch_line(raw)

    async def _read_stderr(self) -> None:
        """Diagnostic stream, kept strictly out of protocol parsing (SPEC 10.3)."""
        stream = self._proc.stderr
        if stream is None:  # pragma: no cover - defensive
            return
        while True:
            try:
                raw = await stream.readline()
            except ValueError:  # pragma: no cover - oversize diagnostics
                continue
            except (asyncio.IncompleteReadError, ConnectionResetError):  # pragma: no cover
                return
            if not raw:
                return
            self._stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))

    def _exit_error(self, code: int | None) -> BaseException:
        """Map a subprocess exit to a SPEC 10.6 category.

        A POSIX shell reports 127 for "command not found", which is the only
        signal available that ``codex.command`` itself is missing.
        """
        if code == 127:
            return CodexNotFound(
                "shell reported the coding-agent command was not found",
                command=self.cfg.command,
                returncode=code,
                stderr_tail=self.stderr_tail()[-5:],
            )
        return PortExit(
            "app-server subprocess exited",
            returncode=code,
            stderr_tail=self.stderr_tail()[-5:],
        )

    async def _watch_exit(self) -> None:
        code = await self._proc.wait()
        exc = self._exit_error(code)
        self._exit_reason = exc
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        turn = self._turn
        if turn is not None:
            turn.finish("exited", {"returncode": code, "stderr_tail": self.stderr_tail()[-5:]})

    def _touch(self) -> None:
        """Any app-server output resets the turn silence window (SPEC 10.6)."""
        self._last_output_at = asyncio.get_running_loop().time()

    def _dispatch_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            msg = json.loads(text)
        except ValueError:
            self._emit(
                "malformed",
                {"reason": "not_json", "preview": text[:_MALFORMED_PREVIEW_CHARS]},
            )
            return
        if not isinstance(msg, dict):
            self._emit(
                "malformed",
                {"reason": "not_an_object", "preview": text[:_MALFORMED_PREVIEW_CHARS]},
            )
            return

        has_id = "id" in msg and msg["id"] is not None
        method = msg.get("method")
        if has_id and method is None:
            self._resolve_response(msg)
        elif has_id and method is not None:
            self._spawn_task(self._handle_server_request(msg))
        elif method is not None:
            self._handle_notification(str(method), _as_dict(msg.get("params")))
        else:
            self._emit("other_message", {"message": msg})

    def _resolve_response(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        fut = self._pending.get(req_id) if isinstance(req_id, int) else None
        if fut is None or fut.done():
            self._emit("other_message", {"reason": "unmatched_response", "message": msg})
            return
        if "error" in msg and msg["error"] is not None:
            err = _as_dict(msg["error"])
            fut.set_exception(
                ResponseError(
                    str(err.get("message") or "app-server returned a JSON-RPC error"),
                    code=err.get("code"),
                    data=err.get("data"),
                )
            )
            return
        fut.set_result(msg.get("result"))

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        p = self.protocol
        turn = self._turn

        if method == p.turn_started:
            if turn is not None and not turn.turn_id:
                turn.turn_id = _dig_str(params, p.f_turn_id) or turn.turn_id
            self._emit("notification", {"method": method, "params": params})
            return

        outcome = p.turn_terminal_outcome(method)
        if outcome is not None:
            if turn is not None:
                if not turn.turn_id:
                    turn.turn_id = _dig_str(params, p.f_turn_id) or turn.turn_id
                turn.finish(outcome, {"method": method, "params": params})
            else:
                self._emit("notification", {"method": method, "params": params})
            return

        if method == p.user_input:
            if turn is not None:
                turn.finish("input_required", {"method": method, "params": params})
            else:  # pragma: no cover - out-of-turn input request
                self._emit("turn_input_required", {"method": method, "params": params})
            return

        self._emit(
            "notification",
            {"method": method, "params": params},
            usage=self._extract_usage(params),
        )

    def _extract_usage(self, params: Mapping[str, Any]) -> dict[str, Any] | None:
        """Surface the raw usage map; SPEC 13.5 accounting is the orchestrator's."""
        usage = params.get(self.protocol.f_usage)
        return dict(usage) if isinstance(usage, Mapping) else None

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        p = self.protocol
        req_id = msg["id"]
        method = str(msg.get("method"))
        params = _as_dict(msg.get("params"))
        try:
            if method in (p.exec_approval, p.patch_approval):
                self._respond_approval(req_id, method, params)
            elif method == p.tool_call:
                await self._respond_tool_call(req_id, params)
            elif method == p.user_input:
                # SPEC 10.5: never stall. Decline, then fail the turn.
                self._send_error(req_id, "user input is not available in this deployment")
                turn = self._turn
                if turn is not None:
                    turn.finish("input_required", {"method": method, "params": params})
                else:  # pragma: no cover - out-of-turn input request
                    self._emit("turn_input_required", {"method": method, "params": params})
            else:
                self._emit("other_message", {"reason": "unknown_request", "method": method})
                self._send_error(req_id, f"unsupported app-server request: {method}")
        except Exception as exc:  # a stalled session is worse than a lost error
            self._send_error(req_id, f"client failed to handle {method}: {exc}")

    def _respond_approval(self, req_id: Any, method: str, params: dict[str, Any]) -> None:
        """SPEC 10.5 documented policy, applied without blocking the stream."""
        decision = self._decide_approval(method, params)
        value = self.protocol.approve_value if decision.approved else self.protocol.deny_value
        self._send_result(req_id, {self.protocol.f_decision: value})
        if decision.approved:
            self._emit(
                "approval_auto_approved",
                {"method": method, "decision": value, "reason": decision.reason},
            )
        else:
            self._emit(
                "notification",
                {"method": method, "decision": value, "reason": decision.reason},
            )

    async def _respond_tool_call(self, req_id: Any, params: dict[str, Any]) -> None:
        """Execute advertised tools host-side; reject the rest (SPEC 10.5)."""
        p = self.protocol
        name = str(params.get(p.f_tool_name) or "")
        arguments = _as_dict(params.get(p.f_tool_arguments))
        if name not in self._tool_names:
            self._emit("unsupported_tool_call", {"tool": name})
            self._send_result(req_id, self._tool_failure(f"unsupported tool: {name}"))
            return
        try:
            outcome = self._tool_executor(name, arguments)
            if isinstance(outcome, Awaitable):
                outcome = await outcome
        except Exception as exc:  # tool errors must not stall the session (SPEC 10.5)
            self._send_result(req_id, self._tool_failure(f"{type(exc).__name__}: {exc}"))
            return
        self._send_result(req_id, self._tool_payload(outcome))

    def _tool_failure(self, error: str) -> dict[str, Any]:
        p = self.protocol
        return {p.f_tool_ok: False, p.f_tool_content: None, p.f_tool_error: error}

    def _tool_payload(self, outcome: Any) -> dict[str, Any]:
        p = self.protocol
        if isinstance(outcome, ToolResult):
            return {
                p.f_tool_ok: outcome.ok,
                p.f_tool_content: outcome.content,
                p.f_tool_error: outcome.error,
            }
        if isinstance(outcome, Mapping) and "ok" in outcome:
            return {
                p.f_tool_ok: bool(outcome.get("ok")),
                p.f_tool_content: outcome.get("content"),
                p.f_tool_error: outcome.get("error"),
            }
        return {p.f_tool_ok: True, p.f_tool_content: outcome, p.f_tool_error: None}

    # ------------------------------------------------------------------
    # Framing writers and the request/response timeout (SPEC 10.6)
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout_ms: int | None = None,
    ) -> Any:
        """One request/response exchange bounded by ``read_timeout_ms``."""
        req_id = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            self._write(
                {
                    "jsonrpc": self.protocol.jsonrpc_version,
                    "id": req_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            window = self.cfg.read_timeout_ms if timeout_ms is None else timeout_ms
            try:
                return await asyncio.wait_for(fut, max(window, 0) / 1000.0)
            except TimeoutError as exc:
                raise ResponseTimeout(
                    "no app-server response within codex.read_timeout_ms",
                    method=method,
                    read_timeout_ms=window,
                ) from exc
        finally:
            self._pending.pop(req_id, None)

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write(
            {
                "jsonrpc": self.protocol.jsonrpc_version,
                "method": method,
                "params": dict(params),
            }
        )

    def _send_result(self, req_id: Any, result: Mapping[str, Any]) -> None:
        self._write(
            {"jsonrpc": self.protocol.jsonrpc_version, "id": req_id, "result": dict(result)}
        )

    def _send_error(self, req_id: Any, message: str) -> None:
        self._write(
            {
                "jsonrpc": self.protocol.jsonrpc_version,
                "id": req_id,
                "error": {"code": JSONRPC_REQUEST_FAILED, "message": message},
            }
        )

    def _write(self, msg: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None:
            raise PortExit("app-server subprocess is not running")
        if proc.stdin is None or proc.returncode is not None:
            # The exit watcher may not have woken yet; classify from returncode
            # directly so a 127 shell exit never races into a generic port_exit.
            raise self._exit_reason or self._exit_error(proc.returncode)
        payload = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            proc.stdin.write(payload)
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise PortExit("app-server stdin is closed") from exc

    # ------------------------------------------------------------------
    # Lifecycle teardown
    # ------------------------------------------------------------------

    async def stop_session(self, session: AppServerSession | None = None) -> None:
        """SPEC 16.5 spelling of :meth:`stop`."""
        del session
        await self.stop()

    async def stop(self) -> None:
        """Terminate the subprocess and drain tasks. Idempotent (SPEC 10.3)."""
        if self._stopped:
            return
        self._stopped = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with suppress(ProcessLookupError, OSError):  # already gone
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), _STOP_GRACE_SECONDS)
            except TimeoutError:  # pragma: no cover - stubborn child
                with suppress(ProcessLookupError, OSError):
                    proc.kill()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()
        if proc is not None:
            if proc.stdin is not None:
                with suppress(RuntimeError, OSError):
                    proc.stdin.close()
            # asyncio does not close subprocess pipe transports until GC, which
            # surfaces as "unclosed transport" noise after the loop is gone.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                with suppress(RuntimeError, OSError):
                    transport.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _spawn_task(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _emit(
        self,
        event: str,
        payload: Mapping[str, Any] | None = None,
        *,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Emit one SPEC 10.4 runtime event upstream."""
        agent_event = AgentEvent(
            event=event,
            timestamp=self._now(),
            codex_app_server_pid=self.pid,
            usage=usage,
            payload=dict(payload or {}),
        )
        with suppress(Exception):  # an observer must not break the run (SPEC 17.6)
            self._on_event(agent_event)


def resolve_bash() -> str:
    """Resolve ``bash`` through PATH, explicitly (SPEC 10.1, CONTRACTS rule 7).

    SPEC 10.1 mandates ``bash -lc <codex.command>``; this function does not
    change that, it only decides *which* ``bash``.

    Windows fallback, stated rather than silently applied: ``CreateProcess``
    searches ``System32`` before ``PATH``, so passing the bare name ``bash``
    resolves to WSL's ``bash.exe`` on any host where WSL is installed — even
    when a Git Bash earlier on PATH is the intended shell. WSL runs in a Linux
    VM that cannot see a ``D:\\...`` workspace at all, so the agent would launch
    with a cwd that does not exist, breaking SPEC 9.5 Invariant 1 with no
    visible error. Resolving through PATH first keeps shell selection
    deterministic and operator-controlled on every platform; on POSIX hosts it
    selects exactly the file ``execvp`` would have chosen.
    """
    found = shutil.which("bash")
    if found is None:
        raise CodexNotFound(
            "no 'bash' found on PATH; SPEC 10.1 requires 'bash -lc <codex.command>'"
        )
    return found


async def _default_spawn(argv: list[str], cwd: Path, env: dict[str, str]) -> Any:
    """SPEC 10.1: ``bash -lc <codex.command>``, cwd = workspace, 10 MB lines."""
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=MAX_LINE_BYTES,
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _dig_str(payload: Any, key: str) -> str | None:
    """Read ``key`` from a result object, or from a single nested object.

    Codex versions differ on whether identities sit at the top level or under a
    wrapper (``{"thread": {"threadId": ...}}``); both shapes are accepted so a
    version bump does not become a lifecycle change.
    """
    if not isinstance(payload, Mapping):
        return None
    direct = payload.get(key)
    if isinstance(direct, str) and direct:
        return direct
    for value in payload.values():
        if isinstance(value, Mapping):
            nested = value.get(key)
            if isinstance(nested, str) and nested:
                return nested
    return None


def _error_payload(exc: BaseException) -> dict[str, Any]:
    to_dict = getattr(exc, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return {"category": "agent_error", "message": str(exc)}


def _safe_details(payload: Mapping[str, Any]) -> dict[str, Any]:
    """JSON-safe error details; never carries secrets (SPEC 15.3)."""
    return {k: v for k, v in payload.items() if k in {"turn_id", "reason", "returncode"}}
