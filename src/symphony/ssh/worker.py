"""SSH worker extension — remote host pool, assignment, remote launch (SPEC Appendix A).

This module is OPTIONAL. When ``worker.ssh_hosts`` is omitted the pool reports
``enabled is False`` and every entry point either raises
:class:`NoSSHHostsConfigured` or returns ``None``; nothing here changes
local-execution behavior (SPEC A, preamble).

What this module does and does not own
--------------------------------------
SPEC A.1: "The orchestrator remains the single source of truth for polling,
claims, retries, and reconciliation." Assignment adds a *host* to a run's
execution identity alongside its workspace. It moves no scheduling authority:
there is no poll loop, no claim table, and no retry ladder here. The pool hands
out and reclaims host leases; :func:`decide_failover` returns a recommendation
that the orchestrator's existing SPEC 8.4 machinery acts on.

The three hazards from SPEC A.3 that this module is built around
----------------------------------------------------------------
**1. Remote path semantics (A.3 "Path and command safety").**
``workspace.root`` is interpreted *on the remote host* (SPEC A.1), so the SPEC
9.5 / 15.2 containment invariants cannot be enforced with
:mod:`symphony.workspace.safety` — that module calls ``Path.resolve()``, which
consults the *orchestrator's* filesystem and, on a Windows orchestrator, would
also rewrite ``/srv/ws`` into ``\\srv\\ws``. Remote containment is therefore
enforced twice, by :func:`assert_remote_within_root`:

* lexically, in pure :class:`~pathlib.PurePosixPath` terms, before anything is
  sent; and
* again against the paths the *remote* shell actually resolved, returned by
  :meth:`SSHWorker.preflight`. Only the second check can see a remote symlink
  that points out of the workspace root, which is the escape a local check is
  structurally unable to detect.

**2. Quoting sits between the check and the directory (A.3).** A path validated
as a Python string is not the path the remote shell ends up in — the shell sees
the *quoted* rendering. So quoting here is not "call ``shlex.quote`` and hope":
:func:`build_remote_command` assembles an explicit token list, quotes it, and
then re-parses the finished command line with :func:`shlex.split`, requiring
the tokens to come back byte-identical. If any quoting decision changed the
path, the round-trip fails and :class:`RemoteQuotingError` is raised instead of
a command being sent. That check is what makes "the path we validated" and "the
path the remote ``cd`` receives" the same object.

**3. Failover must not re-execute a ticket (A.2, A.3 "Startup and failover").**
SPEC A.2 permits failover to another host only "before work has meaningfully
started", and requires that a rerun after side effects "SHOULD be treated as a
new attempt, not as invisible failover". :class:`RunProgress` is a monotonic
latch that flips the moment the remote agent could first have touched anything,
and :func:`decide_failover` refuses transparent failover whenever it is set —
*regardless of how the failure is classified*. Stage classification alone is not
trusted for this, because a connectivity error can arrive mid-turn and would
otherwise look identical to a connectivity error at dial time.

Windows note (CONTRACTS house rule 7)
-------------------------------------
The orchestrator may run on Windows; the remote host is assumed POSIX. Every
path this module builds for the remote side is a :class:`~pathlib.PurePosixPath`
and every command is POSIX-shell quoted, on all platforms. The local ``ssh``
binary is invoked by name and must be on PATH (OpenSSH ships with Windows 10+).
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from symphony.agent.app_server import MAX_LINE_BYTES, AppServerClient
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
    WorkspacePathEscapesRoot,
)
from symphony.models import workspace_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator

    from symphony.agent.events import AgentEvent
    from symphony.trackers.base import ToolSpec
    from symphony.workflow.config import CodexConfig, ServiceConfig

__all__ = [
    # SPEC A — extension errors
    "SSHWorkerError",
    "NoSSHHostsConfigured",
    "SSHHostUnreachable",
    "RemotePreflightFailed",
    "RemoteQuotingError",
    "HostPoolSaturated",
    "RemoteWorkspacePathEscapesRoot",
    # SPEC 9.5 / 15.2 under remote POSIX semantics
    "normalize_remote_path",
    "assert_remote_within_root",
    "remote_workspace_path",
    "remote_workspace_root",
    "quote_remote",
    "build_remote_command",
    "build_remote_launch_command",
    "build_remote_probe_command",
    "build_remote_cleanup_command",
    # SPEC A.1 / A.2 — host pool and assignment
    "SSHHost",
    "HostAssignment",
    "HostPool",
    "ssh_enabled",
    # SPEC A.2 / A.3 — failover boundary
    "FailureStage",
    "FailoverAction",
    "FailoverDecision",
    "RunProgress",
    "classify_failure",
    "decide_failover",
    # SPEC A.1 — remote launch
    "RemoteProcess",
    "SSHTransport",
    "CommandResult",
    "RemotePreflight",
    "OpenSSHTransport",
    "RemoteAppServerClient",
    "SSHWorker",
    "DEFAULT_SSH_OPTIONS",
]


# --------------------------------------------------------------------------
# Extension error taxonomy
#
# `symphony.errors` is immutable and predates this OPTIONAL appendix, so the
# extension's categories are defined here. Every class still descends from a
# class in `errors.__all__`, so existing `except AgentError` / `except
# WorkspacePathEscapesRoot` handling in the orchestrator keeps working
# unchanged.
# --------------------------------------------------------------------------


class SSHWorkerError(AgentError):
    """Base for SPEC Appendix A failures."""

    category = "ssh_worker_error"


class NoSSHHostsConfigured(SSHWorkerError):
    """``worker.ssh_hosts`` is absent or empty; SPEC A says work runs locally."""

    category = "ssh_no_hosts_configured"


class SSHHostUnreachable(SSHWorkerError):
    """Transport-level failure: dial, auth, or a dropped connection (SPEC A.3)."""

    category = "ssh_host_unreachable"


class RemotePreflightFailed(SSHWorkerError):
    """The remote environment does not satisfy the worker contract (SPEC A.1)."""

    category = "ssh_remote_preflight_failed"


class RemoteQuotingError(SSHWorkerError):
    """A built command did not survive a quote/re-parse round trip (SPEC A.3).

    Never recoverable and never host-dependent: the same input produces the same
    failure everywhere, so :func:`decide_failover` maps it to
    :attr:`FailoverAction.FAIL` rather than burning hosts or retries.
    """

    category = "ssh_remote_quoting_error"


class HostPoolSaturated(SSHWorkerError):
    """Every configured host is at capacity or unreachable (SPEC A.2).

    Raised only when the caller supplied a wait deadline. It is *never* a signal
    to fall back to local execution — SPEC A.2 requires dispatch to wait.
    """

    category = "ssh_host_pool_saturated"


class RemoteWorkspacePathEscapesRoot(WorkspacePathEscapesRoot):
    """SPEC 9.5 Invariant 2 violated under *remote* path semantics.

    Deliberately keeps the inherited ``workspace_path_escapes_root`` category so
    the orchestrator's existing "never recoverable, never retried blindly"
    handling applies without knowing this extension exists.
    """


# --------------------------------------------------------------------------
# SPEC 9.5 / 15.2 — containment under remote POSIX semantics
# --------------------------------------------------------------------------

#: Characters that cannot be represented in an argv element or would make the
#: remote command line ambiguous no matter how it is quoted.
_UNQUOTABLE = ("\x00", "\n", "\r")

class _ShellOperator(str):
    """A token :func:`build_remote_command` emits verbatim instead of quoting.

    Operator-ness is carried by the token's *type*, never by its text. A plain
    ``"&&"`` string appearing in a token list is data and gets quoted like any
    other value, so no caller-supplied string — a path, a ``codex.command`` — can
    promote itself into shell syntax by spelling itself like an operator.
    """

    __slots__ = ()


#: The entire set of bytes in a generated command line that are not literal data.
_AND = _ShellOperator("&&")


def normalize_remote_path(path: PurePosixPath | str) -> PurePosixPath:
    """Lexically normalize a path that will be interpreted on the remote host.

    Purely lexical by design (SPEC A.1: ``workspace.root`` "is interpreted on
    the remote host, not on the orchestrator host"). Nothing here touches the
    local filesystem — no ``resolve()``, no ``exists()``, no drive-letter
    rewriting — because the orchestrator's filesystem has no authority over
    remote path meaning.

    ``..`` is rejected rather than collapsed. Collapsing is wrong in the
    presence of remote symlinks: ``/root/ws/link/..`` resolves to the link's
    parent, not to ``/root/ws``, so a lexical collapse would approve a path the
    remote shell places somewhere else entirely.
    """
    text = str(path)
    if not text:
        raise RemoteWorkspacePathEscapesRoot("remote path must not be empty", path=text)
    for bad in _UNQUOTABLE:
        if bad in text:
            raise RemoteWorkspacePathEscapesRoot(
                "remote path contains a character that cannot be safely transmitted",
                path=repr(text),
            )
    if not text.startswith("/"):
        raise RemoteWorkspacePathEscapesRoot(
            "remote path must be absolute; the remote shell's cwd is not a Symphony guarantee",
            path=text,
        )
    if text.startswith("//") and not text.startswith("///"):
        # POSIX leaves a leading "//" implementation-defined; PurePosixPath
        # preserves it as a distinct root, so two spellings of one directory
        # would compare unequal in the containment check below.
        raise RemoteWorkspacePathEscapesRoot(
            "remote path must not begin with '//' (POSIX implementation-defined root)",
            path=text,
        )
    if "~" in text:
        # Single quoting is total, so the remote shell would create a directory
        # literally named "~". Refusing beats silently diverging from intent.
        raise RemoteWorkspacePathEscapesRoot(
            "remote path must not contain '~'; tilde is not expanded inside quoting",
            path=text,
        )
    if "$" in text:
        raise RemoteWorkspacePathEscapesRoot(
            "remote path must not contain '$'; variables are not expanded inside quoting",
            path=text,
        )
    pure = PurePosixPath(text)
    if ".." in pure.parts:
        raise RemoteWorkspacePathEscapesRoot(
            "remote path must not contain '..' components",
            path=text,
        )
    return pure


def assert_remote_within_root(
    path: PurePosixPath | str,
    root: PurePosixPath | str,
) -> PurePosixPath:
    """Enforce SPEC 9.5 Invariant 2 / 15.2 against remote path semantics.

    Comparison is by path *component* and is case-sensitive, unlike
    :func:`symphony.workspace.safety.assert_within_root`, which case-folds for
    Windows. The remote host is POSIX, so case-folding here would wrongly treat
    ``/srv/WS`` and ``/srv/ws`` as the same directory.

    Containment is strict: the root itself is not inside the root. That closes
    the degenerate case where a workspace path collapsing onto the root would
    let :meth:`SSHWorker.cleanup` remove every workspace on the host.
    """
    target = normalize_remote_path(path)
    base = normalize_remote_path(root)
    tparts, bparts = target.parts, base.parts
    if len(tparts) <= len(bparts) or tparts[: len(bparts)] != bparts:
        raise RemoteWorkspacePathEscapesRoot(
            "remote workspace path is not inside the remote workspace root",
            path=str(target),
            root=str(base),
        )
    return target


def remote_workspace_path(
    root: PurePosixPath | str,
    identifier: str,
) -> PurePosixPath:
    """Remote per-issue workspace path (SPEC 9.1 layout, 9.5 Invariants 2 and 3).

    Invariant 3 sanitization is delegated to :func:`symphony.models.workspace_key`
    so local and remote runs derive the same directory name for the same issue.
    """
    key = workspace_key(identifier)
    candidate = normalize_remote_path(root) / key
    return assert_remote_within_root(candidate, root)


def remote_workspace_root(cfg: ServiceConfig) -> PurePosixPath:
    """Remote reading of ``workspace.root`` (SPEC A.1).

    Reads the *verbatim* front-matter string from ``cfg.raw`` rather than
    ``cfg.workspace_root``. ``ServiceConfig.workspace_root`` is a local
    :class:`~pathlib.Path` that has already been made absolute against the
    orchestrator's own filesystem and, on Windows, re-rendered with backslashes
    and a drive letter. Neither transformation is meaningful on the remote host.
    """
    raw = getattr(cfg, "raw", None)
    section = raw.get("workspace") if isinstance(raw, Mapping) else None
    value = section.get("root") if isinstance(section, Mapping) else None
    if isinstance(value, str) and value.strip():
        return normalize_remote_path(value.strip())
    raise RemotePreflightFailed(
        "workspace.root must be an absolute remote POSIX path when worker.ssh_hosts is set",
        configured=None if value is None else str(value),
    )


def quote_remote(value: str) -> str:
    """POSIX single-quote *value* for a remote shell.

    :func:`shlex.quote` is pure string manipulation and always emits POSIX
    quoting, so this behaves identically on a Windows orchestrator.
    """
    for bad in _UNQUOTABLE:
        if bad in value:
            raise RemoteQuotingError(
                "value contains a character that cannot be quoted for a remote shell",
                value=repr(value),
            )
    return shlex.quote(value)


def build_remote_command(tokens: Sequence[str]) -> str:
    """Quote *tokens* into one remote command line and verify the round trip.

    This is the mitigation for the SPEC A.3 hazard that shell quoting sits
    between a path check and the directory the shell actually enters. The
    finished line is re-parsed with :func:`shlex.split` and required to yield
    the original token list byte-for-byte. If quoting altered any token — a path
    with a quote, an embedded operator, an unbalanced escape — the mismatch
    surfaces as :class:`RemoteQuotingError` before anything is sent, instead of
    a remote shell being handed a command whose meaning differs from the one
    that was validated.

    ``ssh`` hands its command argument to the remote login shell, which parses
    it exactly once, so one round of quoting is the correct amount.
    """
    if not tokens:
        raise RemoteQuotingError("remote command must have at least one token")
    rendered = " ".join(t if isinstance(t, _ShellOperator) else quote_remote(t) for t in tokens)
    try:
        reparsed = shlex.split(rendered)
    except ValueError as exc:
        raise RemoteQuotingError(
            "generated remote command line does not parse as a POSIX command",
            error=str(exc),
        ) from exc
    if reparsed != list(tokens):
        raise RemoteQuotingError(
            "generated remote command line does not re-parse to the validated tokens",
            expected=list(tokens),
            parsed=reparsed,
        )
    return rendered


def build_remote_launch_command(workspace: PurePosixPath | str, command: str) -> str:
    """SPEC 10.1 launch, expressed for a remote shell.

    Renders ``cd -- <workspace> && exec bash -lc <codex.command>``.

    * ``cd --`` keeps a workspace path that begins with ``-`` from being read as
      an option (paths are already validated absolute, so this is belt-and-braces).
    * ``exec`` replaces the remote login shell with the agent, so closing the SSH
      channel reaches the agent rather than an intermediate shell.
    * ``bash -lc <command>`` is SPEC 10.1's invocation, honored literally.
    """
    ws = str(normalize_remote_path(workspace))
    if not command or not command.strip():
        raise RemoteQuotingError("codex.command must be a non-empty string")
    return build_remote_command(["cd", "--", ws, _AND, "exec", "bash", "-lc", command])


def build_remote_probe_command(
    workspace: PurePosixPath | str,
    root: PurePosixPath | str,
) -> str:
    """Command whose output is the remote host's own resolution of root and workspace.

    Prints two lines — resolved root, then resolved workspace — using ``pwd -P``,
    which is symlink-resolved. :meth:`SSHWorker.preflight` re-runs the
    containment check on those two values, which is the only way to detect a
    remote symlink that carries the workspace outside the root (SPEC A.3
    "Remote path resolution ... matter more once execution crosses a machine
    boundary").

    ``test -w .`` asserts the writable-workspace-root half of the SPEC A.1
    remote worker contract. The command body contains no substitutions, so every
    token stays literal data and :func:`build_remote_command` can verify it.
    """
    ws = str(assert_remote_within_root(workspace, root))
    base = str(normalize_remote_path(root))
    return build_remote_command(
        [
            "cd", "--", base, _AND, "test", "-w", ".", _AND, "pwd", "-P",
            _AND, "cd", "--", ws, _AND, "test", "-w", ".", _AND, "pwd", "-P",
        ]
    )  # fmt: skip


def build_remote_cleanup_command(
    workspace: PurePosixPath | str,
    root: PurePosixPath | str,
) -> str:
    """Remote workspace removal, gated on containment (SPEC 9.5, A.3 cleanup).

    Containment is asserted before the string is built, so a path that is not
    strictly inside the remote root never reaches a recursive remove.
    """
    ws = str(assert_remote_within_root(workspace, root))
    return build_remote_command(["rm", "-rf", "--", ws])


# --------------------------------------------------------------------------
# SPEC A.1 / A.2 — host pool and assignment
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SSHHost:
    """One parsed ``worker.ssh_hosts`` entry (SPEC A, extension config)."""

    spec: str
    hostname: str
    user: str | None = None
    port: int | None = None

    @classmethod
    def parse(cls, spec: str) -> SSHHost:
        """Parse ``host``, ``user@host``, ``host:port``, ``user@[::1]:port``.

        A destination beginning with ``-`` is rejected. ``ssh`` would otherwise
        read it as an option, which turns a workflow-file host entry into
        arbitrary local ``ssh`` configuration (``-oProxyCommand=...``).
        """
        text = (spec or "").strip()
        if not text:
            raise RemotePreflightFailed("ssh host entry must be a non-empty string")
        if text.startswith("-"):
            raise RemotePreflightFailed(
                "ssh host entry must not begin with '-' (would be parsed as an ssh option)",
                host=text,
            )
        for bad in (*_UNQUOTABLE, " ", "\t"):
            if bad in text:
                raise RemotePreflightFailed(
                    "ssh host entry must not contain whitespace or control characters",
                    host=repr(text),
                )

        user: str | None = None
        remainder = text
        if "@" in remainder:
            user, remainder = remainder.rsplit("@", 1)
            if not user or not remainder:
                raise RemotePreflightFailed("malformed ssh host entry", host=text)

        port: int | None = None
        if remainder.startswith("["):
            closing = remainder.find("]")
            if closing < 0:
                raise RemotePreflightFailed("unterminated '[' in ssh host entry", host=text)
            hostname = remainder[1:closing]
            tail = remainder[closing + 1 :]
            if tail.startswith(":"):
                port = _parse_port(tail[1:], text)
            elif tail:
                raise RemotePreflightFailed("trailing text after ']' in ssh host", host=text)
        elif remainder.count(":") == 1:
            hostname, _, tail = remainder.partition(":")
            port = _parse_port(tail, text)
        else:
            hostname = remainder  # bare IPv6 or an ssh_config alias

        if not hostname:
            raise RemotePreflightFailed("ssh host entry has no hostname", host=text)
        return cls(spec=text, hostname=hostname, user=user, port=port)

    @property
    def destination(self) -> str:
        """The ``[user@]host`` argument passed to ``ssh`` (port travels via ``-p``)."""
        return f"{self.user}@{self.hostname}" if self.user else self.hostname

    def __str__(self) -> str:
        return self.spec


def _parse_port(text: str, spec: str) -> int:
    if not text.isdigit():
        raise RemotePreflightFailed("ssh host port must be numeric", host=spec)
    port = int(text)
    if not 1 <= port <= 65535:
        raise RemotePreflightFailed("ssh host port out of range", host=spec, port=port)
    return port


@dataclass(frozen=True, slots=True)
class HostAssignment:
    """A run's execution identity: host plus remote workspace (SPEC A.1).

    SPEC A.1: "Each worker run is assigned to one host at a time, and that host
    becomes part of the run's effective execution identity along with the issue
    workspace." Continuation turns within one worker lifetime reuse this object,
    which is what keeps them on the same host and workspace (SPEC A.1, last
    bullet) — there is no per-turn reassignment path.
    """

    host: SSHHost
    issue_identifier: str
    workspace_path: PurePosixPath
    remote_root: PurePosixPath
    attempt: int | None = None
    acquired_at: datetime | None = None

    @property
    def host_spec(self) -> str:
        return self.host.spec

    def to_dict(self) -> dict[str, Any]:
        """Flat, named rendering for logs and the SPEC 13.3 snapshot (A.3)."""
        return {
            "host": self.host.spec,
            "hostname": self.host.hostname,
            "port": self.host.port,
            "issue_identifier": self.issue_identifier,
            "workspace_path": str(self.workspace_path),
            "remote_root": str(self.remote_root),
            "attempt": self.attempt,
            "acquired_at": None if self.acquired_at is None else self.acquired_at.isoformat(),
        }


@dataclass(slots=True)
class _HostState:
    host: SSHHost
    in_use: int = 0
    reachable: bool = True
    last_error: str | None = None


class HostPool:
    """Pool of SSH destinations with an OPTIONAL shared per-host cap (SPEC A.2).

    Capacity, not fallback. SPEC A.2: "When all SSH hosts are at capacity,
    dispatch SHOULD wait rather than silently falling back to a different
    execution mode", and SPEC A.3: "A dead or overloaded host SHOULD reduce
    available capacity, not cause duplicate execution or an accidental fallback
    to local work." Accordingly :meth:`try_acquire` returns ``None`` and
    :meth:`acquire` blocks; neither ever yields a local execution mode, and
    :meth:`mark_unreachable` shrinks the pool rather than diverting work.

    This pool caps *per host*. The global ``agent.max_concurrent_agents`` and
    the per-state limits stay with the orchestrator (SPEC 8.3); nothing here
    second-guesses them.
    """

    def __init__(
        self,
        hosts: Iterable[SSHHost | str] = (),
        *,
        max_concurrent_agents_per_host: int | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = [h if isinstance(h, SSHHost) else SSHHost.parse(h) for h in hosts]
        seen: set[str] = set()
        self._states: dict[str, _HostState] = {}
        for host in parsed:
            if host.spec in seen:
                continue  # duplicate entries must not double a host's capacity
            seen.add(host.spec)
            self._states[host.spec] = _HostState(host=host)
        cap = max_concurrent_agents_per_host
        self.max_concurrent_agents_per_host = cap if cap is not None and cap > 0 else None
        self._now = now or (lambda: datetime.now(UTC))
        self._condition = asyncio.Condition()

    # -- introspection (flat and named for the RLM driver) -----------------

    @classmethod
    def from_config(cls, cfg: ServiceConfig, **kwargs: Any) -> HostPool:
        """Build from ``worker.ssh_hosts`` / ``worker.max_concurrent_agents_per_host``.

        Always returns a pool. When ``ssh_hosts`` is omitted the pool is simply
        disabled, which keeps "SSH is configured" a single introspectable
        predicate instead of a ``None`` check scattered across callers.
        """
        return cls(
            getattr(cfg, "ssh_hosts", ()) or (),
            max_concurrent_agents_per_host=getattr(cfg, "max_concurrent_agents_per_host", None),
            **kwargs,
        )

    @property
    def enabled(self) -> bool:
        """``False`` when ``worker.ssh_hosts`` is omitted — SPEC A: work runs locally."""
        return bool(self._states)

    @property
    def hosts(self) -> tuple[SSHHost, ...]:
        return tuple(state.host for state in self._states.values())

    def in_use(self, host: SSHHost | str) -> int:
        return self._state(host).in_use

    def is_reachable(self, host: SSHHost | str) -> bool:
        return self._state(host).reachable

    def has_capacity(self, host: SSHHost | str) -> bool:
        state = self._state(host)
        if not state.reachable:
            return False
        cap = self.max_concurrent_agents_per_host
        return cap is None or state.in_use < cap

    def available_hosts(self) -> list[SSHHost]:
        """Reachable hosts below the per-host cap, in config order."""
        return [s.host for s in self._states.values() if self.has_capacity(s.host)]

    @property
    def saturated(self) -> bool:
        """True when no host can take work — dispatch waits (SPEC A.2)."""
        return self.enabled and not self.available_hosts()

    def snapshot(self) -> dict[str, Any]:
        """Operator view of host ownership and capacity (SPEC A.3 observability)."""
        return {
            "enabled": self.enabled,
            "max_concurrent_agents_per_host": self.max_concurrent_agents_per_host,
            "saturated": self.saturated,
            "hosts": [
                {
                    "host": s.host.spec,
                    "in_use": s.in_use,
                    "reachable": s.reachable,
                    "has_capacity": self.has_capacity(s.host),
                    "last_error": s.last_error,
                }
                for s in self._states.values()
            ],
        }

    # -- assignment --------------------------------------------------------

    def select(self, *, prefer: SSHHost | str | None = None) -> SSHHost | None:
        """Choose a host without reserving it. ``None`` means saturated.

        Prefers *prefer* when it still has capacity — SPEC A.2: "Implementations
        MAY prefer the previously used host on retries when that host is still
        available." Because remote workspaces are host-local (SPEC A.3
        "Workspace locality"), staying put is the difference between a warm
        workspace and a cold restart. Otherwise the least-loaded host wins, with
        config order as a deterministic tie-break.
        """
        if prefer is not None:
            spec = prefer.spec if isinstance(prefer, SSHHost) else str(prefer)
            state = self._states.get(spec)
            if state is not None and self.has_capacity(state.host):
                return state.host
        candidates = [s for s in self._states.values() if self.has_capacity(s.host)]
        if not candidates:
            return None
        return min(candidates, key=lambda s: s.in_use).host

    def try_acquire(
        self,
        issue_identifier: str,
        *,
        remote_root: PurePosixPath | str,
        attempt: int | None = None,
        prefer: SSHHost | str | None = None,
        workspace_path: PurePosixPath | str | None = None,
    ) -> HostAssignment | None:
        """Reserve a host slot, or return ``None`` when every host is at capacity.

        ``None`` means "leave this issue unclaimed on this tick" — the SPEC A.2
        wait. It never means "run locally".
        """
        self._require_enabled()
        host = self.select(prefer=prefer)
        if host is None:
            return None
        root = normalize_remote_path(remote_root)
        path = (
            remote_workspace_path(root, issue_identifier)
            if workspace_path is None
            else assert_remote_within_root(workspace_path, root)
        )
        self._states[host.spec].in_use += 1
        return HostAssignment(
            host=host,
            issue_identifier=issue_identifier,
            workspace_path=path,
            remote_root=root,
            attempt=attempt,
            acquired_at=self._now(),
        )

    async def acquire(
        self,
        issue_identifier: str,
        *,
        remote_root: PurePosixPath | str,
        attempt: int | None = None,
        prefer: SSHHost | str | None = None,
        workspace_path: PurePosixPath | str | None = None,
        timeout_ms: int | None = None,
    ) -> HostAssignment:
        """Reserve a host slot, waiting while the pool is saturated (SPEC A.2).

        With ``timeout_ms=None`` this waits indefinitely, which is the literal
        reading of "dispatch SHOULD wait". A deadline is offered for operators
        who prefer a bounded tick; exceeding it raises
        :class:`HostPoolSaturated`, never a local fallback.
        """
        self._require_enabled()

        async def _wait() -> HostAssignment:
            async with self._condition:
                while True:
                    assignment = self.try_acquire(
                        issue_identifier,
                        remote_root=remote_root,
                        attempt=attempt,
                        prefer=prefer,
                        workspace_path=workspace_path,
                    )
                    if assignment is not None:
                        return assignment
                    await self._condition.wait()

        if timeout_ms is None:
            return await _wait()
        try:
            async with asyncio.timeout(max(timeout_ms, 0) / 1000.0):
                return await _wait()
        except TimeoutError as exc:
            raise HostPoolSaturated(
                "every configured ssh host is at capacity or unreachable",
                timeout_ms=timeout_ms,
                hosts=[s.host.spec for s in self._states.values()],
            ) from exc

    async def release(self, assignment: HostAssignment) -> None:
        """Return a slot and wake one waiter. Idempotent per assignment object."""
        state = self._states.get(assignment.host.spec)
        if state is None:
            return
        async with self._condition:
            if state.in_use > 0:
                state.in_use -= 1
            self._condition.notify_all()

    async def mark_unreachable(self, host: SSHHost | str, error: str | None = None) -> None:
        """Take a host out of rotation: reduce capacity, never divert (SPEC A.3)."""
        state = self._state(host)
        state.reachable = False
        state.last_error = error
        async with self._condition:
            self._condition.notify_all()

    async def mark_reachable(self, host: SSHHost | str) -> None:
        """Return a host to rotation and wake anyone waiting on capacity."""
        state = self._state(host)
        state.reachable = True
        state.last_error = None
        async with self._condition:
            self._condition.notify_all()

    @asynccontextmanager
    async def lease(
        self,
        issue_identifier: str,
        *,
        remote_root: PurePosixPath | str,
        attempt: int | None = None,
        prefer: SSHHost | str | None = None,
        timeout_ms: int | None = None,
    ) -> AsyncIterator[HostAssignment]:
        """Acquire for the duration of a worker lifetime, releasing on exit.

        The whole ``with`` body is one worker lifetime, so continuation turns
        inside it necessarily reuse the same assignment (SPEC A.1).
        """
        assignment = await self.acquire(
            issue_identifier,
            remote_root=remote_root,
            attempt=attempt,
            prefer=prefer,
            timeout_ms=timeout_ms,
        )
        try:
            yield assignment
        finally:
            await self.release(assignment)

    # -- internals ---------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise NoSSHHostsConfigured(
                "worker.ssh_hosts is not configured; SPEC Appendix A runs work locally"
            )

    def _state(self, host: SSHHost | str) -> _HostState:
        spec = host.spec if isinstance(host, SSHHost) else str(host)
        state = self._states.get(spec)
        if state is None:
            raise RemotePreflightFailed("host is not a member of this pool", host=spec)
        return state


def ssh_enabled(cfg: ServiceConfig) -> bool:
    """``True`` only when ``worker.ssh_hosts`` is non-empty (SPEC A, preamble).

    The single predicate callers should branch on so that omitting the key
    leaves local execution untouched.
    """
    return bool(getattr(cfg, "ssh_hosts", ()) or ())


# --------------------------------------------------------------------------
# SPEC A.2 / A.3 — the failover boundary
# --------------------------------------------------------------------------


class FailureStage(StrEnum):
    """Where a remote run died (SPEC A.3 "Startup and failover semantics").

    The distinction SPEC A.3 asks for is between host-connectivity/startup
    failures and in-workspace agent failures, "so the same ticket is not
    accidentally re-executed on multiple hosts".
    """

    CONNECT = "connect"
    """SSH dial, auth, or channel loss. Nothing in the workspace ran *because of
    this error* — but see :class:`RunProgress`; a mid-turn drop lands here too."""

    PREFLIGHT = "preflight"
    """The remote environment failed its checks: root missing, not writable,
    containment violated. Host-specific, workspace untouched."""

    STARTUP = "startup"
    """The app-server failed to launch or to reach a live thread: command not
    found, initialize timeout, exit before the first turn."""

    AGENT = "agent"
    """An in-workspace agent failure: the turn ran and failed, timed out, was
    cancelled, or demanded input. The workspace has been touched."""

    UNKNOWN = "unknown"
    """Unclassified. Treated as unsafe for failover — the conservative default."""


class FailoverAction(StrEnum):
    """What the orchestrator should do next (SPEC A.2)."""

    RETRY_OTHER_HOST = "retry_other_host"
    """Transparent re-dispatch to a different host, same attempt number. Only
    legal "before work has meaningfully started" (SPEC A.2)."""

    NEW_ATTEMPT = "new_attempt"
    """Hand back to the orchestrator's SPEC 8.4 retry ladder as a *visible* new
    attempt. SPEC A.2: after side effects, a rerun elsewhere "SHOULD be treated
    as a new attempt, not as invisible failover"."""

    FAIL = "fail"
    """Host-independent and unrecoverable; another host would fail identically."""


@dataclass(slots=True)
class RunProgress:
    """Monotonic side-effect latch for one remote worker lifetime (SPEC A.2).

    Exists to answer exactly one question: has this run "meaningfully started"
    (SPEC A.2)? The latch is one-way. Once anything on the remote host could
    have produced an effect that a second execution would duplicate — a repo
    push, a tracker write, a deploy — it can never be cleared, so no later
    classification can talk :func:`decide_failover` into a transparent rerun.

    Latch points, and why each is where it is:

    * :meth:`mark_workspace_prepared` does **not** latch. Creating or populating
      a remote workspace is host-local and idempotent; SPEC A.3 already calls
      moving hosts "a cold restart", which is a cost, not a correctness problem.
    * :meth:`mark_hook_started` latches. Hooks are repo-owned scripts (SPEC 9.4,
      15.4) and may push, deploy, or notify. Once one starts, it is unknowable
      whether it finished.
    * :meth:`mark_turn_dispatched` latches, and is called *before* the prompt is
      written to the wire, not after a response. A ``sendMessage`` that never
      returns is precisely the case where the agent may be working right now.
    """

    issue_identifier: str
    host_spec: str
    workspace_path: str
    side_effects_possible: bool = False
    side_effect_reason: str | None = None
    workspace_prepared: bool = False
    turns_dispatched: int = 0

    def mark_workspace_prepared(self) -> None:
        """Remote workspace exists and is populated. Deliberately does not latch."""
        self.workspace_prepared = True

    def mark_hook_started(self, name: str) -> None:
        """A repo-owned hook began executing on the remote host (SPEC 9.4)."""
        self.mark_side_effect(f"hook_started:{name}")

    def mark_turn_dispatched(self, turn_number: int) -> None:
        """About to send a turn prompt. Call before the write, never after."""
        self.turns_dispatched = max(self.turns_dispatched, turn_number)
        self.mark_side_effect(f"turn_dispatched:{turn_number}")

    def mark_side_effect(self, reason: str) -> None:
        """Latch. The first reason is kept; the latch never clears."""
        if not self.side_effects_possible:
            self.side_effects_possible = True
            self.side_effect_reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_identifier": self.issue_identifier,
            "host": self.host_spec,
            "workspace_path": self.workspace_path,
            "side_effects_possible": self.side_effects_possible,
            "side_effect_reason": self.side_effect_reason,
            "workspace_prepared": self.workspace_prepared,
            "turns_dispatched": self.turns_dispatched,
        }


@dataclass(frozen=True, slots=True)
class FailoverDecision:
    """Recommendation returned to the orchestrator, which owns retries (SPEC A.1)."""

    action: FailoverAction
    stage: FailureStage
    reason: str
    side_effects_possible: bool
    error_category: str | None = None

    @property
    def may_switch_host(self) -> bool:
        return self.action is FailoverAction.RETRY_OTHER_HOST

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "stage": self.stage.value,
            "reason": self.reason,
            "side_effects_possible": self.side_effects_possible,
            "error_category": self.error_category,
        }


#: Errors that mean "the app-server never reached a live thread". `PortExit`
#: appears here because a subprocess exit is only *startup* when nothing has
#: been dispatched; the `RunProgress` latch, not this table, decides that.
_STARTUP_ERRORS: tuple[type[BaseException], ...] = (
    CodexNotFound,
    InvalidWorkspaceCwd,
    PortExit,
    ResponseError,
    ResponseTimeout,
)

_AGENT_ERRORS: tuple[type[BaseException], ...] = (
    TurnFailed,
    TurnTimeout,
    TurnCancelled,
    TurnInputRequired,
)

_HOST_INDEPENDENT_ERRORS: tuple[type[BaseException], ...] = (
    WorkspacePathEscapesRoot,
    RemoteQuotingError,
)


def classify_failure(exc: BaseException) -> FailureStage:
    """Map an exception to a SPEC A.3 failure stage.

    Classification is a *hint*, not the safety boundary. Some errors are
    genuinely ambiguous — a dropped SSH channel looks the same whether it
    happened while dialing or halfway through a turn — so
    :func:`decide_failover` consults :class:`RunProgress` first and lets the
    latch override any stage. Conflating those two cases is exactly the failure
    SPEC A.3 warns about, and it is closed by the latch rather than by trying to
    make classification cleverer.
    """
    if isinstance(exc, _HOST_INDEPENDENT_ERRORS):
        return FailureStage.PREFLIGHT
    if isinstance(exc, SSHHostUnreachable):
        return FailureStage.CONNECT
    if isinstance(exc, RemotePreflightFailed):
        return FailureStage.PREFLIGHT
    if isinstance(exc, _AGENT_ERRORS):
        return FailureStage.AGENT
    if isinstance(exc, _STARTUP_ERRORS):
        return FailureStage.STARTUP
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return FailureStage.CONNECT
    if isinstance(exc, FileNotFoundError):
        return FailureStage.CONNECT  # local `ssh` binary missing
    if isinstance(exc, OSError):
        return FailureStage.CONNECT
    return FailureStage.UNKNOWN


#: Stages where no in-workspace execution has begun, so SPEC A.2's "before work
#: has meaningfully started" holds and a transparent host switch is legal.
_FAILOVER_ELIGIBLE_STAGES = frozenset(
    {FailureStage.CONNECT, FailureStage.PREFLIGHT, FailureStage.STARTUP}
)


def decide_failover(
    progress: RunProgress,
    exc: BaseException,
    *,
    hosts_remaining: int,
) -> FailoverDecision:
    """Decide whether another host may run this ticket transparently (SPEC A.2).

    The rules, in the order they are applied:

    1. **Host-independent errors fail outright.** A containment violation or a
       quoting failure reproduces identically everywhere; retrying it burns
       hosts and attempts for nothing, and
       :class:`~symphony.errors.WorkspacePathEscapesRoot` is documented as never
       retried blindly.
    2. **The latch dominates.** If ``progress.side_effects_possible`` is set, the
       answer is :attr:`FailoverAction.NEW_ATTEMPT` no matter what stage the
       error is classified as. This is the rule that stops a connectivity error
       arriving mid-turn from being mistaken for a connectivity error at dial
       time and re-running a ticket that is still executing on the first host.
    3. **Agent-stage failures are side effects even if the latch was missed.**
       Belt-and-braces: reaching an agent error means a turn ran.
    4. **Pre-side-effect connect/preflight/startup failures may switch hosts**,
       provided another host is actually available.
    5. **Everything else, including ``UNKNOWN``, becomes a new attempt.** Never a
       silent rerun.
    """
    stage = classify_failure(exc)
    category = getattr(exc, "category", None)

    def decision(action: FailoverAction, reason: str) -> FailoverDecision:
        return FailoverDecision(
            action=action,
            stage=stage,
            reason=reason,
            side_effects_possible=progress.side_effects_possible,
            error_category=category if isinstance(category, str) else None,
        )

    if isinstance(exc, _HOST_INDEPENDENT_ERRORS):
        return decision(FailoverAction.FAIL, "host_independent_failure")
    if progress.side_effects_possible:
        return decision(
            FailoverAction.NEW_ATTEMPT,
            f"side_effects_possible:{progress.side_effect_reason}",
        )
    if stage is FailureStage.AGENT:
        return decision(FailoverAction.NEW_ATTEMPT, "agent_failure_implies_side_effects")
    if stage in _FAILOVER_ELIGIBLE_STAGES:
        if hosts_remaining > 0:
            return decision(FailoverAction.RETRY_OTHER_HOST, f"pre_side_effect_{stage.value}")
        return decision(FailoverAction.NEW_ATTEMPT, "no_alternate_host_available")
    return decision(FailoverAction.NEW_ATTEMPT, "unclassified_failure")


# --------------------------------------------------------------------------
# SPEC A.1 — remote app-server launch over SSH stdio
# --------------------------------------------------------------------------

#: Non-interactive, fail-fast defaults. ``BatchMode`` keeps a password prompt
#: from stalling a worker forever; the keepalives turn a silently dead host into
#: a prompt CONNECT failure instead of an indefinite hang (SPEC A.3 host health).
DEFAULT_SSH_OPTIONS: tuple[str, ...] = (
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
)  # fmt: skip


class RemoteProcess(Protocol):
    """The subset of :class:`asyncio.subprocess.Process` the app-server client uses."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdin(self) -> Any: ...

    @property
    def stdout(self) -> Any: ...

    @property
    def stderr(self) -> Any: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a one-shot remote command (used for preflight and cleanup)."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SSHTransport(Protocol):
    """Injectable SSH transport. Tests substitute a fake; nothing here dials out."""

    async def spawn(self, host: SSHHost, command: str, *, env: Mapping[str, str]) -> RemoteProcess:
        """Start *command* on *host* with stdio pipes attached (SPEC A.1)."""
        ...

    async def run(self, host: SSHHost, command: str, *, timeout_ms: int) -> CommandResult:
        """Run *command* to completion and capture its output."""
        ...


class OpenSSHTransport:
    """Default transport: the local ``ssh`` client, one process per session.

    SPEC A.1: "The coding-agent app-server is launched over SSH stdio instead of
    as a local subprocess, so the orchestrator still owns the session lifecycle
    even though commands execute remotely." The returned handle satisfies
    :class:`RemoteProcess`, so :class:`~symphony.agent.app_server.AppServerClient`
    drives the remote session with the same code path it uses locally.

    Local process environment is passed to ``ssh`` (it needs ``HOME`` and
    ``SSH_AUTH_SOCK``) but is *not* forwarded to the remote command: the built
    command line carries no environment assignments. That is stricter than SPEC
    15.3 requires and removes the question of tracker credentials reaching a
    remote child entirely.
    """

    def __init__(
        self,
        *,
        ssh_command: str = "ssh",
        options: Sequence[str] = DEFAULT_SSH_OPTIONS,
    ) -> None:
        self.ssh_command = ssh_command
        self.options = tuple(options)

    def argv(self, host: SSHHost, command: str) -> list[str]:
        """Full local ``ssh`` argv. The destination is never option-shaped."""
        argv = [self.ssh_command, *self.options]
        if host.port is not None:
            argv += ["-p", str(host.port)]
        argv.append(host.destination)
        argv.append(command)
        return argv

    async def spawn(
        self,
        host: SSHHost,
        command: str,
        *,
        env: Mapping[str, str],
    ) -> RemoteProcess:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv(host, command),
                env=dict(env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_LINE_BYTES,  # SPEC 10.1 RECOMMENDED 10 MB line budget
            )
        except FileNotFoundError as exc:
            raise SSHHostUnreachable(
                "could not launch the local ssh client",
                host=host.spec,
                ssh_command=self.ssh_command,
            ) from exc
        except OSError as exc:
            raise SSHHostUnreachable(
                "failed to start an ssh session", host=host.spec, error=str(exc)
            ) from exc
        return proc

    async def run(self, host: SSHHost, command: str, *, timeout_ms: int) -> CommandResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv(host, command),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_LINE_BYTES,
            )
        except (FileNotFoundError, OSError) as exc:
            raise SSHHostUnreachable(
                "failed to start an ssh session", host=host.spec, error=str(exc)
            ) from exc
        try:
            async with asyncio.timeout(max(timeout_ms, 0) / 1000.0):
                out, err = await proc.communicate()
        except TimeoutError as exc:
            proc.kill()
            raise SSHHostUnreachable(
                "ssh command timed out", host=host.spec, timeout_ms=timeout_ms
            ) from exc
        return CommandResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )


@dataclass(frozen=True, slots=True)
class RemotePreflight:
    """Result of the SPEC A.1 remote worker-contract check.

    ``resolved_root`` and ``resolved_workspace`` are the remote host's own
    ``pwd -P`` output, i.e. after remote symlink resolution. Containment is
    re-asserted on these values by :meth:`SSHWorker.preflight`.
    """

    host: SSHHost
    resolved_root: PurePosixPath
    resolved_workspace: PurePosixPath

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host.spec,
            "resolved_root": str(self.resolved_root),
            "resolved_workspace": str(self.resolved_workspace),
        }


class RemoteAppServerClient(AppServerClient):
    """:class:`AppServerClient` bound to a remote workspace (SPEC A.1, 10.1).

    Two overrides, both forced by the machine boundary:

    * ``self.workspace`` is replaced with a :class:`~pathlib.PurePosixPath`.
      The base class stores ``Path(workspace)``, and every protocol payload
      sends ``str(self.workspace)``; on a Windows orchestrator that would put
      ``\\srv\\ws\\ABC-1`` on the wire for a POSIX host.
    * :meth:`_assert_launch_cwd` enforces SPEC 9.5 Invariant 1 remotely. The base
      implementation calls ``Path.is_dir()``, which asks the *orchestrator's*
      filesystem whether a *remote* directory exists — a question it cannot
      answer. Existence and writability are established instead by
      :meth:`SSHWorker.preflight`; this override enforces the containment half.

    Everything else — transport framing, turn state machine, timeout mapping,
    approval policy, SPEC 15.3 secret stripping — is inherited unchanged.
    """

    def __init__(
        self,
        cfg: CodexConfig,
        *,
        assignment: HostAssignment,
        transport: SSHTransport,
        tool_specs: list[ToolSpec],
        tool_executor: Any,
        on_event: Callable[[AgentEvent], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(
            cfg,
            workspace=Path(str(assignment.workspace_path)),
            tool_specs=tool_specs,
            tool_executor=tool_executor,
            on_event=on_event,
            spawn=_make_remote_spawn(assignment, transport, cfg),
            **kwargs,
        )
        # Must follow super().__init__, which normalizes to a local Path. The
        # deliberate type variance (PurePosixPath where the base declares Path)
        # is the whole point: the base only ever calls str() on this attribute,
        # and a local Path would render POSIX remote paths with backslashes on a
        # Windows orchestrator. PurePosixPath also has no filesystem methods,
        # which makes an accidental local existence check a type error rather
        # than a silently wrong answer about a remote directory.
        self.workspace = assignment.workspace_path  # type: ignore[assignment]
        self.assignment = assignment
        self.transport = transport

    def launch_argv(self) -> list[str]:
        """The remote command line; SPEC 10.1's ``bash -lc`` invocation, remotely."""
        return [build_remote_launch_command(self.assignment.workspace_path, self.cfg.command)]

    def _assert_launch_cwd(self) -> None:
        """SPEC 9.5 Invariant 1 / 15.2, evaluated in remote terms."""
        try:
            assert_remote_within_root(self.assignment.workspace_path, self.assignment.remote_root)
        except WorkspacePathEscapesRoot as exc:
            raise InvalidWorkspaceCwd(
                "remote agent cwd is not inside the remote workspace root",
                workspace=str(self.assignment.workspace_path),
                root=str(self.assignment.remote_root),
                host=self.assignment.host.spec,
            ) from exc


def _make_remote_spawn(
    assignment: HostAssignment,
    transport: SSHTransport,
    cfg: CodexConfig,
) -> Callable[[list[str], Any, dict[str, str]], Any]:
    """Adapt :class:`SSHTransport` to the ``AppServerClient`` spawn seam.

    The base client passes ``(argv, cwd, env)``. ``cwd`` is ignored here: the
    working directory is already fixed inside the remote command line by
    ``cd -- <workspace>``, and it is validated there against remote semantics,
    which a local ``cwd=`` could not be.
    """

    async def spawn(argv: list[str], cwd: Any, env: dict[str, str]) -> RemoteProcess:
        del cwd
        if argv:
            command = argv[0]
        else:  # pragma: no cover - launch_argv always supplies the command
            command = build_remote_launch_command(assignment.workspace_path, cfg.command)
        return await transport.spawn(assignment.host, command, env=env)

    return spawn


class SSHWorker:
    """Façade over pool, remote path safety, preflight, and remote launch (SPEC A).

    Holds no scheduling authority (SPEC A.1). It hands out host assignments,
    validates remote paths, checks the remote worker contract, builds
    :class:`RemoteAppServerClient` instances, and cleans up on the owning host.
    Polling, claims, retries, and reconciliation stay with the orchestrator.
    """

    def __init__(
        self,
        cfg: ServiceConfig,
        *,
        pool: HostPool | None = None,
        transport: SSHTransport | None = None,
        remote_root: PurePosixPath | str | None = None,
        secret_env_names: Sequence[str] = (),
        preflight_timeout_ms: int = 30_000,
        cleanup_timeout_ms: int = 60_000,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.pool = pool if pool is not None else HostPool.from_config(cfg, now=now)
        self.transport: SSHTransport = transport if transport is not None else OpenSSHTransport()
        self.secret_env_names = tuple(secret_env_names)
        self.preflight_timeout_ms = preflight_timeout_ms
        self.cleanup_timeout_ms = cleanup_timeout_ms
        self._remote_root: PurePosixPath | None = (
            normalize_remote_path(remote_root) if remote_root is not None else None
        )

    @property
    def enabled(self) -> bool:
        """``False`` when ``worker.ssh_hosts`` is omitted (SPEC A, preamble)."""
        return self.pool.enabled

    @property
    def remote_root(self) -> PurePosixPath:
        """``workspace.root`` as the remote host will read it (SPEC A.1)."""
        if self._remote_root is None:
            self._remote_root = remote_workspace_root(self.cfg)
        return self._remote_root

    # -- assignment --------------------------------------------------------

    def try_assign(
        self,
        issue_identifier: str,
        *,
        attempt: int | None = None,
        prefer: SSHHost | str | None = None,
    ) -> HostAssignment | None:
        """Reserve a host, or ``None`` when saturated — dispatch waits (SPEC A.2)."""
        return self.pool.try_acquire(
            issue_identifier,
            remote_root=self.remote_root,
            attempt=attempt,
            prefer=prefer,
        )

    async def assign(
        self,
        issue_identifier: str,
        *,
        attempt: int | None = None,
        prefer: SSHHost | str | None = None,
        timeout_ms: int | None = None,
    ) -> HostAssignment:
        """Reserve a host, waiting while every host is at capacity (SPEC A.2)."""
        return await self.pool.acquire(
            issue_identifier,
            remote_root=self.remote_root,
            attempt=attempt,
            prefer=prefer,
            timeout_ms=timeout_ms,
        )

    async def release(self, assignment: HostAssignment) -> None:
        """Return the host slot at the end of a worker lifetime."""
        await self.pool.release(assignment)

    def progress_for(self, assignment: HostAssignment) -> RunProgress:
        """Fresh side-effect latch for one worker lifetime (SPEC A.2)."""
        return RunProgress(
            issue_identifier=assignment.issue_identifier,
            host_spec=assignment.host.spec,
            workspace_path=str(assignment.workspace_path),
        )

    # -- remote worker contract -------------------------------------------

    async def preflight(self, assignment: HostAssignment) -> RemotePreflight:
        """Verify the remote worker contract and re-check containment (SPEC A.1, A.3).

        The lexical containment check already ran when the assignment was built.
        This runs it a second time on the paths the *remote* shell resolved,
        because only the remote host can tell us whether a component of the
        workspace path is a symlink pointing outside the root — the escape that
        SPEC A.3 flags when it says path resolution "matter[s] more once
        execution crosses a machine boundary".
        """
        command = build_remote_probe_command(assignment.workspace_path, assignment.remote_root)
        result = await self.transport.run(
            assignment.host, command, timeout_ms=self.preflight_timeout_ms
        )
        if not result.ok:
            raise RemotePreflightFailed(
                "remote workspace preflight failed",
                host=assignment.host.spec,
                workspace_path=str(assignment.workspace_path),
                remote_root=str(assignment.remote_root),
                returncode=result.returncode,
                stderr=result.stderr.strip()[:500],
            )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            raise RemotePreflightFailed(
                "remote preflight did not report both resolved paths",
                host=assignment.host.spec,
                stdout=result.stdout.strip()[:500],
            )
        resolved_root = normalize_remote_path(lines[-2])
        resolved_workspace = assert_remote_within_root(lines[-1], resolved_root)
        return RemotePreflight(
            host=assignment.host,
            resolved_root=resolved_root,
            resolved_workspace=resolved_workspace,
        )

    # -- launch ------------------------------------------------------------

    def launch_command(self, assignment: HostAssignment) -> str:
        """The verified remote command line for SPEC 10.1's launch contract."""
        return build_remote_launch_command(assignment.workspace_path, self.cfg.codex.command)

    def app_server_client(
        self,
        assignment: HostAssignment,
        *,
        tool_specs: list[ToolSpec],
        tool_executor: Any,
        on_event: Callable[[AgentEvent], None],
        **kwargs: Any,
    ) -> RemoteAppServerClient:
        """Build the remote-bound app-server client for this assignment (SPEC A.1)."""
        kwargs.setdefault("secret_env_names", self.secret_env_names)
        return RemoteAppServerClient(
            self.cfg.codex,
            assignment=assignment,
            transport=self.transport,
            tool_specs=tool_specs,
            tool_executor=tool_executor,
            on_event=on_event,
            **kwargs,
        )

    # -- cleanup and observability ----------------------------------------

    async def cleanup(self, assignment: HostAssignment) -> bool:
        """Remove the remote workspace on its owning host (SPEC 8.6, A.3).

        Cleanup runs on ``assignment.host`` and nowhere else — SPEC A.3:
        operators need to know "whether cleanup happened on the right machine".
        The remote resolution is re-checked first, so a symlinked workspace
        cannot redirect the recursive remove outside the root.
        """
        await self.preflight(assignment)
        command = build_remote_cleanup_command(assignment.workspace_path, assignment.remote_root)
        result = await self.transport.run(
            assignment.host, command, timeout_ms=self.cleanup_timeout_ms
        )
        return result.ok

    def snapshot(self) -> dict[str, Any]:
        """Host ownership and capacity for the SPEC 13.3 snapshot (SPEC A.3)."""
        return {
            "ssh_enabled": self.enabled,
            "remote_root": str(self.remote_root) if self.enabled else None,
            "pool": self.pool.snapshot(),
        }
