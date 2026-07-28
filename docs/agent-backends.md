# Agent Backends

Symphony runs the actual work in a coding agent. **SPEC 10** describes that boundary
using Codex as its worked example, but nothing it requires of Symphony — launch in the
per-issue workspace, render the first turn from the workflow template, continue on the
same conversation, forward structured events, never stall — is Codex-specific.

`src/symphony/agent/base.py` is that seam. Two backends ship:

| `agent.kind` | Module | Transport | Protocol verified? |
|---|---|---|---|
| `codex` (default) | `agent/app_server.py` | JSON-RPC over stdio to a long-lived `codex app-server` | **No** — written against documentation, never run |
| `claude` | `agent/claude.py` | NDJSON `stream-json` on stdout, one `claude --print` process per turn | **Yes** — observed against `claude 2.1.214` |

That asymmetry is the single most important thing to carry into a decision, and it cuts
both ways: the Claude backend's *wire contract* is verified while the Codex one's is not,
and the Codex backend's *Symphony-side behavior* is covered by tests while the Claude
one's is not covered at all. See [Verification status](#verification-status).

---

## Choosing a backend

| If you need… | Use |
|---|---|
| The agent to move issues or comment on the tracker from inside the run | `codex` — the Claude driver does not advertise tracker tools ([gap 1](#known-gaps)) |
| Remote execution via `worker.ssh_hosts` | `codex` — the SSH worker is Codex-only ([gap 5](#known-gaps)) |
| Spend visibility in USD, or a hard budget ceiling | `claude` |
| Per-pattern tool allow/deny (`Bash(git *)`, `Read(src/**)`) | `claude` |
| Model and reasoning-effort selection per workflow | `claude` |
| Rate-limit windows and reset times surfaced to the orchestrator | `claude` |
| To run on Windows without Git Bash on `PATH` | `claude` — SPEC 10.1 mandates `bash -lc` for Codex |
| A wire protocol somebody has actually watched | `claude` |
| The literal SPEC 10 worked example, with test coverage behind it | `codex` |

Switching is one line, because each backend reads its own front-matter block:

```yaml
agent:
  kind: claude        # was: codex
```

---

## Selecting a backend

`agent.kind` is an **implementation extension**, not a spec field. SPEC 5.3.5 lists no
`kind` under `agent`, but SPEC 5.3 states that "the workflow front matter is extensible"
and that unknown keys SHOULD be ignored, so both the key and the top-level `claude:`
block sit inside what the spec sanctions.

- **Default:** `codex` (`workflow/config.py::DEFAULT_AGENT_KIND`, mirrored by
  `agent/base.py::DEFAULT_BACKEND`). Codex is the default because it is the
  specification's worked example; selecting Claude Code is a deliberate act.
- **Each backend owns one block.** `AgentBackendSpec.config_key` names it: `agent.kind:
  codex` reads `codex:`, `agent.kind: claude` reads `claude:`. The unselected block is
  parsed by nobody, so a single `WORKFLOW.md` can carry both and switch with one line.
- **`ServiceConfig.codex` always exists** regardless of `agent.kind`, because it predates
  the abstraction and other modules still read it. `ServiceConfig.agent_config` holds the
  typed settings for the *selected* backend — a `CodexConfig` or a `ClaudeConfig`.

```yaml
---
tracker:
  kind: linear
  provider:
    api_key: $LINEAR_API_KEY
    team_key: ENG
agent:
  kind: claude
  max_turns: 20               # orchestrator continuation turns — NOT claude.max_turns
codex:                        # ignored while kind is claude, kept so the switch back is free
  command: codex app-server
claude:
  model: opus
  effort: high
  permission_mode: bypassPermissions
  max_budget_usd: 25.0
  disallowed_tools: ["WebFetch"]
---
```

### Preflight

SPEC 6.3 check 4 ("`codex.command` is present and non-empty") is generalized in
`workflow/config.py::validate_dispatch_config` to whichever backend is selected. Before
dispatching new work the scheduler rejects:

- an `agent.kind` no backend registered — the error lists the supported kinds;
- an empty `command` for the selected backend — reported as `codex.command` or
  `claude.command` to match what the operator wrote.

Both raise `ConfigValidationError`, and per SPEC 6.2 an invalid config stalls dispatch
rather than taking the service down.

> [!NOTE]
> Backends register lazily, on first call to `backend_kinds()` / `backend_spec()`.
> `agent/base.py::_ensure_loaded` imports each backend module inside a `try`, so a
> backend that fails to import degrades to "unsupported agent.kind" instead of breaking
> the package. If `agent.kind: claude` is rejected as unsupported on a machine where it
> should work, suspect an import error in `symphony/agent/claude.py`, not the config.

---

## The abstraction

A backend implements two runtime-checkable protocols from `agent/base.py`. Neither
mentions a wire format, a subprocess, or a spec section number.

```python
class CodingAgentSession(Protocol):
    thread_id: str                      # stable for the session; SPEC 4.2 builds
    async def stop(self) -> None: ...   # session_id = "<thread_id>-<turn_id>" from it

class CodingAgentClient(Protocol):
    async def start_session(self) -> CodingAgentSession: ...
    async def run_turn(self, session, prompt, *, title: str | None = None) -> None: ...
    async def stop(self) -> None: ...
```

Contract points a third backend has to honor:

- **`thread_id` is immutable for the session's lifetime.** SPEC 4.2 composes the
  orchestrator-visible `session_id` from it plus a per-turn id.
- **`run_turn` returns only when the turn has terminated**, and never blocks
  indefinitely (SPEC 10.5). Failure is raised as one of the typed errors in
  `symphony.errors`; the runner turns that into a worker failure and the orchestrator
  into a retry.
- **Both `stop()` methods are idempotent.** SPEC 16.5 calls them on every exit path,
  including ones that already failed.
- **The client is constructed per attempt** and bound to one workspace, because SPEC 9.5
  Invariant 1 requires the working directory to be the per-issue workspace.

Registration is a single call, and every backend receives the identical factory
invocation from `build_agent_client`, so `agent/runner.py` composes one way regardless of
which backend it is holding:

```python
register_backend(AgentBackendSpec(
    kind="claude",              # the agent.kind value
    config_key="claude",        # the front-matter block it owns
    factory=_build,             # (agent_config, *, workspace, tool_specs, tool_executor,
    description="...",          #  on_event, secret_env_names, approval_decider, **extra)
))
```

`workflow/config.py::_agent_backend_config` maps the block to a typed object. It
special-cases `codex` (whose typed view predates the abstraction) and `claude`; any other
registered kind receives its raw block as a `dict`.

---

## `claude:` reference

Every field of `ClaudeConfig` (`src/symphony/agent/claude.py`), its default, and the CLI
flag it produces. Flag semantics below were read from `claude --help` at **2.1.214**.

### Launch

| Field | Default | Flag | Notes |
|---|---|---|---|
| `command` | `claude` | *(argv[0])* | Resolved through `PATH` by `shutil.which`, which honors `PATHEXT` so a `.cmd`/`.exe` shim is found on Windows. **Unlike `codex.command`, this is an executable, not a shell command line** — only the first whitespace-delimited token is used and any arguments you append are silently dropped. Use `extra_args` for flags. |
| `extra_args` | `()` | *(appended verbatim)* | Escape hatch, added last. Nothing validates these; a flag that contradicts one the driver already emitted is your problem. |

### Model and effort

| Field | Default | Flag | Notes |
|---|---|---|---|
| `model` | *unset* | `--model` | Alias (`opus`, `sonnet`, `haiku`, `fable`) or a full model id. **Not validated at parse time** — a bad value fails later, at API call time. Unset means the CLI's own default. |
| `effort` | *unset* | `--effort` | `low`, `medium`, `high`, `xhigh`, `max`. An unrecognized value does not fail: the CLI prints `Warning: Unknown --effort value ... using the default effort` **on stderr** and continues. That warning lands in `stderr_tail()`, not in the protocol stream. |

### Permissions and tools

| Field | Default | Flag | Notes |
|---|---|---|---|
| `permission_mode` | `bypassPermissions` | `--permission-mode` | One of `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`. An unrecognized value in the workflow file **silently falls back to the default** rather than failing validation. The CLI itself enforces the choice list and would reject anything else at parse time. |
| `allowed_tools` | `()` | `--allowedTools` | Joined with commas. Supports patterns: `Bash(git *)`, `Read(src/**)`, `mcp__server__*`. A bare string is accepted as a one-element list. |
| `disallowed_tools` | `()` | `--disallowedTools` | Same shape; deny wins. |

> [!WARNING]
> `bypassPermissions` is the default because SPEC 15.1 requires a stated posture and this
> implementation targets **trusted environments**, matching the Codex backend's documented
> auto-approval. It bypasses every permission check in the agent. If that is not your
> posture, set `permission_mode` and constrain `disallowed_tools` explicitly.
>
> `manual` is accepted by the config layer but **is not a supported posture**: how an
> interactive prompt surfaces in `stream-json` was never observed, and SPEC 10.5 forbids a
> run that stalls waiting for one. It is selectable only so an operator who has verified
> the behavior can choose it knowingly.

### Budget and turn caps

| Field | Default | Flag | Notes |
|---|---|---|---|
| `max_turns` | *unset* | `--max-turns` | Caps agentic turns **inside one `claude` invocation**. Distinct from `agent.max_turns`, which caps Symphony's continuation turns across invocations. `0` or negative means unset (flag omitted). Accepted by the CLI parser at 2.1.214 but **not listed in `claude --help`**; its effect was not observed. |
| `max_budget_usd` | *unset* | `--max-budget-usd` | Hard spend ceiling for the invocation; `--print` only. `0` or negative means unset. Applies **per invocation, not per session** — the driver re-passes it on every continuation turn, so a workflow's total spend is bounded by roughly `agent.max_turns × max_budget_usd`, not by `max_budget_usd`. |

### Context and extensions

| Field | Default | Flag | Notes |
|---|---|---|---|
| `system_prompt` | *unset* | `--system-prompt` | Replaces the default system prompt entirely. |
| `append_system_prompt` | *unset* | `--append-system-prompt` | Appends to it. Prefer this. |
| `add_dirs` | `()` | `--add-dir` (repeated) | Extra readable directories outside the workspace. |
| `mcp_config` | `()` | `--mcp-config` (repeated) | MCP servers, as JSON file paths or inline JSON strings. |
| `settings` | *unset* | `--settings` | Settings file path or inline JSON. |
| `agents` | *unset* | `--agents` | A **JSON object string** defining custom subagents, e.g. `'{"reviewer": {"description": "...", "prompt": "..."}}'`. |
| `bare` | `false` | `--bare` | Minimal mode: skips hooks, LSP, plugin sync, auto-memory, keychain reads, and `CLAUDE.md` auto-discovery. |

> [!WARNING]
> `--bare` restricts Anthropic auth to `ANTHROPIC_API_KEY` or `apiKeyHelper`; **OAuth and
> keychain are never read**. A host authenticated interactively fails with
> `Not logged in · Please run /login`. Observed directly — see
> [`docs/claude-protocol.md`](claude-protocol.md) §1. Set `bare: true` only on API-key
> deployments.

> [!NOTE]
> `~` and `$VAR` are **not** expanded in any of these values. SPEC 6.1 restricts expansion
> to local filesystem path values, and this implementation applies it only to
> `workspace.root`. `add_dirs: ["~/shared"]` is passed to the CLI as the literal string
> `~/shared`.

### Session identity

| Field | Default | Flag | Notes |
|---|---|---|---|
| `deterministic_session_id` | `true` | *(chooses the `--session-id` value)* | When true and an issue identifier is available, the session uuid is `uuid5(NAMESPACE_URL, "symphony:claude:<identifier>")`, so the same issue always maps to the same conversation. When false, a fresh `uuid4` per attempt. |
| `session_persistence` | `true` | `--no-session-persistence` when false | Setting this false stops the transcript being written to disk — which also makes `--resume` impossible, so continuation turns cannot work. |
| `fork_session` | `false` | `--fork-session` | Only emitted on resume turns. Branches to a new session id instead of reusing the original. |

Turn 1 emits `--session-id <uuid>`; every later turn on the same client emits
`--resume <uuid>`. A cold client (a retry, or a restart) cannot know the session
already exists, so its first turn self-heals: Claude Code rejects a reused
`--session-id` with `Session ID <uuid> is already in use.`, and the driver flips to
`--resume` and retries once.

### Timeouts

| Field | Default | Codex default | Meaning |
|---|---|---|---|
| `read_timeout_ms` | `60000` | `5000` | Bounds the wait for the **first** stream event. Raised twelvefold over the Codex value because a `claude` process does real startup work — settings, MCP servers, plugin sync — before its first `system/init` line. Exceeding it raises `ResponseTimeout`. |
| `turn_timeout_ms` | `3600000` | `3600000` | Bounds **silence** after the first event, and resets on every line received. Exceeding it raises `TurnTimeout`. |
| `stall_timeout_ms` | `300000` | `300000` | SPEC 8.5 Part A orchestrator-side stall detection. **Currently inert for this backend** — see [gap 2](#known-gaps). |

---

## `codex:` reference

The `codex:` block is a spec field, not an extension, and is already documented:

| Where | What it gives you |
|---|---|
| **SPEC 5.3.6** | The normative field list: `command`, `approval_policy`, `thread_sandbox`, `turn_sandbox_policy`, `turn_timeout_ms`, `read_timeout_ms`, `stall_timeout_ms` |
| **SPEC 6.4** | Cheat-sheet defaults table |
| `workflow/config.py` | Every SPEC 6.4 default as a named constant (`DEFAULT_CODEX_COMMAND`, `DEFAULT_APPROVAL_POLICY`, …) so the table can be diffed against code without running anything |
| [`docs/SECURITY.md`](SECURITY.md) | This implementation's chosen values for the three fields SPEC 5.3.6 leaves implementation-defined, and why |
| [`docs/CONFORMANCE.md`](CONFORMANCE.md) | Requirement → module → test traceability |

Three values SPEC 5.3.6 declines to enumerate are passed through to Codex verbatim rather
than checked against a hand-maintained enum: `approval_policy` (default `never`),
`thread_sandbox` and `turn_sandbox_policy` (both default `danger-full-access`). To see
what your Codex accepts, run `codex app-server generate-json-schema --out <dir>` and read
the definitions referenced by `v2/ThreadStartParams.json` and `v2/TurnStartParams.json`.

---

## What each backend can do

### Only the Claude backend

| Capability | How it reaches you |
|---|---|
| **USD cost** | `turn_completed` payload `cost: {turn_usd, session_usd}`. `total_cost_usd` appears on the `result` event only, so a run killed mid-turn reports no cost for that attempt. |
| **Cache-token metrics** | `turn_completed` payload `cache: {read_input_tokens, creation_input_tokens}`, accumulated across turns. Cache reads are counted as input in the SPEC 13.5 totals, because they are input the model processed. |
| **Rate-limit events** | `rate_limit_event` is a top-level stream type carrying `status`, `rateLimitType` (e.g. `five_hour`), `resetsAt` (Unix **seconds**), and overage state. It is emitted as a `notification`, repeated on `turn_completed`, and picked up by `events.extract_rate_limits` into `OrchestratorState.codex_rate_limits`. |
| **Per-pattern tool control** | `allowed_tools` / `disallowed_tools` accept `Bash(git *)`, `Read(src/**)`, `mcp__server__*`. The Codex path has approval policies, not patterns. |
| **Hard budget cap** | `max_budget_usd`, enforced by the CLI. |
| **Model and effort selection** | `model`, `effort`. |
| **Deterministic session ids** | Derived from the issue identifier, so the id survives an orchestrator restart. |
| **No `bash` dependency** | Executed directly. The Codex path is required by SPEC 10.1 to use `bash -lc`, which on Windows means Git Bash on `PATH`. |
| **A verified wire contract** | [`docs/claude-protocol.md`](claude-protocol.md). |

### Only the Codex backend

| Capability | Why Claude lacks it |
|---|---|
| **Tracker tools advertised to the agent** | The Codex client sends `agent_tool_specs()` in the thread-creation params and executes calls host-side, so the agent can invoke `linear_set_issue_state`, `github_add_issue_comment`, and friends without ever seeing a token. The Claude driver accepts `tool_specs`/`tool_executor` and never uses them ([gap 1](#known-gaps)). Note that the field name carrying them is one of the unverified protocol strings. |
| **Symphony's approval state machine** | `agent/approvals.py` models four outcomes including `FAIL_RUN`, and `ApprovalDecision.ends_run` lets a denial actually end a run (SPEC 10.5). The Claude factory discards `approval_decider`; posture is entirely `--permission-mode`. |
| **Sandbox configuration** | `thread_sandbox` / `turn_sandbox_policy` pass through to Codex. `permission_mode` is a permission gate, not a sandbox — it constrains what the agent asks to do, not what the OS lets the process do. |
| **Remote execution** | `worker.ssh_hosts` builds an `AppServerClient` over SSH ([gap 5](#known-gaps)). |
| **Session titles** | SPEC 10.2 asks for `<issue.identifier>: <issue.title>` where the protocol supports it. The CLI does (`-n/--name`); the driver does not pass it ([gap 6](#known-gaps)). |
| **Test coverage** | `tests/test_app_server.py` exists. Nothing under `tests/` mentions Claude ([gap 4](#known-gaps)). |

### Equivalent in both

Absolute token totals in the SPEC 13.5 shape with delta tracking upstream; the SPEC 10.4
event vocabulary (both emit only canonical names, so `AgentEvent.is_known` holds);
workspace-cwd enforcement; SPEC 15.3 credential stripping; 10 MB max line size;
protocol-stream and stderr kept strictly separate; `turn_timeout_ms` / `read_timeout_ms`.

---

## Structural differences

### One process per turn, not one long-lived thread

`claude --print` runs a turn and exits, so there is no live stdin to write a second turn
into. Continuation turns start a new process with `--resume <session-id>`.

SPEC 10.3 says the subprocess *SHOULD* remain alive across continuation turns. That is a
`SHOULD` about not resending the task prompt and not losing thread state, and resuming a
persisted session satisfies both. Verified end to end: two separate `claude` processes
sharing one session id, where the second recalled a codeword given only to the first.

Consequences worth knowing:

- `start_session()` starts **no process**. The first process starts with the first turn,
  so a workspace or prompt failure costs nothing.
- `ClaudeCodeClient.pid` is `None` between turns, and the `codex_app_server_pid` field on
  emitted events changes from turn to turn.
- Killing a run mid-turn loses that turn's cost figure entirely.
- Exit code `143` is SIGTERM and is mapped to `TurnCancelled`, not `PortExit`.
- The transcript on disk outlives the workspace — it lives under Claude's own project
  directory keyed by cwd, not inside the workspace.

### No login shell

SPEC 15.3 requires that declared tracker credentials are not inherited by the agent. Both
backends strip them from the child's environment copy. The difference is what happens
next:

- **Codex** must be launched as `bash -lc <codex.command>` (SPEC 10.1). `-l` sources the
  login profile *after* the strip, and exporting a tracker token from `~/.bash_profile` is
  an ordinary way for one to be set. A real child was observed receiving the credential
  the strip had removed. The fix, described in [`docs/SECURITY.md`](SECURITY.md) §12.2, is
  to prefix the command with `unset -v <NAME>` for each declared secret, with the
  environment strip retained as defense in depth. Two mechanisms, because one was
  provably insufficient.
- **Claude** is executed directly, with no shell between Symphony and the agent. There is
  no profile-sourcing step to undo the strip, so the environment this process builds is
  the environment the child gets. One mechanism suffices.

This is not a claim that the Codex path is unsafe today — it is fixed and verified by
reproduction. It is a claim that the Claude path has one fewer thing that can go wrong.

---

## Verification status

### Verified

**The Claude wire contract**, by running `claude 2.1.214` on Windows and capturing stdout.
Every event shape in [`docs/claude-protocol.md`](claude-protocol.md) was observed, not
inferred. Re-verify after an upgrade with the command that document specifies:

```bash
claude -p "Reply with exactly: OK" --output-format stream-json --verbose \
  --permission-mode bypassPermissions --model haiku
```

Three additional checks are cheap and cost no API call, because the CLI rejects unknown
options before doing any work. Append a flag that is definitely unknown and read which
one it complains about:

```bash
# Does the driver still emit only flags this build accepts?
claude --max-turns 3 --effort high --definitely-not-a-flag -p x
#   -> "unknown option '--definitely-not-a-flag'"  means everything before it parsed

# Has the permission-mode choice list changed?
claude --permission-mode bogus --definitely-not-a-flag -p x
#   -> lists the accepted modes; compare against claude.py::PERMISSION_MODES
```

Also verified for this backend, against the code as it stands: the deterministic session
uuid reaches the CLI and Claude Code persists a transcript under it (a workspace for issue
`ENG-77` produced exactly the uuid `deterministic_session_uuid("ENG-77")` returns);
`--resume` preserves conversational memory across processes; every emitted event name is
in `AGENT_EVENT_NAMES`; rate limits reach `AgentEvent.rate_limits()`; token totals reach
`AgentEvent.token_totals()` in the SPEC 13.5 absolute shape.

### Not verified

- **The entire Codex protocol vocabulary.** `app_server.py::ProtocolNames` was written
  against the documented shape of the app-server protocol and was never confirmed against
  a running `codex` binary. Exactly one string in it appears verbatim in SPEC.md
  (`thread/tokenUsage/updated`, SPEC 13.5); the rest follow its `namespace/camelCase`
  convention and are best-effort. The **framing** assumption — newline-delimited JSON-RPC
  2.0 rather than `Content-Length` headers — lives outside that dataclass, in
  `_read_stdout` and `_write`. To correct: run `codex app-server generate-json-schema`,
  diff the names, and edit only that dataclass.
- **`--max-turns` behavior.** Accepted by the CLI parser at 2.1.214, absent from
  `claude --help`, effect unobserved.
- **Permission prompts under `permission_mode: manual`.** Every probe ran with
  `bypassPermissions` or `dontAsk`. How a prompt surfaces in `stream-json`, and whether a
  programmatic caller can answer one, is unobserved.
- **The `permission_denials` element shape.** The array was observed present but always
  empty; the driver reports only its length.
- **Any Symphony-side behavior of the Claude backend under test.** There are no tests.

---

## Known gaps

Confirmed against the code as it stands. Each is a real limitation, not a caveat.

1. **Provider-native tracker tools are not advertised to Claude Code.** This is the one
   place the Codex backend can do something the Claude backend cannot.
   `ClaudeCodeClient.__init__` stores `tool_specs` and `tool_executor` and never reads
   them, so an agent under `agent.kind: claude` cannot call `linear_set_issue_state`,
   `linear_add_comment`, `github_set_project_status`, or any other host-side tracker
   tool. SPEC 10.2 asks that implemented client-side tools be advertised.

   It matters beyond convenience. SPEC 11.5 routes ticket mutations through host-side
   adapter tools precisely so the coding agent never holds a tracker credential. A run
   still works — the agent edits the workspace and the orchestrator reconciles tracker
   state from the outside — but an agent that must update a ticket itself needs either
   an MCP server via `claude.mcp_config` or its own credential, and the second gives up
   the SPEC 15.3 isolation the Codex path preserves.

   Closing this properly means exposing the adapter's tools as an MCP server the CLI can
   load. That is real work, not a small patch, and it is not done.

2. **Driver test coverage is thin.** Nothing under `tests/` exercises `agent/claude.py`. The Codex
   backend has `tests/test_app_server.py`. Read this as: the Claude *protocol* is the
   better-verified of the two, and the Claude *driver* is the less-verified.

3. **The SSH worker is Codex-only.** `ssh/worker.py` constructs an `AppServerClient`
   directly and builds `cd -- <workspace> && exec bash -lc <codex.command>`. It does not
   branch on `agent.kind`, so `worker.ssh_hosts` with `agent.kind: claude` runs Codex
   remotely regardless of the setting.

4. **No session title is passed.** `run_turn(..., title=...)` is forwarded into the
   `session_started` event payload but never onto the command line, though the CLI accepts
   `-n/--name`. SPEC 10.2 asks for issue-identifying metadata where the protocol supports
   titles.

5. **Symphony's approval machinery is bypassed.** The Claude factory discards
   `approval_decider`, so `agent/approvals.py` — including the `FAIL_RUN` verdict that
   SPEC 10.5 depends on to stop a run that keeps asking — never participates. Permission
   posture is whatever `--permission-mode` enforces, and by default that is
   `bypassPermissions`.
