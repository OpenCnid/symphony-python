# SSH Worker Extension

Implementation notes for `src/symphony/ssh/worker.py` — the OPTIONAL remote-execution
profile described in **SPEC Appendix A**.

This extension is off unless `worker.ssh_hosts` is present and non-empty. With the key
omitted, `HostPool.enabled` and `SSHWorker.enabled` are `False`, every assignment entry
point raises `NoSSHHostsConfigured`, and nothing in the local execution path is touched.

---

## Configuration

```yaml
worker:
  ssh_hosts:
    - build-1
    - deploy@build-2:2222
    - user@[2001:db8::1]:22
  max_concurrent_agents_per_host: 2

workspace:
  root: /srv/symphony/workspaces   # interpreted ON THE REMOTE HOST
```

Both keys are already parsed by `symphony.workflow.config` into
`ServiceConfig.ssh_hosts` and `ServiceConfig.max_concurrent_agents_per_host`.

### `workspace.root` is read remotely, from `cfg.raw`

SPEC A.1: *"`workspace.root` is interpreted on the remote host, not on the orchestrator
host."*

`ServiceConfig.workspace_root` is a **local** `pathlib.Path`: it has been expanded and
made absolute against the orchestrator's filesystem, and on a Windows orchestrator it is
rendered with a drive letter and backslashes. None of that is meaningful remotely, so
`remote_workspace_root(cfg)` reads the verbatim front-matter string out of `cfg.raw`
instead.

Consequences worth knowing before you write a workflow file:

| Input | Behavior | Why |
|---|---|---|
| `/srv/ws` | accepted | absolute POSIX path |
| `~/ws` | **rejected** | commands are single-quoted, so the remote shell would not expand `~`; it would create a directory literally named `~` |
| `$HOME/ws` | **rejected** | same reason |
| `C:\ws` | **rejected** | not an absolute POSIX path |
| `/srv/../etc` | **rejected** | `..` is never collapsed (see below) |
| `//srv/ws` | **rejected** | POSIX leaves a leading `//` implementation-defined |

Pass `SSHWorker(cfg, remote_root=...)` to override the config value explicitly.

---

## Remote path safety (SPEC 9.5, 15.2 under Appendix A semantics)

`symphony.workspace.safety` is **not** used for remote paths. It calls `Path.resolve()`,
which consults the orchestrator's filesystem — it would resolve *local* symlinks that have
nothing to do with the remote host, and on Windows would rewrite the path entirely. Remote
containment is enforced by `assert_remote_within_root`, which is pure `PurePosixPath`
arithmetic and touches no filesystem.

Differences from the local check, all deliberate:

- **Case-sensitive.** The local check case-folds for Windows. The remote host is POSIX, so
  folding would merge `/srv/WS` and `/srv/ws`, which are two real directories.
- **`..` is rejected, not collapsed.** Lexically collapsing `/root/ws/link/..` yields
  `/root/ws`, but the remote shell resolves it to the *link's* parent. A collapse would
  approve a path that lands somewhere else.
- **Strictly inside.** The root is not inside the root, so a degenerate key can never make
  cleanup target every workspace on the host.

### Two checks, because one cannot be enough

A lexical check runs when the assignment is built. It cannot see remote symlinks — no
purely local computation can. So `SSHWorker.preflight()` runs a second check against the
paths the remote shell actually resolved:

```
cd -- /srv/ws && test -w . && pwd -P && cd -- /srv/ws/ABC-1 && test -w . && pwd -P
```

`pwd -P` is symlink-resolved, so if `ABC-1` is a symlink to `/tmp/elsewhere`, the second
line reveals it and containment fails before anything is launched. `test -w .` covers the
"writable workspace root" half of the SPEC A.1 remote worker contract.

Run `preflight()` before launching, and note that `cleanup()` runs it too — so a symlinked
workspace cannot redirect the recursive remove.

### Quoting is inside the safety boundary, not after it

SPEC A.3 names this directly: *"Remote path resolution, shell quoting, and
workspace-boundary checks matter more once execution crosses a machine boundary."* A path
validated as a Python string is not what the remote shell receives — the shell receives the
*quoted* rendering, and a quoting mistake silently changes which directory `cd` enters.

`build_remote_command()` therefore does not trust its own quoting:

1. it takes an explicit token list;
2. it POSIX single-quotes every token that is not an operator;
3. it re-parses the finished line with `shlex.split` and requires the tokens back
   **byte-identical**, raising `RemoteQuotingError` otherwise.

Step 3 is the load-bearing one. It means "the path that was validated" and "the path the
remote `cd` receives" are provably the same string, rather than the same string modulo a
quoting function nobody re-checked.

Operator-ness is carried by a private `str` subclass, not by text. A token whose value
happens to be `&&` is data and gets quoted; only the internal sentinel is emitted as shell
syntax. No caller-supplied string — a workspace path, a `codex.command` — can promote
itself into syntax by spelling itself like an operator.

`ssh` hands its command argument to the remote login shell, which parses it exactly once,
so one round of quoting is the correct amount.

### Host entries are validated against option injection

`SSHHost.parse` rejects any entry beginning with `-`. Without that, a `worker.ssh_hosts`
entry of `-oProxyCommand=...` would be read by the local `ssh` client as an option, turning
a workflow file into arbitrary local command execution. Whitespace and control characters
are rejected for the same reason.

---

## Host pool, assignment, and saturation

```python
pool = HostPool.from_config(cfg)
async with pool.lease("ABC-1", remote_root="/srv/ws", prefer=previous_host) as assignment:
    ...   # one worker lifetime
```

`HostAssignment` is the run's **execution identity** (SPEC A.1): host plus remote
workspace. It is created once per worker lifetime and reused, which is what keeps
continuation turns on the same host and workspace — there is no per-turn reassignment path
to get wrong.

**Selection** prefers the previously used host when it still has capacity (SPEC A.2), since
remote workspaces are host-local and moving is a cold restart (SPEC A.3). Otherwise the
least-loaded host wins, with config order as a deterministic tie-break.

**Saturation reduces capacity; it never causes fallback.** SPEC A.2: *"When all SSH hosts
are at capacity, dispatch SHOULD wait rather than silently falling back to a different
execution mode."*

| Situation | Behavior |
|---|---|
| A host is at its per-host cap | skipped as a candidate |
| Every host is at capacity | `try_acquire()` returns `None`; `acquire()` blocks |
| `acquire(timeout_ms=...)` expires | raises `HostPoolSaturated` |
| A host is marked unreachable | removed from candidates, capacity shrinks |
| **Every** host is unreachable | dispatch waits — there is no local fallback path |

`try_acquire() -> None` means *leave this issue unclaimed on this tick*. It never means
"run it locally". `mark_unreachable` / `mark_reachable` are the health hooks; marking a host
reachable again wakes waiters.

This pool caps **per host only**. The global `agent.max_concurrent_agents` and the
per-state limits remain the orchestrator's (SPEC 8.3); nothing here second-guesses them.

---

## The failover boundary

This is the correctness-critical part of the extension. SPEC A.2 permits failover to another
host *"before work has meaningfully started"*, and requires that a rerun after that point
*"SHOULD be treated as a new attempt, not as invisible failover"*. SPEC A.3 asks
implementations to distinguish host-connectivity/startup failures from in-workspace agent
failures *"so the same ticket is not accidentally re-executed on multiple hosts"*.

### Classification is a hint; the latch is the boundary

`classify_failure()` maps an exception to a `FailureStage`
(`CONNECT` / `PREFLIGHT` / `STARTUP` / `AGENT` / `UNKNOWN`). It is deliberately **not** the
safety mechanism, because the classes genuinely overlap: a dropped SSH channel raises the
same `SSHHostUnreachable` whether it happened while dialing or halfway through a turn. An
implementation that failed over on every `CONNECT` error would re-run a ticket that is still
executing on the first host — exactly the hazard A.3 names.

`RunProgress` is a **monotonic latch**. It flips the first moment the remote side could have
produced an effect a second execution would duplicate, and never clears:

| Call | Latches? | Reasoning |
|---|---|---|
| `mark_workspace_prepared()` | no | host-local and idempotent; A.3 already calls a host move a cold restart |
| `mark_hook_started(name)` | **yes** | hooks are repo-owned scripts (SPEC 9.4, 15.4) and may push, deploy, or notify; once one starts it is unknowable whether it finished |
| `mark_turn_dispatched(n)` | **yes** | call this *before* writing the prompt, never after a response — a `sendMessage` that never returns is precisely when the agent may be working right now |
| `mark_side_effect(reason)` | **yes** | explicit escape hatch |

### Decision table

`decide_failover(progress, exc, hosts_remaining=...)`, applied in order:

| Condition | Action |
|---|---|
| Containment violation or quoting error | `FAIL` — host-independent, reproduces identically everywhere, and SPEC 9.5 errors are documented as never retried blindly |
| `side_effects_possible` is set | `NEW_ATTEMPT` — **regardless of stage** |
| Stage is `AGENT` | `NEW_ATTEMPT` — belt-and-braces; reaching an agent error means a turn ran |
| Stage is `CONNECT`/`PREFLIGHT`/`STARTUP` and another host is free | `RETRY_OTHER_HOST` |
| Same, but no host available | `NEW_ATTEMPT` |
| `UNKNOWN` | `NEW_ATTEMPT` — never a silent rerun |

`RETRY_OTHER_HOST` is the only transparent path, and it is reachable only with the latch
clear. Everything else hands back to the orchestrator's SPEC 8.4 retry ladder as a visible
new attempt. **`decide_failover` returns a recommendation, not an action** — the
orchestrator remains the single source of truth for retries (SPEC A.1).

Worked example of the case this exists for:

```python
progress = worker.progress_for(assignment)
progress.mark_turn_dispatched(1)          # prompt about to go on the wire
# ... the SSH channel drops ...
d = decide_failover(progress, SSHHostUnreachable("channel closed"), hosts_remaining=3)
d.stage    # FailureStage.CONNECT     <- classification says "connectivity"
d.action   # FailoverAction.NEW_ATTEMPT <- the latch overrules it
```

---

## Remote launch

SPEC A.1: *"The coding-agent app-server is launched over SSH stdio instead of as a local
subprocess, so the orchestrator still owns the session lifecycle even though commands
execute remotely."*

SPEC 10.1's launch contract is honored literally, one machine over:

```
ssh -T -o BatchMode=yes ... build-1 "cd -- '/srv/ws/ABC-1' && exec bash -lc 'codex app-server'"
```

- `cd --` fixes the working directory remotely, which is where SPEC 9.5 Invariant 1 has to
  be enforced.
- `exec` replaces the remote login shell with the agent, so closing the SSH channel reaches
  the agent rather than an intermediate shell.
- `bash -lc <codex.command>` is SPEC 10.1's invocation, unchanged.

`RemoteAppServerClient` subclasses `AppServerClient` and inherits the entire transport,
turn state machine, timeout mapping, and approval policy. Two overrides are forced by the
machine boundary:

- **`self.workspace` becomes a `PurePosixPath`.** Every protocol payload sends
  `str(self.workspace)`; a local `Path` would put `\srv\ws\ABC-1` on the wire from a Windows
  orchestrator. `PurePosixPath` also has no filesystem methods, so an accidental local
  existence check is a type error rather than a silently wrong answer about a remote
  directory.
- **`_assert_launch_cwd` enforces Invariant 1 remotely.** The base implementation calls
  `Path.is_dir()` — asking the orchestrator's filesystem whether a *remote* directory
  exists, a question it cannot answer. Existence and writability come from `preflight()`
  instead; the override enforces the containment half and fails closed before any `ssh`
  process starts.

`SSHTransport` is a `Protocol`, so the default suite injects a fake and never dials out.
`OpenSSHTransport` is the real implementation.

### Secrets

SPEC 15.3: *"Adapters MUST declare secret environment names so local and remote launchers
can remove them from child environments."* The local `ssh` process receives the
orchestrator environment minus the declared secret names (it needs `HOME` and
`SSH_AUTH_SOCK`), and the generated remote command carries **no environment assignments at
all** — nothing from the orchestrator's environment is forwarded across the connection.
This is stricter than the SPEC requires and removes the question of tracker credentials
reaching a remote child. Remote credentials must be provisioned on the host.

### Cleanup and observability

SPEC A.3 asks that operators know which host owns a run, where its workspace lives, and
whether cleanup happened on the right machine.

- `HostAssignment.to_dict()` — host, hostname, port, workspace path, remote root, attempt.
- `HostPool.snapshot()` / `SSHWorker.snapshot()` — per-host in-use counts, reachability,
  capacity, last error.
- `SSHWorker.cleanup(assignment)` runs on `assignment.host` and nowhere else, re-runs
  preflight first, and returns whether the remove succeeded.

---

## Error taxonomy

`symphony/errors.py` is immutable and predates this OPTIONAL appendix, so the extension's
categories are defined in `worker.py`. Every class still descends from a class in
`errors.__all__`, so existing handling keeps working:

| Class | Category | Base |
|---|---|---|
| `SSHWorkerError` | `ssh_worker_error` | `AgentError` |
| `NoSSHHostsConfigured` | `ssh_no_hosts_configured` | `SSHWorkerError` |
| `SSHHostUnreachable` | `ssh_host_unreachable` | `SSHWorkerError` |
| `RemotePreflightFailed` | `ssh_remote_preflight_failed` | `SSHWorkerError` |
| `RemoteQuotingError` | `ssh_remote_quoting_error` | `SSHWorkerError` |
| `HostPoolSaturated` | `ssh_host_pool_saturated` | `SSHWorkerError` |
| `RemoteWorkspacePathEscapesRoot` | `workspace_path_escapes_root` *(inherited)* | `WorkspacePathEscapesRoot` |

The last row is intentional: a remote containment violation keeps the SPEC 9.5 category so
the orchestrator's existing "never recoverable, never retried blindly" handling applies
without it knowing this extension exists.

---

## Operational notes

- **Remote environment drift (SPEC A.3).** Each host needs the shell environment, the
  coding-agent executable, auth, and repository prerequisites. `preflight()` checks the
  workspace root's existence, writability, and containment; it does **not** verify that
  `codex.command` resolves on the host. A missing binary surfaces as a shell exit 127 at
  launch, which `AppServerClient` already maps to `CodexNotFound` — classified `STARTUP`,
  so it fails over to another host if nothing has started yet.
- **Keepalives.** `DEFAULT_SSH_OPTIONS` sets `BatchMode=yes` (a password prompt would stall
  a worker forever), `ConnectTimeout=10`, and `ServerAliveInterval=15` /
  `ServerAliveCountMax=3` so a silently dead host becomes a prompt `CONNECT` failure rather
  than an indefinite hang.
- **Remote process reaping.** Stopping the session terminates the local `ssh` client, which
  closes the channel; `exec` means the remote agent receives the hangup directly. Without a
  TTY this is not guaranteed on every host — if orphaned agents are observed, either enable
  a server-side reaper or run the agent under a supervisor that exits with its session.
- **Windows orchestrator.** Supported. The remote host is assumed POSIX; every remote path
  is a `PurePosixPath` and every command is POSIX-quoted on all platforms. The local `ssh`
  binary must be on `PATH` (OpenSSH ships with Windows 10+); a missing one raises
  `SSHHostUnreachable`, classified `CONNECT`.

---

## Testing

`tests/test_ssh_worker.py` — 114 tests, no network. `SSHTransport` is injected as a fake
throughout; the one test needing a reachable machine is marked `@pytest.mark.integration`
and skips unless `SYMPHONY_TEST_SSH_HOST` and `SYMPHONY_TEST_SSH_ROOT` are set.

```
./.venv/Scripts/python.exe -m pytest tests/test_ssh_worker.py -q
```

The tests are written to fail on regression of the hazards rather than the happy path:
hostile-path quoting round trips, the remote-symlink escape that only `pwd -P` reveals, and
the connect-error-after-dispatch case that must not become transparent failover.
