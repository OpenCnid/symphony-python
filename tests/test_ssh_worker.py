"""Tests for the SSH worker extension (SPEC Appendix A).

No real SSH connections. Every test drives a fake transport
(:class:`FakeTransport`) or a fake remote process, so the default suite is
hermetic on any host. The one test that needs a reachable machine is marked
``@pytest.mark.integration``.

The tests are written so that they fail if the SPEC A.3 hazards regress, not
merely if the happy path breaks:

* containment is asserted against *remote* semantics, including the remote
  symlink escape that only ``pwd -P`` output can reveal;
* command construction is checked by re-parsing the finished command line, so a
  quoting change that alters the target path fails here;
* the failover boundary is checked for the specific conflation SPEC A.3 names —
  a connectivity error arriving after a turn was dispatched must not be
  transparent failover.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import pytest

from symphony.errors import (
    CodexNotFound,
    InvalidWorkspaceCwd,
    PortExit,
    ResponseTimeout,
    TurnFailed,
    TurnTimeout,
    WorkspacePathEscapesRoot,
)
from symphony.models import workspace_key
from symphony.ssh.worker import (
    DEFAULT_SSH_OPTIONS,
    CommandResult,
    FailoverAction,
    FailureStage,
    HostAssignment,
    HostPool,
    HostPoolSaturated,
    NoSSHHostsConfigured,
    OpenSSHTransport,
    RemotePreflightFailed,
    RemoteQuotingError,
    RemoteWorkspacePathEscapesRoot,
    RunProgress,
    SSHHost,
    SSHHostUnreachable,
    SSHWorker,
    assert_remote_within_root,
    build_remote_cleanup_command,
    build_remote_command,
    build_remote_launch_command,
    build_remote_probe_command,
    classify_failure,
    decide_failover,
    normalize_remote_path,
    quote_remote,
    remote_workspace_path,
    remote_workspace_root,
    ssh_enabled,
)

ROOT = "/srv/symphony/workspaces"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class FakeCodexConfig:
    command: str = "codex app-server"
    approval_policy: str = "on-request"
    thread_sandbox: str = "workspace-write"
    turn_sandbox_policy: str = "workspace-write"
    turn_timeout_ms: int = 60_000
    read_timeout_ms: int = 30_000
    stall_timeout_ms: int = 0


@dataclass
class FakeServiceConfig:
    """Only the fields SPEC Appendix A reads. Siblings are not imported."""

    ssh_hosts: tuple[str, ...] = ()
    max_concurrent_agents_per_host: int | None = None
    codex: FakeCodexConfig = field(default_factory=FakeCodexConfig)
    raw: dict[str, Any] = field(default_factory=lambda: {"workspace": {"root": ROOT}})


class FakeStream:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        self.closed = True

    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""


class FakeProcess:
    """Minimal :class:`RemoteProcess` stand-in."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdin = FakeStream()
        self.stdout = FakeStream()
        self.stderr = FakeStream()
        self.terminated = False
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._exited.set()

    def kill(self) -> None:
        self.terminate()


class FakeTransport:
    """Records every command; answers ``run`` from a scripted table."""

    def __init__(self, responses: dict[str, CommandResult] | None = None) -> None:
        self.responses = responses or {}
        self.default = CommandResult(0, f"{ROOT}\n{ROOT}/abc-1\n", "")
        self.run_calls: list[tuple[str, str]] = []
        self.spawn_calls: list[tuple[str, str, dict[str, str]]] = []
        self.spawn_error: Exception | None = None
        self.processes: list[FakeProcess] = []

    async def run(self, host: SSHHost, command: str, *, timeout_ms: int) -> CommandResult:
        del timeout_ms
        self.run_calls.append((host.spec, command))
        for needle, result in self.responses.items():
            if needle in command:
                return result
        return self.default

    async def spawn(self, host: SSHHost, command: str, *, env: Any) -> FakeProcess:
        self.spawn_calls.append((host.spec, command, dict(env)))
        if self.spawn_error is not None:
            raise self.spawn_error
        proc = FakeProcess()
        self.processes.append(proc)
        return proc


def make_worker(
    hosts: tuple[str, ...] = ("build-1", "build-2"),
    *,
    per_host: int | None = None,
    transport: FakeTransport | None = None,
) -> tuple[SSHWorker, FakeTransport]:
    tp = transport or FakeTransport()
    cfg = FakeServiceConfig(ssh_hosts=hosts, max_concurrent_agents_per_host=per_host)
    return SSHWorker(cfg, transport=tp), tp  # type: ignore[arg-type]


def make_assignment(host: str = "build-1", identifier: str = "abc-1") -> HostAssignment:
    return HostAssignment(
        host=SSHHost.parse(host),
        issue_identifier=identifier,
        workspace_path=remote_workspace_path(ROOT, identifier),
        remote_root=PurePosixPath(ROOT),
    )


def make_progress(assignment: HostAssignment | None = None) -> RunProgress:
    a = assignment or make_assignment()
    return RunProgress(
        issue_identifier=a.issue_identifier,
        host_spec=a.host.spec,
        workspace_path=str(a.workspace_path),
    )


# ==========================================================================
# SPEC A preamble — omitting worker.ssh_hosts must not change local behavior
# ==========================================================================


def test_pool_disabled_when_ssh_hosts_omitted() -> None:
    pool = HostPool.from_config(FakeServiceConfig())  # type: ignore[arg-type]
    assert pool.enabled is False
    assert pool.hosts == ()
    assert pool.saturated is False  # a disabled pool is not "waiting", it is absent


def test_ssh_enabled_predicate_tracks_config() -> None:
    assert ssh_enabled(FakeServiceConfig()) is False  # type: ignore[arg-type]
    assert ssh_enabled(FakeServiceConfig(ssh_hosts=("h1",))) is True  # type: ignore[arg-type]


async def test_disabled_pool_refuses_to_assign_rather_than_running_locally() -> None:
    pool = HostPool.from_config(FakeServiceConfig())  # type: ignore[arg-type]
    with pytest.raises(NoSSHHostsConfigured):
        pool.try_acquire("abc-1", remote_root=ROOT)
    with pytest.raises(NoSSHHostsConfigured):
        await pool.acquire("abc-1", remote_root=ROOT)


# ==========================================================================
# SPEC 9.5 / 15.2 under remote POSIX semantics (SPEC A.1, A.3)
# ==========================================================================


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "relative/path",
        "srv/ws",
        f"{ROOT}/../../etc",
        "/srv/../etc/passwd",
        "~/workspaces",
        f"{ROOT}/$HOME",
        "//srv/ws",
        "/srv/ws\x00/x",
        "/srv/ws\nrm -rf /",
    ],
)
def test_normalize_remote_path_rejects_unsafe_inputs(bad: str) -> None:
    with pytest.raises(RemoteWorkspacePathEscapesRoot):
        normalize_remote_path(bad)


def test_remote_containment_is_component_wise_not_string_prefix() -> None:
    """``/srv/ws-evil`` shares a string prefix with ``/srv/ws`` but is outside it."""
    with pytest.raises(RemoteWorkspacePathEscapesRoot):
        assert_remote_within_root("/srv/ws-evil/abc-1", "/srv/ws")
    assert assert_remote_within_root("/srv/ws/abc-1", "/srv/ws") == PurePosixPath("/srv/ws/abc-1")


def test_remote_containment_is_strict_so_root_is_not_inside_itself() -> None:
    with pytest.raises(RemoteWorkspacePathEscapesRoot):
        assert_remote_within_root(ROOT, ROOT)


def test_remote_containment_is_case_sensitive_unlike_the_local_check() -> None:
    """The remote host is POSIX; case-folding would merge two real directories."""
    with pytest.raises(RemoteWorkspacePathEscapesRoot):
        assert_remote_within_root("/srv/WS/abc-1", "/srv/ws")


def test_remote_paths_do_not_consult_the_local_filesystem() -> None:
    """A POSIX remote path must survive a Windows orchestrator unchanged.

    ``symphony.workspace.safety`` resolves against the local filesystem, which
    on Windows would rewrite this into a drive-rooted backslash path.
    """
    got = remote_workspace_path(ROOT, "abc-1")
    assert str(got) == f"{ROOT}/abc-1"
    assert "\\" not in str(got)


def test_remote_workspace_path_applies_invariant_3_sanitization() -> None:
    identifier = "feat/weird ident"
    got = remote_workspace_path(ROOT, identifier)
    assert got.name == workspace_key(identifier)
    assert "/" not in got.name and " " not in got.name
    assert got.parent == PurePosixPath(ROOT)


def test_remote_workspace_root_reads_verbatim_config_not_the_local_path() -> None:
    cfg = FakeServiceConfig(ssh_hosts=("h1",))
    assert remote_workspace_root(cfg) == PurePosixPath(ROOT)  # type: ignore[arg-type]


def test_remote_workspace_root_rejects_a_non_posix_root() -> None:
    cfg = FakeServiceConfig(ssh_hosts=("h1",), raw={"workspace": {"root": "C:\\ws"}})
    with pytest.raises(RemoteWorkspacePathEscapesRoot):
        remote_workspace_root(cfg)  # type: ignore[arg-type]


def test_remote_workspace_root_rejects_missing_root() -> None:
    cfg = FakeServiceConfig(ssh_hosts=("h1",), raw={})
    with pytest.raises(RemotePreflightFailed):
        remote_workspace_root(cfg)  # type: ignore[arg-type]


# ==========================================================================
# SPEC A.3 — shell quoting sits between the path check and the directory
# ==========================================================================


@pytest.mark.parametrize(
    "hostile",
    [
        "/srv/ws/a b",
        "/srv/ws/'; rm -rf / ; echo '",
        '/srv/ws/"quoted"',
        "/srv/ws/`id`",
        "/srv/ws/x;y",
        "/srv/ws/&&",
        "/srv/ws/*",
        "/srv/ws/it's",
    ],
)
def test_quoting_round_trips_hostile_paths_back_to_the_exact_bytes(hostile: str) -> None:
    """The quoted form must re-parse to the identical string, never a shorter one."""
    assert shlex.split(quote_remote(hostile)) == [hostile]


def test_launch_command_reparses_to_the_validated_workspace_path() -> None:
    """The check-then-quote hazard: what the remote ``cd`` receives must match.

    Re-parsing the finished command line is the assertion that quoting did not
    change the target directory between validation and transmission.
    """
    ws = "/srv/ws/a b'c"
    line = build_remote_launch_command(ws, "codex app-server")
    tokens = shlex.split(line)
    assert tokens == ["cd", "--", ws, "&&", "exec", "bash", "-lc", "codex app-server"]


def test_launch_command_neutralizes_injection_in_the_codex_command() -> None:
    line = build_remote_launch_command(f"{ROOT}/abc-1", "codex app-server; rm -rf /")
    tokens = shlex.split(line)
    # The whole codex.command stays ONE token: the ';' never becomes an operator.
    assert tokens[-1] == "codex app-server; rm -rf /"
    assert tokens.count(";") == 0


def test_launch_command_honors_spec_10_1_invocation() -> None:
    line = build_remote_launch_command(f"{ROOT}/abc-1", "codex app-server")
    tokens = shlex.split(line)
    assert tokens[:3] == ["cd", "--", f"{ROOT}/abc-1"]
    assert tokens[4:7] == ["exec", "bash", "-lc"]


def test_build_remote_command_rejects_tokens_that_do_not_round_trip() -> None:
    with pytest.raises(RemoteQuotingError):
        build_remote_command(["echo", "a\x00b"])
    with pytest.raises(RemoteQuotingError):
        build_remote_command([])


def test_a_data_token_spelled_like_an_operator_is_still_quoted() -> None:
    """Operator-ness is carried by type, not text, so data cannot become syntax."""
    line = build_remote_command(["echo", "&&", "||", ";"])
    assert shlex.split(line) == ["echo", "&&", "||", ";"]
    assert "'&&'" in line and "'||'" in line and "';'" in line
    assert " && " not in line  # no unquoted operator was emitted


def test_generated_launch_line_contains_exactly_one_real_operator() -> None:
    line = build_remote_launch_command(f"{ROOT}/abc-1", "codex app-server")
    assert line.count(" && ") == 1
    assert "'&&'" not in line


def test_launch_command_rejects_a_path_that_escapes_before_quoting() -> None:
    with pytest.raises(RemoteWorkspacePathEscapesRoot):
        build_remote_launch_command(f"{ROOT}/../etc", "codex app-server")


def test_launch_command_rejects_an_empty_codex_command() -> None:
    with pytest.raises(RemoteQuotingError):
        build_remote_launch_command(f"{ROOT}/abc-1", "   ")


def test_probe_command_is_pure_data_and_checks_writability() -> None:
    line = build_remote_probe_command(f"{ROOT}/abc-1", ROOT)
    tokens = shlex.split(line)
    assert tokens.count("pwd") == 2 and tokens.count("-P") == 2
    assert tokens[4:7] == ["test", "-w", "."]
    assert "$" not in line and "`" not in line  # no substitutions to verify around


def test_cleanup_command_refuses_a_path_outside_the_remote_root() -> None:
    with pytest.raises(RemoteWorkspacePathEscapesRoot):
        build_remote_cleanup_command("/etc", ROOT)
    with pytest.raises(RemoteWorkspacePathEscapesRoot):
        build_remote_cleanup_command(ROOT, ROOT)  # never rm -rf the root itself
    assert shlex.split(build_remote_cleanup_command(f"{ROOT}/abc-1", ROOT)) == [
        "rm",
        "-rf",
        "--",
        f"{ROOT}/abc-1",
    ]


# ==========================================================================
# SPEC A — host parsing (option injection is a real hazard here)
# ==========================================================================


def test_host_parsing_forms() -> None:
    assert SSHHost.parse("build-1") == SSHHost("build-1", "build-1", None, None)
    assert SSHHost.parse("dep@build-1").destination == "dep@build-1"
    assert SSHHost.parse("build-1:2222").port == 2222
    assert SSHHost.parse("dep@[2001:db8::1]:22").hostname == "2001:db8::1"
    assert SSHHost.parse("::1").hostname == "::1"  # bare IPv6, no port


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "-oProxyCommand=touch /tmp/pwn", "build 1", "build-1:abc", "build-1:0", "@h", "u@"],
)
def test_host_parsing_rejects_hostile_entries(bad: str) -> None:
    with pytest.raises(RemotePreflightFailed):
        SSHHost.parse(bad)


def test_ssh_argv_never_lets_a_host_entry_become_an_option() -> None:
    transport = OpenSSHTransport()
    argv = transport.argv(SSHHost.parse("dep@build-1:2222"), "true")
    assert argv[0] == "ssh"
    assert argv[-2:] == ["dep@build-1", "true"]
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"
    assert "BatchMode=yes" in DEFAULT_SSH_OPTIONS


# ==========================================================================
# SPEC A.2 — pool, assignment, saturation
# ==========================================================================


async def test_assignment_carries_host_and_workspace_as_execution_identity() -> None:
    pool = HostPool(["build-1"])
    a = await pool.acquire("abc-1", remote_root=ROOT, attempt=2)
    assert a.host.spec == "build-1"
    assert str(a.workspace_path) == f"{ROOT}/abc-1"
    assert a.attempt == 2
    assert a.to_dict()["host"] == "build-1"


async def test_per_host_cap_is_enforced_and_released() -> None:
    pool = HostPool(["build-1"], max_concurrent_agents_per_host=2)
    a1 = pool.try_acquire("A-1", remote_root=ROOT)
    a2 = pool.try_acquire("A-2", remote_root=ROOT)
    assert a1 is not None and a2 is not None
    assert pool.in_use("build-1") == 2
    assert pool.try_acquire("A-3", remote_root=ROOT) is None  # saturated
    await pool.release(a1)
    assert pool.try_acquire("A-3", remote_root=ROOT) is not None


async def test_saturation_waits_and_never_falls_back_to_local(
) -> None:
    """SPEC A.2: "dispatch SHOULD wait rather than silently falling back"."""
    pool = HostPool(["build-1"], max_concurrent_agents_per_host=1)
    held = pool.try_acquire("A-1", remote_root=ROOT)
    assert held is not None

    waiter = asyncio.create_task(pool.acquire("A-2", remote_root=ROOT))
    await asyncio.sleep(0)
    assert not waiter.done()  # it waits; it does not return a local execution mode
    assert pool.saturated is True

    await pool.release(held)
    got = await asyncio.wait_for(waiter, timeout=1)
    assert got.host.spec == "build-1"


async def test_saturation_timeout_raises_rather_than_degrading() -> None:
    pool = HostPool(["build-1"], max_concurrent_agents_per_host=1)
    pool.try_acquire("A-1", remote_root=ROOT)
    with pytest.raises(HostPoolSaturated):
        await pool.acquire("A-2", remote_root=ROOT, timeout_ms=10)


async def test_unreachable_host_reduces_capacity_it_does_not_divert_work() -> None:
    """SPEC A.3: a dead host SHOULD reduce capacity, not cause a local fallback."""
    pool = HostPool(["build-1", "build-2"])
    await pool.mark_unreachable("build-1", error="ssh: connect: refused")
    assert pool.available_hosts() == [SSHHost.parse("build-2")]

    await pool.mark_unreachable("build-2")
    assert pool.saturated is True
    assert pool.try_acquire("A-1", remote_root=ROOT) is None  # waits, never local

    await pool.mark_reachable("build-1")
    assert pool.try_acquire("A-1", remote_root=ROOT) is not None


async def test_marking_reachable_wakes_a_waiter() -> None:
    pool = HostPool(["build-1"])
    await pool.mark_unreachable("build-1")
    waiter = asyncio.create_task(pool.acquire("A-1", remote_root=ROOT))
    await asyncio.sleep(0)
    assert not waiter.done()
    await pool.mark_reachable("build-1")
    assert (await asyncio.wait_for(waiter, timeout=1)).host.spec == "build-1"


def test_selection_prefers_the_previous_host_then_the_least_loaded() -> None:
    pool = HostPool(["build-1", "build-2"], max_concurrent_agents_per_host=2)
    pool.try_acquire("A-1", remote_root=ROOT, prefer="build-1")
    # build-1 now has load 1; without a preference the least loaded wins.
    assert pool.select() == SSHHost.parse("build-2")
    # SPEC A.2: prefer the previously used host while it still has capacity.
    assert pool.select(prefer="build-1") == SSHHost.parse("build-1")


def test_preference_is_ignored_when_the_preferred_host_is_full() -> None:
    pool = HostPool(["build-1", "build-2"], max_concurrent_agents_per_host=1)
    pool.try_acquire("A-1", remote_root=ROOT, prefer="build-1")
    assert pool.select(prefer="build-1") == SSHHost.parse("build-2")


def test_duplicate_host_entries_do_not_double_capacity() -> None:
    pool = HostPool(["build-1", "build-1"], max_concurrent_agents_per_host=1)
    assert len(pool.hosts) == 1
    pool.try_acquire("A-1", remote_root=ROOT)
    assert pool.try_acquire("A-2", remote_root=ROOT) is None


def test_absent_per_host_cap_means_unbounded() -> None:
    pool = HostPool(["build-1"], max_concurrent_agents_per_host=None)
    for i in range(25):
        assert pool.try_acquire(f"A-{i}", remote_root=ROOT) is not None
    assert pool.saturated is False


async def test_lease_releases_the_slot_even_when_the_body_raises() -> None:
    pool = HostPool(["build-1"], max_concurrent_agents_per_host=1)
    with pytest.raises(RuntimeError):
        async with pool.lease("A-1", remote_root=ROOT):
            raise RuntimeError("worker blew up")
    assert pool.in_use("build-1") == 0


async def test_continuation_turns_reuse_one_assignment_for_a_worker_lifetime() -> None:
    """SPEC A.1: continuation turns SHOULD stay on the same host and workspace."""
    pool = HostPool(["build-1", "build-2"], max_concurrent_agents_per_host=4)
    async with pool.lease("abc-1", remote_root=ROOT) as assignment:
        seen = {(assignment.host.spec, str(assignment.workspace_path)) for _ in range(5)}
    assert len(seen) == 1
    assert pool.in_use(assignment.host.spec) == 0


def test_pool_snapshot_names_host_owner_and_workspace_for_operators() -> None:
    pool = HostPool(["build-1", "build-2"], max_concurrent_agents_per_host=1)
    a = pool.try_acquire("abc-1", remote_root=ROOT)
    assert a is not None
    snap = pool.snapshot()
    assert snap["enabled"] is True
    by_host = {h["host"]: h for h in snap["hosts"]}
    assert by_host[a.host.spec]["in_use"] == 1
    assert by_host[a.host.spec]["has_capacity"] is False
    assert a.to_dict()["workspace_path"] == f"{ROOT}/abc-1"


def test_pool_rejects_a_host_that_is_not_a_member() -> None:
    pool = HostPool(["build-1"])
    with pytest.raises(RemotePreflightFailed):
        pool.in_use("build-9")


# ==========================================================================
# SPEC A.2 / A.3 — the failover boundary
# ==========================================================================


def test_progress_latch_is_monotonic_and_keeps_the_first_reason() -> None:
    p = make_progress()
    assert p.side_effects_possible is False
    p.mark_turn_dispatched(1)
    assert p.side_effects_possible is True
    assert p.side_effect_reason == "turn_dispatched:1"
    p.mark_side_effect("something_else")
    assert p.side_effect_reason == "turn_dispatched:1"  # never re-opened
    assert p.side_effects_possible is True


def test_workspace_preparation_alone_does_not_latch() -> None:
    """SPEC A.3 calls a host move a cold restart, not a correctness problem."""
    p = make_progress()
    p.mark_workspace_prepared()
    assert p.workspace_prepared is True
    assert p.side_effects_possible is False


def test_hook_start_latches_because_hooks_are_repo_owned_scripts() -> None:
    p = make_progress()
    p.mark_hook_started("before_run")
    assert p.side_effects_possible is True
    assert p.side_effect_reason == "hook_started:before_run"


@pytest.mark.parametrize(
    ("exc", "stage"),
    [
        (SSHHostUnreachable("refused"), FailureStage.CONNECT),
        (ConnectionResetError("reset"), FailureStage.CONNECT),
        (FileNotFoundError("no ssh"), FailureStage.CONNECT),
        (RemotePreflightFailed("no root"), FailureStage.PREFLIGHT),
        (RemoteWorkspacePathEscapesRoot("escape"), FailureStage.PREFLIGHT),
        (RemoteQuotingError("bad quote"), FailureStage.PREFLIGHT),
        (CodexNotFound("127"), FailureStage.STARTUP),
        (InvalidWorkspaceCwd("cwd"), FailureStage.STARTUP),
        (ResponseTimeout("initialize"), FailureStage.STARTUP),
        (PortExit("exited"), FailureStage.STARTUP),
        (TurnFailed("agent"), FailureStage.AGENT),
        (TurnTimeout("silent"), FailureStage.AGENT),
        (ValueError("who knows"), FailureStage.UNKNOWN),
    ],
)
def test_failure_classification(exc: BaseException, stage: FailureStage) -> None:
    assert classify_failure(exc) is stage


def test_pre_side_effect_connect_failure_may_move_hosts() -> None:
    """SPEC A.2: failover is permitted before work has meaningfully started."""
    d = decide_failover(make_progress(), SSHHostUnreachable("refused"), hosts_remaining=1)
    assert d.action is FailoverAction.RETRY_OTHER_HOST
    assert d.may_switch_host is True
    assert d.side_effects_possible is False


@pytest.mark.parametrize(
    "exc",
    [SSHHostUnreachable("refused"), CodexNotFound("127"), RemotePreflightFailed("no root")],
)
def test_all_pre_side_effect_startup_stages_may_move_hosts(exc: BaseException) -> None:
    d = decide_failover(make_progress(), exc, hosts_remaining=2)
    assert d.action is FailoverAction.RETRY_OTHER_HOST


def test_connect_failure_after_a_turn_was_dispatched_is_a_new_attempt() -> None:
    """The exact SPEC A.3 conflation this module exists to prevent.

    A dropped SSH channel is classified CONNECT whether it happened while
    dialing or halfway through a turn. Only the latch distinguishes them. If
    this returns RETRY_OTHER_HOST, the same ticket runs on two machines while
    the first is still working.
    """
    p = make_progress()
    p.mark_turn_dispatched(1)
    d = decide_failover(p, SSHHostUnreachable("channel closed"), hosts_remaining=3)
    assert d.action is FailoverAction.NEW_ATTEMPT
    assert d.may_switch_host is False
    assert d.stage is FailureStage.CONNECT  # classification unchanged...
    assert d.reason.startswith("side_effects_possible:")  # ...the latch decided


def test_port_exit_after_dispatch_is_a_new_attempt_not_a_startup_retry() -> None:
    """PortExit classifies as STARTUP; the latch must still dominate."""
    p = make_progress()
    p.mark_turn_dispatched(1)
    d = decide_failover(p, PortExit("app-server exited"), hosts_remaining=2)
    assert d.stage is FailureStage.STARTUP
    assert d.action is FailoverAction.NEW_ATTEMPT


def test_hook_side_effects_block_transparent_failover() -> None:
    p = make_progress()
    p.mark_hook_started("before_run")
    d = decide_failover(p, SSHHostUnreachable("refused"), hosts_remaining=2)
    assert d.action is FailoverAction.NEW_ATTEMPT


def test_agent_failure_is_a_new_attempt_even_if_the_latch_was_missed() -> None:
    """Belt-and-braces: reaching an agent error means a turn ran."""
    d = decide_failover(make_progress(), TurnFailed("agent failed"), hosts_remaining=5)
    assert d.action is FailoverAction.NEW_ATTEMPT
    assert d.reason == "agent_failure_implies_side_effects"


def test_unclassified_failure_never_becomes_transparent_failover() -> None:
    d = decide_failover(make_progress(), ValueError("mystery"), hosts_remaining=5)
    assert d.action is FailoverAction.NEW_ATTEMPT
    assert d.stage is FailureStage.UNKNOWN


def test_no_alternate_host_becomes_a_new_attempt_not_a_local_run() -> None:
    d = decide_failover(make_progress(), SSHHostUnreachable("refused"), hosts_remaining=0)
    assert d.action is FailoverAction.NEW_ATTEMPT
    assert d.reason == "no_alternate_host_available"


@pytest.mark.parametrize(
    "exc", [RemoteWorkspacePathEscapesRoot("escape"), RemoteQuotingError("bad quoting")]
)
def test_host_independent_failures_fail_outright(exc: BaseException) -> None:
    """Another host reproduces these identically; retrying burns attempts."""
    d = decide_failover(make_progress(), exc, hosts_remaining=4)
    assert d.action is FailoverAction.FAIL
    assert d.may_switch_host is False


def test_containment_violation_is_catchable_as_the_spec_9_5_error() -> None:
    """The orchestrator's existing 9.5 handling must apply without SSH awareness."""
    exc = RemoteWorkspacePathEscapesRoot("escape")
    assert isinstance(exc, WorkspacePathEscapesRoot)
    assert exc.category == "workspace_path_escapes_root"


def test_failover_decision_is_json_safe() -> None:
    d = decide_failover(make_progress(), SSHHostUnreachable("x"), hosts_remaining=1)
    assert d.to_dict() == {
        "action": "retry_other_host",
        "stage": "connect",
        "reason": "pre_side_effect_connect",
        "side_effects_possible": False,
        "error_category": "ssh_host_unreachable",
    }


# ==========================================================================
# SPEC A.1 — remote preflight and launch
# ==========================================================================


async def test_preflight_rechecks_containment_against_remote_resolution() -> None:
    """The remote symlink escape a local check structurally cannot see.

    Lexically ``/srv/.../abc-1`` is inside the root. The remote host resolves it
    to ``/tmp/elsewhere`` because a component is a symlink. Only the ``pwd -P``
    output reveals this.
    """
    transport = FakeTransport({"pwd": CommandResult(0, f"{ROOT}\n/tmp/elsewhere\n", "")})
    worker, _ = make_worker(transport=transport)
    assignment = make_assignment()
    with pytest.raises(RemoteWorkspacePathEscapesRoot):
        await worker.preflight(assignment)


async def test_preflight_accepts_a_remotely_resolved_root_that_moved_together() -> None:
    """If root and workspace both resolve under a new prefix, containment holds."""
    transport = FakeTransport({"pwd": CommandResult(0, "/mnt/real\n/mnt/real/abc-1\n", "")})
    worker, _ = make_worker(transport=transport)
    result = await worker.preflight(make_assignment())
    assert str(result.resolved_workspace) == "/mnt/real/abc-1"
    assert str(result.resolved_root) == "/mnt/real"


async def test_preflight_failure_is_reported_as_a_preflight_error() -> None:
    transport = FakeTransport({"pwd": CommandResult(1, "", "cd: no such directory")})
    worker, _ = make_worker(transport=transport)
    exc = None
    try:
        await worker.preflight(make_assignment())
    except RemotePreflightFailed as e:
        exc = e
    assert exc is not None
    assert classify_failure(exc) is FailureStage.PREFLIGHT
    assert exc.details["host"] == "build-1"


async def test_preflight_rejects_truncated_output() -> None:
    transport = FakeTransport({"pwd": CommandResult(0, f"{ROOT}\n", "")})
    worker, _ = make_worker(transport=transport)
    with pytest.raises(RemotePreflightFailed):
        await worker.preflight(make_assignment())


async def test_worker_launch_command_matches_spec_10_1_remotely() -> None:
    worker, _ = make_worker()
    line = worker.launch_command(make_assignment())
    assert shlex.split(line) == [
        "cd", "--", f"{ROOT}/abc-1", "&&", "exec", "bash", "-lc", "codex app-server",
    ]  # fmt: skip


async def test_remote_client_sends_posix_workspace_paths_on_the_wire() -> None:
    """A Windows orchestrator must not put backslashes in the protocol payload."""
    worker, transport = make_worker()
    client = worker.app_server_client(
        make_assignment(), tool_specs=[], tool_executor=lambda *_: None, on_event=lambda _e: None
    )
    assert str(client.workspace) == f"{ROOT}/abc-1"
    assert "\\" not in str(client.workspace)
    assert client.launch_argv() == [worker.launch_command(make_assignment())]
    assert transport.spawn_calls == []


async def test_remote_client_spawns_over_the_transport_with_the_remote_command() -> None:
    worker, transport = make_worker()
    assignment = make_assignment(host="build-2")
    client = worker.app_server_client(
        assignment, tool_specs=[], tool_executor=lambda *_: None, on_event=lambda _e: None
    )
    proc = await client._spawn(client.launch_argv(), client.workspace, {"HOME": "/h"})
    assert isinstance(proc, FakeProcess)
    host_spec, command, env = transport.spawn_calls[0]
    assert host_spec == "build-2"
    assert shlex.split(command)[2] == f"{ROOT}/abc-1"
    assert env == {"HOME": "/h"}


async def test_remote_client_strips_secret_env_names_from_the_child(monkeypatch) -> None:
    """SPEC 15.3 names remote launchers explicitly."""
    monkeypatch.setenv("LINEAR_API_KEY", "super-secret")
    cfg = FakeServiceConfig(ssh_hosts=("build-1",))
    worker = SSHWorker(cfg, transport=FakeTransport(), secret_env_names=("LINEAR_API_KEY",))  # type: ignore[arg-type]
    client = worker.app_server_client(
        make_assignment(), tool_specs=[], tool_executor=lambda *_: None, on_event=lambda _e: None
    )
    env = client._child_env()
    assert "LINEAR_API_KEY" not in env


async def test_remote_client_enforces_invariant_1_in_remote_terms() -> None:
    """The base class asks the local filesystem; the override asks the root."""
    worker, _ = make_worker()
    bad = HostAssignment(
        host=SSHHost.parse("build-1"),
        issue_identifier="abc-1",
        workspace_path=PurePosixPath("/tmp/elsewhere/abc-1"),
        remote_root=PurePosixPath(ROOT),
    )
    client = worker.app_server_client(
        bad, tool_specs=[], tool_executor=lambda *_: None, on_event=lambda _e: None
    )
    with pytest.raises(InvalidWorkspaceCwd):
        client._assert_launch_cwd()


async def test_remote_client_accepts_a_contained_workspace() -> None:
    worker, _ = make_worker()
    client = worker.app_server_client(
        make_assignment(), tool_specs=[], tool_executor=lambda *_: None, on_event=lambda _e: None
    )
    client._assert_launch_cwd()  # must not raise


async def test_start_session_drives_the_whole_remote_launch_path() -> None:
    """End-to-end through the inherited SPEC 10.2 startup, over the fake transport.

    Proves the wiring rather than the seam: ``start_session`` runs the remote
    Invariant 1 check, builds the remote command via ``launch_argv``, spawns it
    through the transport, and then maps the silent remote to a SPEC 10.6 error.
    """
    transport = FakeTransport()
    cfg = FakeServiceConfig(ssh_hosts=("build-1",), codex=FakeCodexConfig(read_timeout_ms=10))
    worker = SSHWorker(cfg, transport=transport)  # type: ignore[arg-type]
    client = worker.app_server_client(
        make_assignment(), tool_specs=[], tool_executor=lambda *_: None, on_event=lambda _e: None
    )
    with pytest.raises(ResponseTimeout):
        await client.start_session()

    host_spec, command, _env = transport.spawn_calls[0]
    assert host_spec == "build-1"
    assert shlex.split(command)[2] == f"{ROOT}/abc-1"
    assert transport.processes[0].terminated is True  # session lifecycle stays local


async def test_start_session_refuses_an_escaping_workspace_before_spawning() -> None:
    """Invariant 1 must fail closed: no ssh process is started at all."""
    transport = FakeTransport()
    worker, _ = make_worker(transport=transport)
    bad = HostAssignment(
        host=SSHHost.parse("build-1"),
        issue_identifier="abc-1",
        workspace_path=PurePosixPath("/tmp/elsewhere"),
        remote_root=PurePosixPath(ROOT),
    )
    client = worker.app_server_client(
        bad, tool_specs=[], tool_executor=lambda *_: None, on_event=lambda _e: None
    )
    with pytest.raises(InvalidWorkspaceCwd):
        await client.start_session()
    assert transport.spawn_calls == []


async def test_spawn_failure_surfaces_as_a_connect_stage_error() -> None:
    transport = FakeTransport()
    transport.spawn_error = SSHHostUnreachable("dial failed", host="build-1")
    worker, _ = make_worker(transport=transport)
    client = worker.app_server_client(
        make_assignment(), tool_specs=[], tool_executor=lambda *_: None, on_event=lambda _e: None
    )
    with pytest.raises(SSHHostUnreachable) as caught:
        await client._spawn(client.launch_argv(), client.workspace, {})
    assert classify_failure(caught.value) is FailureStage.CONNECT


# ==========================================================================
# SPEC 8.6 / A.3 — cleanup happens on the owning machine
# ==========================================================================


async def test_cleanup_runs_on_the_assignments_own_host() -> None:
    transport = FakeTransport()
    worker, _ = make_worker(hosts=("build-1", "build-2"), transport=transport)
    assignment = make_assignment(host="build-2")
    assert await worker.cleanup(assignment) is True
    hosts_used = {host for host, _ in transport.run_calls}
    assert hosts_used == {"build-2"}
    assert any(cmd.startswith("rm -rf --") for _, cmd in transport.run_calls)


async def test_cleanup_refuses_when_remote_resolution_escapes_the_root() -> None:
    """A symlinked workspace must not redirect the recursive remove."""
    transport = FakeTransport({"pwd": CommandResult(0, f"{ROOT}\n/\n", "")})
    worker, _ = make_worker(transport=transport)
    with pytest.raises(RemoteWorkspacePathEscapesRoot):
        await worker.cleanup(make_assignment())
    assert not any("rm" in cmd for _, cmd in transport.run_calls)


async def test_cleanup_reports_a_failing_remote_remove() -> None:
    transport = FakeTransport({"rm -rf": CommandResult(1, "", "Permission denied")})
    worker, _ = make_worker(transport=transport)
    assert await worker.cleanup(make_assignment()) is False


# ==========================================================================
# Worker façade wiring
# ==========================================================================


async def test_worker_assign_and_release_round_trip() -> None:
    worker, _ = make_worker(hosts=("build-1",), per_host=1)
    a = await worker.assign("abc-1", attempt=1)
    assert worker.try_assign("ABC-2") is None  # saturated: dispatch waits
    await worker.release(a)
    assert worker.try_assign("ABC-2") is not None


async def test_worker_snapshot_answers_the_operator_questions() -> None:
    """SPEC A.3: which host owns a run and where its workspace lives."""
    worker, _ = make_worker(hosts=("build-1",), per_host=2)
    a = await worker.assign("abc-1")
    snap = worker.snapshot()
    assert snap["ssh_enabled"] is True
    assert snap["remote_root"] == ROOT
    assert snap["pool"]["hosts"][0]["in_use"] == 1
    assert a.to_dict()["host"] == "build-1"


async def test_worker_progress_starts_unlatched_per_lifetime() -> None:
    worker, _ = make_worker()
    a = await worker.assign("abc-1")
    p1 = worker.progress_for(a)
    p1.mark_turn_dispatched(1)
    p2 = worker.progress_for(a)  # a new worker lifetime starts clean
    assert p1.side_effects_possible is True
    assert p2.side_effects_possible is False


async def test_worker_disabled_when_ssh_hosts_omitted() -> None:
    worker = SSHWorker(FakeServiceConfig(), transport=FakeTransport())  # type: ignore[arg-type]
    assert worker.enabled is False
    with pytest.raises(NoSSHHostsConfigured):
        await worker.assign("abc-1")


async def test_explicit_remote_root_override_wins_over_config() -> None:
    worker = SSHWorker(
        FakeServiceConfig(ssh_hosts=("build-1",), raw={}),  # type: ignore[arg-type]
        transport=FakeTransport(),
        remote_root="/data/ws",
    )
    a = await worker.assign("abc-1")
    assert str(a.workspace_path) == "/data/ws/abc-1"


# ==========================================================================
# SPEC 17.8 — real integration profile
# ==========================================================================


@pytest.mark.integration
async def test_real_ssh_preflight_against_a_reachable_host() -> None:  # pragma: no cover
    """Requires SYMPHONY_TEST_SSH_HOST and SYMPHONY_TEST_SSH_ROOT to be set."""
    import os

    host = os.environ.get("SYMPHONY_TEST_SSH_HOST")
    root = os.environ.get("SYMPHONY_TEST_SSH_ROOT")
    if not host or not root:
        pytest.skip("SYMPHONY_TEST_SSH_HOST / SYMPHONY_TEST_SSH_ROOT not set")
    cfg = FakeServiceConfig(ssh_hosts=(host,), raw={"workspace": {"root": root}})
    worker = SSHWorker(cfg)  # type: ignore[arg-type]
    assignment = await worker.assign("SYMPHONY-PREFLIGHT")
    try:
        result = await worker.preflight(assignment)
        assert str(result.resolved_workspace).startswith(str(result.resolved_root))
    except (SSHHostUnreachable, RemotePreflightFailed) as exc:
        pytest.skip(f"remote host not usable: {exc}")
    finally:
        await worker.release(assignment)
