"""Workspace lifecycle hook execution (SPEC 9.4, SPEC 15.4).

Four hooks are defined by SPEC 5.3.4 / 9.4 -- ``after_create``, ``before_run``,
``after_run`` and ``before_remove``. They are identical in execution and differ
only in *failure semantics*, which SPEC 9.4 states as:

===============  ==========================================
hook             failure or timeout
===============  ==========================================
after_create     fatal to workspace creation
before_run       fatal to the current run attempt
after_run        logged and ignored
before_remove    logged and ignored
===============  ==========================================

That table is a property of the *call site*, not of this module: the same hook
name is a best-effort call in one branch of SPEC 16.5 and a fatal call in
another. :meth:`HookRunner.run` therefore takes ``fatal`` from the caller and
never infers it from the hook name. :func:`default_fatal` exposes the table
above for callers and tests that want to assert it, but this module's control
flow does not consult it.

Timeouts
--------
SPEC 15.4 makes hook timeouts REQUIRED "to avoid hanging the orchestrator". A
bare :func:`asyncio.wait_for` does not satisfy that requirement: it abandons the
subprocess rather than terminating it, leaving a detached shell (and any
grandchildren it spawned) running inside the workspace directory, holding the
inherited stdout pipe open and blocking workspace removal. This module kills the
whole process tree instead -- a Windows Job Object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, or a POSIX process group -- and then
drains the pipe, which also confirms no descendant survived holding it.

Shell selection
---------------
SPEC 9.4 names ``sh -lc <script>`` as the POSIX-conforming default (with
``bash -lc`` as a stricter equivalent). This module honors that contract on any
host where a POSIX shell exists, *including Windows*, where Git for Windows /
MSYS2 / Cygwin all provide one. Only when no POSIX shell is on ``PATH`` does it
fall back to ``%COMSPEC% /d /s /c`` -- and that fallback is reported through
:attr:`HookShell.kind` and logged on every hook start, so an operator can see
that hook scripts on that host are being interpreted by ``cmd.exe`` rather than
silently mis-executed. See :func:`resolve_hook_shell`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from symphony.errors import HookError, HookTimeout

if TYPE_CHECKING:  # pragma: no cover - typing only
    from symphony.workflow.config import HookConfig

__all__ = [
    "DEFAULT_HOOK_TIMEOUT_MS",
    "HOOK_NAMES",
    "HOOK_OUTPUT_LOG_LIMIT",
    "SPEC_FATAL_HOOKS",
    "HookOutcome",
    "HookRunner",
    "HookShell",
    "default_fatal",
    "resolve_hook_shell",
    "truncate_for_log",
]

#: SPEC 9.4 supported hooks, in specification order.
HOOK_NAMES: Final[tuple[str, ...]] = (
    "after_create",
    "before_run",
    "after_run",
    "before_remove",
)

#: SPEC 5.3.4 ``hooks.timeout_ms`` default. The config layer owns validation;
#: this value is the safety net so a malformed config can never mean "no
#: timeout" (SPEC 15.4 requires a timeout).
DEFAULT_HOOK_TIMEOUT_MS: Final[int] = 60_000

#: SPEC 15.4: "Hook output SHOULD be truncated in logs."
HOOK_OUTPUT_LOG_LIMIT: Final[int] = 2_000

#: SPEC 9.4 failure semantics, keyed by hook name. Advisory: see module docstring.
SPEC_FATAL_HOOKS: Final[dict[str, bool]] = {
    "after_create": True,
    "before_run": True,
    "after_run": False,
    "before_remove": False,
}

#: Preference order for the POSIX shell named by SPEC 9.4.
POSIX_SHELL_PREFERENCE: Final[tuple[str, ...]] = ("sh", "bash")

_LOGIN_SHELL_ARGS: Final[tuple[str, ...]] = ("-lc",)
_CMD_SHELL_ARGS: Final[tuple[str, ...]] = ("/d", "/s", "/c")

# How long to wait for the stdout pipe to reach EOF after a kill. Reaching EOF
# is the proof that no descendant of the hook still holds the inherited handle.
_DRAIN_TIMEOUT_S: Final[float] = 5.0

_LOGGER_NAME: Final[str] = "symphony.workspace.hooks"


def default_fatal(name: str) -> bool:
    """Return the SPEC 9.4 failure disposition for ``name``.

    Advisory only -- :meth:`HookRunner.run` takes ``fatal`` from its caller.
    Provided so call sites and conformance tests can state the spec table
    once instead of hard-coding booleans.
    """
    try:
        return SPEC_FATAL_HOOKS[name]
    except KeyError:
        raise ValueError(f"unknown hook name: {name!r}; expected one of {HOOK_NAMES}") from None


def truncate_for_log(text: str, limit: int = HOOK_OUTPUT_LOG_LIMIT) -> str:
    """Shorten hook output for logs and error details (SPEC 15.4).

    Keeps the head and the tail, because shell failure reasons are usually the
    last thing written while the command that produced them is usually the
    first.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n... [{omitted} chars truncated] ...\n{text[-tail:]}"


@dataclass(frozen=True, slots=True)
class HookShell:
    """The resolved interpreter used to run hook scripts (SPEC 9.4)."""

    executable: str
    args: tuple[str, ...]
    kind: str
    posix: bool

    def argv(self, script: str) -> list[str]:
        return [self.executable, *self.args, script]

    @property
    def display(self) -> str:
        return " ".join([self.kind, *self.args])


def _is_windows_system_launcher(path: str) -> bool:
    """True for ``%SystemRoot%\\System32\\bash.exe`` -- the WSL interop stub.

    That executable is not a shell for the Windows filesystem; it hands the
    script to a Linux distribution with a different mount namespace, so the
    workspace ``cwd`` contract in SPEC 9.4 would silently not hold. Skip it and
    keep looking for a real MSYS/Cygwin shell.
    """
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    try:
        resolved = os.path.normcase(os.path.abspath(path))
    except (OSError, ValueError):  # pragma: no cover - defensive
        return False
    return any(
        resolved.startswith(os.path.normcase(os.path.join(root, sub)) + os.sep)
        for sub in ("System32", "SysWOW64", "Sysnative")
    )


def resolve_hook_shell(
    *,
    which: Callable[[str], str | None] = shutil.which,
    os_name: str = os.name,
    comspec: str | None = None,
) -> HookShell:
    """Pick the hook interpreter, honoring SPEC 9.4's ``sh -lc`` contract.

    Resolution order:

    1. ``sh``, then ``bash``, from ``PATH`` -- invoked as ``<shell> -lc <script>``.
       This is the conforming POSIX default and is used on Windows too whenever
       Git for Windows / MSYS2 / Cygwin put such a shell on ``PATH``.
    2. On Windows with no POSIX shell: ``%COMSPEC% /d /s /c <script>``.

    The Windows fallback is a documented behavior change, not a transparent
    substitution -- ``cmd.exe`` cannot interpret POSIX shell syntax, so a
    ``WORKFLOW.md`` written against SPEC 9.4 will fail loudly there. Callers see
    which one is in use via :attr:`HookShell.kind`, and every hook start is
    logged with it.
    """
    for name in POSIX_SHELL_PREFERENCE:
        found = which(name)
        if not found:
            continue
        if os_name == "nt" and _is_windows_system_launcher(found):
            continue
        return HookShell(executable=found, args=_LOGIN_SHELL_ARGS, kind=name, posix=True)
    if os_name == "nt":
        exe = comspec or os.environ.get("COMSPEC") or "cmd.exe"
        return HookShell(executable=exe, args=_CMD_SHELL_ARGS, kind="cmd", posix=False)
    # A POSIX host without /bin/sh is not a real configuration; name the spec
    # default so the launch error points at the missing shell.
    return HookShell(executable="/bin/sh", args=_LOGIN_SHELL_ARGS, kind="sh", posix=True)


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """Result of one hook execution. Introspection surface for the RLM and tests."""

    name: str
    status: str
    exit_code: int | None
    duration_ms: int
    output: str
    killed: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skipped")

    @property
    def timed_out(self) -> bool:
        return self.status == "timeout"


# --------------------------------------------------------------------------
# Process-tree termination (SPEC 15.4)
# --------------------------------------------------------------------------


def _spawn_kwargs(os_name: str = os.name) -> dict[str, Any]:
    """Spawn options that make the hook killable as a tree."""
    if os_name == "nt":
        # No console window for a service process. The Job Object created in
        # ``_open_job`` is what makes the tree killable.
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    # New session => new process group => os.killpg reaches grandchildren.
    return {"start_new_session": True}


class _WindowsJob:
    """A Job Object that kills every process assigned to it on terminate.

    ``taskkill /F /T`` is not sufficient here: MSYS/Cygwin's fork emulation
    re-parents descendants, so the tree walk misses them and a ``sleep``
    grandchild outlives its shell. A Job Object is membership-based, so
    descendants cannot escape it.
    """

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    def __init__(self) -> None:
        import ctypes
        import ctypes.wintypes as wt

        self._ctypes = ctypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateJobObjectW.restype = wt.HANDLE
        self._k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._k32.OpenProcess.restype = wt.HANDLE
        self._k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        self._k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        self._k32.TerminateJobObject.argtypes = [wt.HANDLE, ctypes.c_uint]
        self._k32.CloseHandle.argtypes = [wt.HANDLE]
        self._k32.SetInformationJobObject.argtypes = [
            wt.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wt.DWORD,
        ]

        handle = self._k32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self._handle = handle
        info = _job_limit_struct(ctypes, wt)
        info.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k32.SetInformationJobObject(
            handle,
            self._JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            self.close()
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, pid: int) -> None:
        import ctypes

        hproc = self._k32.OpenProcess(
            self._PROCESS_TERMINATE | self._PROCESS_SET_QUOTA, False, pid
        )
        if not hproc:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not self._k32.AssignProcessToJobObject(self._handle, hproc):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            self._k32.CloseHandle(hproc)

    def terminate(self) -> bool:
        return bool(self._k32.TerminateJobObject(self._handle, 1))

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle:
            self._k32.CloseHandle(handle)


def _job_limit_struct(ctypes: Any, wt: Any) -> Any:
    class IoCounters(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_ulonglong)
            for n in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wt.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wt.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", wt.DWORD),
            ("SchedulingClass", wt.DWORD),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return EXTENDED()


async def _taskkill_tree(pid: int) -> bool:
    """Best-effort Windows fallback when a Job Object is unavailable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "taskkill",
            "/F",
            "/T",
            "/PID",
            str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:  # pragma: no cover - taskkill missing
        return False
    return await proc.wait() == 0


def _killpg(pid: int) -> bool:
    """POSIX: signal the whole process group created by ``start_new_session``."""
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


# --------------------------------------------------------------------------
# Logging (SPEC 13.1)
# --------------------------------------------------------------------------


class _StdlibLogger:
    """Fallback for ``symphony.observability.logging`` while it is being written.

    Renders the same ``key=value`` phrasing SPEC 13.1 requires, so log output is
    stable whichever backend is present.
    """

    def __init__(self, name: str) -> None:
        import logging

        self._log = logging.getLogger(name)

    @staticmethod
    def _render(msg: str, fields: dict[str, Any]) -> str:
        if not fields:
            return msg
        return msg + " " + " ".join(f"{k}={v}" for k, v in fields.items())

    def debug(self, msg: str, **fields: Any) -> None:
        self._log.debug(self._render(msg, fields))

    def info(self, msg: str, **fields: Any) -> None:
        self._log.info(self._render(msg, fields))

    def warning(self, msg: str, **fields: Any) -> None:
        self._log.warning(self._render(msg, fields))

    def error(self, msg: str, **fields: Any) -> None:
        self._log.error(self._render(msg, fields))


def _default_logger() -> Any:
    try:
        from symphony.observability.logging import get_logger
    except ImportError:
        return _StdlibLogger(_LOGGER_NAME)
    return get_logger(_LOGGER_NAME)


# --------------------------------------------------------------------------
# HookRunner
# --------------------------------------------------------------------------


class HookRunner:
    """Runs the SPEC 9.4 workspace lifecycle hooks with a REQUIRED timeout.

    ``cfg`` is a ``symphony.workflow.config.HookConfig``: four optional script
    strings named by :data:`HOOK_NAMES` plus ``timeout_ms``. Only attribute
    access is used, so the runner is decoupled from that module's construction.
    """

    def __init__(
        self,
        cfg: HookConfig,
        *,
        shell: HookShell | None = None,
        logger: Any | None = None,
    ) -> None:
        self._cfg = cfg
        self.shell = shell or resolve_hook_shell()
        self._log = logger if logger is not None else _default_logger()
        self._warned_bad_timeout = False

    # -- public API --------------------------------------------------------

    async def run(self, name: str, cwd: Path, *, fatal: bool) -> None:
        """Run hook ``name`` in ``cwd`` (SPEC 9.4).

        ``fatal=True`` raises :class:`~symphony.errors.HookError` on a non-zero
        exit or launch failure and :class:`~symphony.errors.HookTimeout` on a
        timeout. ``fatal=False`` logs the failure and returns -- SPEC 9.4's
        "logged and ignored". A missing or blank script is a no-op either way.

        The caller supplies ``fatal``; see :func:`default_fatal` for the spec's
        per-hook table and the module docstring for why it is not applied here.
        """
        outcome = await self.execute(name, cwd)
        if outcome.ok:
            return

        details: dict[str, Any] = {
            "hook": outcome.name,
            "cwd": str(cwd),
            "exit_code": outcome.exit_code,
            "duration_ms": outcome.duration_ms,
            "output": outcome.output,
        }
        if outcome.timed_out:
            message = f"hook {outcome.name} timed out after {self.timeout_ms} ms"
            details["timeout_ms"] = self.timeout_ms
            details["killed"] = outcome.killed
            error: HookError | HookTimeout = HookTimeout(message, **details)
            event = "hook timed out"
        else:
            message = f"hook {outcome.name} failed with exit code {outcome.exit_code}"
            error = HookError(message, **details)
            event = "hook failed"

        self._log.error(
            event,
            hook=outcome.name,
            outcome="aborting" if fatal else "ignored",
            fatal=fatal,
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            killed=outcome.killed,
            output=outcome.output,
        )
        if fatal:
            raise error

    async def execute(self, name: str, cwd: Path) -> HookOutcome:
        """Run hook ``name`` and report the result without raising.

        Failure disposition is :meth:`run`'s job; this is the introspection
        entry point. Only a bad hook name or an unusable ``cfg`` raises.
        """
        script = self.script_for(name)
        if script is None:
            return HookOutcome(name=name, status="skipped", exit_code=None, duration_ms=0, output="")

        timeout_ms = self.timeout_ms
        argv = self.shell.argv(script)
        self._log.info(
            "hook started",
            hook=name,
            cwd=str(cwd),
            shell=self.shell.display,
            timeout_ms=timeout_ms,
        )
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        job = self._open_job()
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.DEVNULL,
                    env=self._child_env(),
                    **_spawn_kwargs(),
                )
            except OSError as exc:
                return HookOutcome(
                    name=name,
                    status="failed",
                    exit_code=None,
                    duration_ms=elapsed_ms(),
                    output=f"could not launch hook shell {self.shell.executable!r}: {exc}",
                )

            if job is not None:
                try:
                    job.assign(proc.pid)
                except OSError:
                    job.close()
                    job = None

            communicate = asyncio.ensure_future(proc.communicate())
            try:
                raw, _ = await asyncio.wait_for(
                    asyncio.shield(communicate), timeout=timeout_ms / 1000
                )
            except TimeoutError:
                killed = await self._kill_tree(proc, job)
                raw = await self._drain(communicate)
                return HookOutcome(
                    name=name,
                    status="timeout",
                    exit_code=proc.returncode,
                    duration_ms=elapsed_ms(),
                    output=truncate_for_log(_decode(raw)),
                    killed=killed,
                )
            except asyncio.CancelledError:
                # Orchestrator shutdown or an aborted attempt must not leave the
                # hook running in a workspace that is about to be removed.
                await self._kill_tree(proc, job)
                with contextlib.suppress(asyncio.CancelledError):
                    await self._drain(communicate)
                raise
        finally:
            if job is not None:
                job.close()

        exit_code = proc.returncode
        output = truncate_for_log(_decode(raw))
        status = "ok" if exit_code == 0 else "failed"
        if status == "ok":
            self._log.info(
                "hook completed",
                hook=name,
                exit_code=exit_code,
                duration_ms=elapsed_ms(),
            )
        return HookOutcome(
            name=name,
            status=status,
            exit_code=exit_code,
            duration_ms=elapsed_ms(),
            output=output,
        )

    def script_for(self, name: str) -> str | None:
        """The configured script for ``name``, or ``None`` when not configured.

        Raises ``ValueError`` for a name outside :data:`HOOK_NAMES` -- an
        unknown hook is a programming error, so it is reported even on the
        non-fatal path rather than silently doing nothing.
        """
        if name not in HOOK_NAMES:
            raise ValueError(f"unknown hook name: {name!r}; expected one of {HOOK_NAMES}")
        script = getattr(self._cfg, name, None)
        if script is None:
            return None
        if not isinstance(script, str) or not script.strip():
            return None
        return script

    @property
    def timeout_ms(self) -> int:
        """``hooks.timeout_ms`` (SPEC 5.3.4), falling back to the spec default.

        The config layer rejects invalid values (SPEC 5.3.4); if one reaches
        here anyway, degrade to :data:`DEFAULT_HOOK_TIMEOUT_MS` rather than
        raise. SPEC 15.4 requires a timeout, and a runner that raises on the
        ``after_run`` path would convert an ignorable condition into a fatal
        one.
        """
        raw = getattr(self._cfg, "timeout_ms", None)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            return raw
        if not self._warned_bad_timeout:
            self._warned_bad_timeout = True
            self._log.warning(
                "hook timeout invalid, using default",
                configured=raw,
                default_ms=DEFAULT_HOOK_TIMEOUT_MS,
            )
        return DEFAULT_HOOK_TIMEOUT_MS

    # -- internals ---------------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        """Hook environment.

        Hooks are fully trusted configuration (SPEC 15.4), so the host
        environment passes through unfiltered -- unlike the coding-agent child,
        which SPEC 15.3 requires be stripped of tracker credentials. Nothing
        here is logged.
        """
        env = dict(os.environ)
        if os.name == "nt" and self.shell.posix:
            # MSYS/Cygwin login shells (`-l`) source a profile that may `cd
            # $HOME`; CHERE_INVOKING keeps the workspace as cwd, which SPEC 9.4
            # requires.
            env.setdefault("CHERE_INVOKING", "1")
        return env

    def _open_job(self) -> _WindowsJob | None:
        if os.name != "nt":
            return None
        try:
            return _WindowsJob()
        except (OSError, AttributeError):  # pragma: no cover - no Win32 access
            self._log.warning("hook job object unavailable, falling back to taskkill")
            return None

    async def _kill_tree(self, proc: asyncio.subprocess.Process, job: _WindowsJob | None) -> str:
        """Terminate the hook and every process it spawned. Returns the method used."""
        if proc.returncode is not None:
            return "already_exited"
        if os.name == "nt":
            if job is not None and job.terminate():
                return "job_object"
            if await _taskkill_tree(proc.pid):
                return "taskkill"
        elif _killpg(proc.pid):
            return "process_group"
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        return "terminate"

    @staticmethod
    async def _drain(communicate: asyncio.Future[tuple[bytes, bytes]]) -> bytes:
        """Collect output after a kill.

        Reaching EOF here is the observable proof that no surviving descendant
        holds the inherited stdout handle; if one did, this would time out.
        """
        try:
            raw, _ = await asyncio.wait_for(
                asyncio.shield(communicate), timeout=_DRAIN_TIMEOUT_S
            )
        except (TimeoutError, OSError, ValueError):
            communicate.cancel()
            with contextlib.suppress(BaseException):
                await communicate
            return b""
        return raw


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")
