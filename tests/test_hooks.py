"""Conformance tests for workspace lifecycle hooks (SPEC 9.4, 15.4, 17.2).

The four hooks share one execution path and differ only in failure semantics,
so the tests that matter are: the per-hook fatal/ignored table (SPEC 9.4), and
proof that a timed-out hook's *process tree* is actually dead rather than
merely abandoned (SPEC 15.4, "Hook timeouts are REQUIRED to avoid hanging the
orchestrator").

``HookConfig`` lives in ``symphony.workflow.config``, which is being written
concurrently; these tests use :class:`FakeHookConfig` with the field names
CONTRACTS.md pins, so a field rename in either place shows up as a failure here
rather than as a silent no-op.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from symphony.errors import HookError, HookTimeout, SymphonyError, WorkspaceError
from symphony.workspace.hooks import (
    DEFAULT_HOOK_TIMEOUT_MS,
    HOOK_NAMES,
    HOOK_OUTPUT_LOG_LIMIT,
    SPEC_FATAL_HOOKS,
    HookRunner,
    HookShell,
    default_fatal,
    resolve_hook_shell,
    truncate_for_log,
)

# --------------------------------------------------------------------------
# Test doubles and helpers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeHookConfig:
    """Stand-in for ``symphony.workflow.config.HookConfig`` (CONTRACTS.md 3)."""

    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 15_000


class RecordingLogger:
    """Captures structured log calls so SPEC 9.4's logging duties are assertable."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def _add(self, level: str, msg: str, fields: dict[str, Any]) -> None:
        self.records.append((level, msg, fields))

    def debug(self, msg: str, **fields: Any) -> None:
        self._add("debug", msg, fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._add("info", msg, fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._add("warning", msg, fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._add("error", msg, fields)

    def messages(self, level: str | None = None) -> list[str]:
        return [m for lvl, m, _ in self.records if level is None or lvl == level]

    def fields_for(self, msg: str) -> dict[str, Any]:
        for _, m, f in self.records:
            if m == msg:
                return f
        raise AssertionError(f"no log record {msg!r}; got {self.messages()}")


SHELL = resolve_hook_shell()

requires_posix_shell = pytest.mark.skipif(
    not SHELL.posix,
    reason="SPEC 9.4 POSIX shell (sh/bash) not available on this host",
)
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-specific behavior")


def make_runner(logger: RecordingLogger | None = None, **cfg_kwargs: Any) -> HookRunner:
    return HookRunner(FakeHookConfig(**cfg_kwargs), logger=logger or RecordingLogger())


def pid_alive(pid: int) -> bool:
    """True while ``pid`` is a live OS process.

    ``os.kill(pid, 0)`` is not usable on Windows -- CPython implements it as
    ``TerminateProcess``, which would *cause* the state being asserted.
    """
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return any(str(pid) in line.split() for line in out.stdout.splitlines())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def await_pid_gone(pid: int, timeout_s: float = 15.0) -> bool:
    """Bounded poll -- a kill is asynchronous, but it is not slow.

    Async, and ``pid_alive`` runs off-loop: these tests observe a hook task that
    needs the event loop to make progress, so a blocking poll would deadlock the
    thing being measured.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not await asyncio.to_thread(pid_alive, pid):
            return True
        await asyncio.sleep(0.05)
    return not await asyncio.to_thread(pid_alive, pid)


# A hook that records the OS pid of a *grandchild* and then blocks. If only the
# shell is killed, the grandchild survives holding the workspace open -- exactly
# the orphan SPEC 15.4 exists to prevent.
GRANDCHILD_SCRIPT = """
( sleep 45 ) &
child=$!
if [ -r "/proc/$child/winpid" ]; then
  cat "/proc/$child/winpid" > grandchild.pid
else
  echo "$child" > grandchild.pid
fi
sleep 45
"""


@pytest.fixture
def reaper():
    """Force-kill any process a failing kill-verification test leaked."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        if not pid_alive(pid):
            continue
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        else:  # pragma: no cover - POSIX cleanup
            import signal

            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


async def read_grandchild_pid(ws: Path, timeout_s: float = 20.0) -> int:
    """Wait for the running hook to publish its grandchild's OS pid."""
    deadline = time.monotonic() + timeout_s
    marker = ws / "grandchild.pid"
    while time.monotonic() < deadline:
        if marker.exists():
            text = marker.read_text(encoding="utf-8").strip()
            if text.isdigit():
                return int(text)
        await asyncio.sleep(0.02)
    raise AssertionError("hook never recorded its grandchild pid")


@contextlib.asynccontextmanager
async def running_hook(runner: HookRunner, name: str, ws: Path, reaper: list[int]):
    """Run ``name`` in the background and yield its grandchild's OS pid.

    Guarantees the task is settled on exit so a failed assertion cannot leave a
    live subprocess pinned to a closing event loop.
    """
    task = asyncio.ensure_future(runner.run(name, ws, fatal=True))
    try:
        pid = await read_grandchild_pid(ws)
        reaper.append(pid)
        assert await asyncio.to_thread(pid_alive, pid), "scaffolding: grandchild never started"
        yield task, pid
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


# --------------------------------------------------------------------------
# SPEC 9.4 -- surface and failure-semantics table
# --------------------------------------------------------------------------


def test_hook_names_are_the_four_spec_hooks() -> None:
    assert HOOK_NAMES == ("after_create", "before_run", "after_run", "before_remove")


@pytest.mark.parametrize(
    ("name", "fatal"),
    [
        ("after_create", True),  # fatal to workspace creation
        ("before_run", True),  # fatal to the current run attempt
        ("after_run", False),  # logged and ignored
        ("before_remove", False),  # logged and ignored
    ],
)
def test_default_fatal_matches_spec_9_4_table(name: str, fatal: bool) -> None:
    assert default_fatal(name) is fatal
    assert SPEC_FATAL_HOOKS[name] is fatal


def test_default_fatal_rejects_unknown_hook() -> None:
    with pytest.raises(ValueError, match="unknown hook name"):
        default_fatal("during_run")


def test_contract_signature_is_preserved() -> None:
    """CONTRACTS.md pins ``HookRunner(cfg)`` and ``run(name, cwd, *, fatal)``."""
    init = inspect.signature(HookRunner.__init__).parameters
    assert list(init)[:2] == ["self", "cfg"]
    assert init["cfg"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(
        p.default is not inspect.Parameter.empty
        for n, p in init.items()
        if n not in ("self", "cfg")
    )

    run = inspect.signature(HookRunner.run).parameters
    assert list(run) == ["self", "name", "cwd", "fatal"]
    assert run["fatal"].kind is inspect.Parameter.KEYWORD_ONLY
    assert run["fatal"].default is inspect.Parameter.empty


def test_hook_errors_are_workspace_errors_with_spec_categories() -> None:
    assert issubclass(HookError, WorkspaceError) and issubclass(HookError, SymphonyError)
    assert HookError("x").category == "hook_error"
    assert HookTimeout("x").category == "hook_timeout"


# --------------------------------------------------------------------------
# Unconfigured / invalid hooks
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", HOOK_NAMES)
@pytest.mark.parametrize("fatal", [True, False])
async def test_unconfigured_hook_is_a_noop_and_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, fatal: bool
) -> None:
    async def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unconfigured hook must not spawn a shell")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    runner = make_runner()
    assert await runner.run(name, tmp_path, fatal=fatal) is None
    assert (await runner.execute(name, tmp_path)).status == "skipped"


@pytest.mark.parametrize("script", ["", "   ", "\n\t \n"])
async def test_blank_script_is_treated_as_unconfigured(tmp_path: Path, script: str) -> None:
    runner = make_runner(before_run=script)
    assert (await runner.execute("before_run", tmp_path)).status == "skipped"


async def test_unknown_hook_name_raises_regardless_of_fatal(tmp_path: Path) -> None:
    runner = make_runner()
    for fatal in (True, False):
        with pytest.raises(ValueError, match="unknown hook name"):
            await runner.run("on_boot", tmp_path, fatal=fatal)


async def test_launch_failure_is_reported_as_a_hook_failure(tmp_path: Path) -> None:
    missing = HookShell(
        executable=str(tmp_path / "no-such-shell"), args=("-lc",), kind="sh", posix=True
    )
    runner = HookRunner(
        FakeHookConfig(before_run="echo hi"), shell=missing, logger=RecordingLogger()
    )
    outcome = await runner.execute("before_run", tmp_path)
    assert outcome.status == "failed"
    assert outcome.exit_code is None
    assert "could not launch hook shell" in outcome.output
    with pytest.raises(HookError):
        await runner.run("before_run", tmp_path, fatal=True)


# --------------------------------------------------------------------------
# SPEC 9.4 -- execution contract
# --------------------------------------------------------------------------


@requires_posix_shell
async def test_hook_runs_with_the_workspace_directory_as_cwd(tmp_path: Path) -> None:
    ws = tmp_path / "SYM-1"
    ws.mkdir()
    runner = make_runner(after_create="pwd > where.txt; touch created.marker")
    await runner.run("after_create", ws, fatal=True)

    assert (ws / "created.marker").exists()
    reported = (ws / "where.txt").read_text(encoding="utf-8").strip()
    # Compare through the shell's own view of the path to stay MSYS-safe.
    proc = subprocess.run(
        [*SHELL.argv("pwd")], cwd=str(ws), capture_output=True, text=True, check=True
    )
    assert reported == proc.stdout.strip()


@requires_posix_shell
async def test_spec_9_4_shell_contract_is_sh_or_bash_dash_lc() -> None:
    assert SHELL.kind in ("sh", "bash")
    assert SHELL.args == ("-lc",)
    assert SHELL.argv("echo hi")[1:] == ["-lc", "echo hi"]


@requires_posix_shell
async def test_start_and_completion_are_logged(tmp_path: Path) -> None:
    log = RecordingLogger()
    runner = make_runner(log, before_run="exit 0", timeout_ms=9_000)
    await runner.run("before_run", tmp_path, fatal=True)

    started = log.fields_for("hook started")
    assert started["hook"] == "before_run"
    assert started["cwd"] == str(tmp_path)
    assert started["timeout_ms"] == 9_000
    assert SHELL.kind in started["shell"]
    assert log.fields_for("hook completed")["exit_code"] == 0


@requires_posix_shell
async def test_stdin_is_closed_so_an_interactive_hook_cannot_hang(tmp_path: Path) -> None:
    runner = make_runner(before_run='read line; echo "got:[$line]"; exit 0')
    outcome = await runner.execute("before_run", tmp_path)
    assert outcome.status == "ok"
    assert "got:[]" in outcome.output


# --------------------------------------------------------------------------
# SPEC 9.4 -- failure semantics, the conformance surface
# --------------------------------------------------------------------------


@requires_posix_shell
@pytest.mark.parametrize("name", HOOK_NAMES)
async def test_failure_disposition_follows_spec_9_4_per_hook(tmp_path: Path, name: str) -> None:
    """after_create/before_run abort; after_run/before_remove are ignored."""
    log = RecordingLogger()
    runner = HookRunner(
        FakeHookConfig(**{name: "echo boom >&2; exit 3"}, timeout_ms=15_000),
        logger=log,
    )
    fatal = default_fatal(name)

    if fatal:
        with pytest.raises(HookError) as excinfo:
            await runner.run(name, tmp_path, fatal=fatal)
        assert not isinstance(excinfo.value, HookTimeout)
        assert excinfo.value.details["exit_code"] == 3
    else:
        assert await runner.run(name, tmp_path, fatal=fatal) is None

    assert "hook failed" in log.messages("error")
    assert log.fields_for("hook failed")["outcome"] == ("aborting" if fatal else "ignored")


@requires_posix_shell
async def test_fatal_failure_carries_hook_name_cwd_and_output(tmp_path: Path) -> None:
    runner = make_runner(before_run="echo to-stdout; echo to-stderr >&2; exit 12")
    with pytest.raises(HookError) as excinfo:
        await runner.run("before_run", tmp_path, fatal=True)

    err = excinfo.value
    assert err.category == "hook_error"
    assert err.details["hook"] == "before_run"
    assert err.details["cwd"] == str(tmp_path)
    assert err.details["exit_code"] == 12
    assert "to-stdout" in err.details["output"]
    assert "to-stderr" in err.details["output"]  # stderr is folded into stdout
    assert err.to_dict()["category"] == "hook_error"


@requires_posix_shell
async def test_non_fatal_failure_does_not_prevent_the_next_hook(tmp_path: Path) -> None:
    """SPEC 9.4: before_remove failure is ignored and cleanup still proceeds."""
    runner = make_runner(before_remove="exit 9", after_run="touch after_run.marker; exit 0")
    assert await runner.run("before_remove", tmp_path, fatal=False) is None
    await runner.run("after_run", tmp_path, fatal=False)
    assert (tmp_path / "after_run.marker").exists()


# --------------------------------------------------------------------------
# SPEC 15.4 -- timeouts and process-tree termination
# --------------------------------------------------------------------------


@requires_posix_shell
async def test_fatal_timeout_raises_hook_timeout(tmp_path: Path) -> None:
    log = RecordingLogger()
    runner = make_runner(log, before_run="sleep 45", timeout_ms=1_200)
    started = time.monotonic()
    with pytest.raises(HookTimeout) as excinfo:
        await runner.run("before_run", tmp_path, fatal=True)

    assert time.monotonic() - started < 20.0, "timeout did not bound the hook"
    err = excinfo.value
    assert err.category == "hook_timeout"
    assert err.details["timeout_ms"] == 1_200
    assert err.details["hook"] == "before_run"
    assert err.details["killed"] in ("job_object", "process_group", "taskkill", "terminate")
    assert "hook timed out" in log.messages("error")


@requires_posix_shell
async def test_non_fatal_timeout_is_logged_and_ignored(tmp_path: Path) -> None:
    log = RecordingLogger()
    runner = make_runner(log, after_run="sleep 45", timeout_ms=1_200)
    assert await runner.run("after_run", tmp_path, fatal=False) is None
    assert log.fields_for("hook timed out")["outcome"] == "ignored"


@requires_posix_shell
async def test_timeout_kills_the_whole_process_tree(tmp_path: Path, reaper: list[int]) -> None:
    """SPEC 15.4: a timed-out hook must be terminated, not merely abandoned.

    ``asyncio.wait_for`` alone leaves the shell and its children running inside
    the workspace. The grandchild's OS pid is checked directly, because the
    absence of an exception proves nothing about the process table.
    """
    runner = make_runner(before_run=GRANDCHILD_SCRIPT, timeout_ms=3_000)
    async with running_hook(runner, "before_run", tmp_path, reaper) as (task, grandchild):
        with pytest.raises(HookTimeout):
            await task
        assert await await_pid_gone(grandchild), (
            f"orphaned hook grandchild {grandchild} survived the timeout kill"
        )


@requires_posix_shell
async def test_cancelling_a_running_hook_kills_the_process_tree(
    tmp_path: Path, reaper: list[int]
) -> None:
    """Orchestrator shutdown must not leak a shell holding the workspace open."""
    runner = make_runner(before_run=GRANDCHILD_SCRIPT, timeout_ms=120_000)
    async with running_hook(runner, "before_run", tmp_path, reaper) as (task, grandchild):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await await_pid_gone(grandchild), (
            f"orphaned hook grandchild {grandchild} survived cancellation"
        )


@requires_posix_shell
async def test_timeout_is_bounded_and_reports_how_the_tree_was_killed(tmp_path: Path) -> None:
    """SPEC 15.4: the orchestrator must not hang, whatever the hook spawned.

    This asserts the *bound* and the reported kill method. It does not by itself
    prove the tree died -- the post-kill drain is capped, so it returns either
    way. ``test_timeout_kills_the_whole_process_tree`` is what proves that.
    """
    runner = make_runner(before_run=GRANDCHILD_SCRIPT, timeout_ms=2_000)
    started = time.monotonic()
    outcome = await runner.execute("before_run", tmp_path)
    assert outcome.status == "timeout"
    assert outcome.timed_out and not outcome.ok
    assert outcome.killed in ("job_object", "process_group", "taskkill", "terminate")
    assert time.monotonic() - started < 12.0


def test_invalid_timeout_degrades_to_the_spec_default() -> None:
    for bad in (0, -1, None, "60000", True):
        log = RecordingLogger()
        runner = HookRunner(FakeHookConfig(timeout_ms=bad), logger=log)  # type: ignore[arg-type]
        assert runner.timeout_ms == DEFAULT_HOOK_TIMEOUT_MS == 60_000
        assert "hook timeout invalid, using default" in log.messages("warning")
        runner.timeout_ms  # noqa: B018 - warn-once guard must not spam
        assert log.messages("warning").count("hook timeout invalid, using default") == 1


def test_valid_timeout_is_taken_from_config() -> None:
    assert HookRunner(FakeHookConfig(timeout_ms=1234), logger=RecordingLogger()).timeout_ms == 1234


# --------------------------------------------------------------------------
# SPEC 15.4 -- hook output truncation
# --------------------------------------------------------------------------


def test_truncate_for_log_keeps_short_output_verbatim() -> None:
    assert truncate_for_log("small") == "small"
    assert truncate_for_log("x" * HOOK_OUTPUT_LOG_LIMIT) == "x" * HOOK_OUTPUT_LOG_LIMIT


def test_truncate_for_log_bounds_and_marks_long_output() -> None:
    text = "".join(f"line{i}\n" for i in range(5_000))
    out = truncate_for_log(text)
    assert len(out) < len(text)
    assert len(out) <= HOOK_OUTPUT_LOG_LIMIT + 64
    assert "chars truncated" in out
    assert out.startswith("line0")
    assert out.rstrip().endswith("line4999")


@requires_posix_shell
async def test_hook_output_is_truncated_in_logs_and_error_details(tmp_path: Path) -> None:
    log = RecordingLogger()
    runner = make_runner(log, before_run="seq 1 5000; exit 4")
    with pytest.raises(HookError) as excinfo:
        await runner.run("before_run", tmp_path, fatal=True)

    logged = log.fields_for("hook failed")["output"]
    assert len(logged) <= HOOK_OUTPUT_LOG_LIMIT + 64
    assert "chars truncated" in logged
    assert excinfo.value.details["output"] == logged


# --------------------------------------------------------------------------
# SPEC 9.4 / 18.3 -- shell resolution and the documented Windows fallback
# --------------------------------------------------------------------------


def test_resolve_prefers_sh_then_bash_with_login_c() -> None:
    both = resolve_hook_shell(which={"sh": "/bin/sh", "bash": "/bin/bash"}.get, os_name="posix")
    assert (both.kind, both.executable, both.args, both.posix) == (
        "sh",
        "/bin/sh",
        ("-lc",),
        True,
    )
    only_bash = resolve_hook_shell(which={"bash": "/usr/bin/bash"}.get, os_name="posix")
    assert (only_bash.kind, only_bash.args) == ("bash", ("-lc",))


def test_windows_uses_a_posix_shell_when_one_is_installed() -> None:
    """SPEC 9.4's shell contract is honored on Windows, not replaced by cmd."""
    shell = resolve_hook_shell(
        which={"sh": r"C:\Program Files\Git\usr\bin\sh.exe"}.get, os_name="nt"
    )
    assert shell.posix is True
    assert (shell.kind, shell.args) == ("sh", ("-lc",))


def test_windows_falls_back_to_comspec_only_without_a_posix_shell() -> None:
    shell = resolve_hook_shell(
        which=lambda _name: None, os_name="nt", comspec=r"C:\Windows\System32\cmd.exe"
    )
    assert shell.posix is False
    assert shell.kind == "cmd"
    assert shell.args == ("/d", "/s", "/c")
    assert shell.argv("echo hi")[-1] == "echo hi"


@windows_only
def test_wsl_interop_launcher_is_never_selected_as_the_posix_shell() -> None:
    """``System32\\bash.exe`` runs in a different mount namespace, so cwd would lie."""
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    shell = resolve_hook_shell(
        which={"bash": os.path.join(root, "System32", "bash.exe")}.get,
        os_name="nt",
        comspec="cmd.exe",
    )
    assert shell.kind == "cmd"


@windows_only
async def test_documented_windows_cmd_fallback_actually_executes(tmp_path: Path) -> None:
    """SPEC 18.3: verify hook execution on the target host shell."""
    cmd_shell = resolve_hook_shell(which=lambda _name: None, os_name="nt")
    ok = HookRunner(
        FakeHookConfig(after_create="echo ok> made.txt"),
        shell=cmd_shell,
        logger=RecordingLogger(),
    )
    await ok.run("after_create", tmp_path, fatal=True)
    assert (tmp_path / "made.txt").read_text(encoding="utf-8").strip() == "ok"

    bad = HookRunner(
        FakeHookConfig(after_create="exit /b 5"), shell=cmd_shell, logger=RecordingLogger()
    )
    with pytest.raises(HookError) as excinfo:
        await bad.run("after_create", tmp_path, fatal=True)
    assert excinfo.value.details["exit_code"] == 5


@windows_only
async def test_windows_cmd_fallback_still_enforces_the_required_timeout(tmp_path: Path) -> None:
    cmd_shell = resolve_hook_shell(which=lambda _name: None, os_name="nt")
    runner = HookRunner(
        FakeHookConfig(after_run="ping -n 60 127.0.0.1 > nul", timeout_ms=1_200),
        shell=cmd_shell,
        logger=RecordingLogger(),
    )
    outcome = await runner.execute("after_run", tmp_path)
    assert outcome.status == "timeout"
    assert outcome.killed in ("job_object", "taskkill", "terminate")
