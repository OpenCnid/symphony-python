# Symphony-Python — Build Contracts

This file is the shared interface agreement for the implementation. Every module
is written against it. If a signature here disagrees with your module's local
convenience, **this file wins** — a divergent interface breaks siblings that
cannot see your work.

Target: **Python 3.11+**, async-first (`asyncio`), no framework lock-in.
Spec: `SPEC.md` at the repository root (verbatim copy of `openai/symphony@main`).

---

## 1. Ownership map

Each path has exactly one owner. Write only inside your own paths.

| Path | Spec sections |
|---|---|
| `src/symphony/errors.py` | 5.5, 10.6, 11.4 — **already written, do not modify** |
| `src/symphony/models.py` | 4.1, 4.2 — **already written, do not modify** |
| `src/symphony/trackers/base.py` | 11.1–11.4 — **already written, do not modify** |
| `src/symphony/workflow/loader.py` | 5.1, 5.2 |
| `src/symphony/workflow/config.py` | 5.3, 6.1, 6.3, 6.4 |
| `src/symphony/workflow/watcher.py` | 6.2 |
| `src/symphony/workflow/template.py` | 5.4, 12 |
| `src/symphony/orchestrator/core.py` | 7, 8.1, 16.1–16.4, 16.6 |
| `src/symphony/orchestrator/scheduling.py` | 8.2, 8.3 |
| `src/symphony/orchestrator/retry.py` | 8.4 |
| `src/symphony/orchestrator/reconcile.py` | 8.5, 8.6, 16.3 |
| `src/symphony/workspace/manager.py` | 9.1, 9.2, 9.3 |
| `src/symphony/workspace/safety.py` | 9.5, 15.2 |
| `src/symphony/workspace/hooks.py` | 9.4, 15.4 |
| `src/symphony/agent/app_server.py` | 10.1–10.3, 10.6 |
| `src/symphony/agent/events.py` | 10.4, 13.5 |
| `src/symphony/agent/approvals.py` | 10.5 |
| `src/symphony/agent/runner.py` | 10.7, 16.5 |
| `src/symphony/trackers/memory.py` | 11 (in-process adapter for tests) |
| `src/symphony/trackers/github.py` | 11 (GitHub Projects v2) |
| `src/symphony/trackers/linear.py` | 11 (Linear) |
| `src/symphony/observability/logging.py` | 13.1, 13.2 |
| `src/symphony/observability/snapshot.py` | 13.3, 13.5 |
| `src/symphony/observability/humanize.py` | 13.6 |
| `src/symphony/observability/status.py` | 13.4 |
| `src/symphony/http/server.py`, `api.py`, `dashboard.py` | 13.7 |
| `src/symphony/cli.py`, `__main__.py` | 17.7 |
| `src/symphony/rlm/` | RLM addressability surface (this implementation's extension) |
| `src/symphony/ssh/worker.py` | Appendix A |

Tests live in `tests/test_<module>.py` and are owned by whoever owns the module.

---

## 2. Already-written contract surface

Import these; do not redefine them.

### `symphony.models`

```python
Issue(id, identifier, title, state, dispatchable, native_ref=None,
      description=None, priority=None, branch_name=None, url=None,
      assignee_id=None, labels=(), blocked_by=(), created_at=None, updated_at=None)
Issue.normalized_state -> str
Issue.label_set -> frozenset[str]
Issue.has_labels(required) -> bool
Issue.to_template_context() -> dict[str, Any]

BlockerRef(id=None, identifier=None, state=None)
WorkflowDefinition(config: dict, prompt_template: str, source_path: str | None)
Workspace(path: str, workspace_key: str, created_now: bool)
RunPhase  # 7.2 enum; .is_terminal, .is_success
RunAttempt(issue_id, issue_identifier, attempt, workspace_path, started_at, status, error)
ClaimState  # 7.1 enum
LiveSession(...)          # 4.1.6, all token counters default 0
RetryEntry(issue_id, identifier, attempt, due_at_ms, timer_handle, error)
CodexTotals(input_tokens, output_tokens, total_tokens, seconds_running)
RunningEntry(issue, identifier, started_at, worker_handle, session, retry_attempt,
             workspace_path, phase, recent_events, last_error)
OrchestratorState(poll_interval_ms, max_concurrent_agents, running, claimed,
                  retry_attempts, completed, codex_totals, codex_rate_limits)
OrchestratorState.claim_state(issue_id) -> ClaimState
OrchestratorState.running_count() -> int
OrchestratorState.running_count_for_state(state) -> int

normalize_state(s) -> str      # trim + lowercase
normalize_label(s) -> str      # trim + lowercase
workspace_key(identifier) -> str   # 4.2 sanitize + 64-bit hash on change
session_id(thread_id, turn_id) -> str
```

### `symphony.errors`

Every error subclasses `SymphonyError` and carries `.category` (the spec slug),
`.message`, `.details`, and `.to_dict()`. Raise the specific class; never a bare
`Exception`. Full list in `errors.__all__`.

### `symphony.trackers.base`

```python
class TrackerAdapter(ABC):
    kind: ClassVar[str]
    default_active_states: ClassVar[tuple[str, ...]]
    default_terminal_states: ClassVar[tuple[str, ...]]
    def __init__(self, provider: dict[str, Any], **kwargs) -> None
    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]
    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]
    def agent_tool_specs(self) -> list[ToolSpec]
    def secret_environment_names(self) -> list[str]
    async def execute_agent_tool(self, name, arguments, context: ToolContext) -> ToolResult
    async def aclose(self) -> None

@register_adapter            # class decorator
build_adapter(kind, provider, **kwargs) -> TrackerAdapter
adapter_kinds() -> list[str]
ToolSpec(name, description, input_schema, mutates_tracker)
ToolResult(ok, content, error); .success(c) / .failure(e)
ToolContext(issue)
parse_rfc3339(v), normalize_labels(v), coerce_priority(v), coerce_blockers(v), require_str(...)
```

---

## 3. Cross-module signatures to build against

These modules do not exist yet. Write to *these* signatures so siblings compose.

### `symphony.workflow.loader`

```python
def load_workflow(path: str | os.PathLike[str]) -> WorkflowDefinition
def resolve_workflow_path(explicit: str | None = None) -> Path   # 5.1 precedence
```

### `symphony.workflow.config`

```python
@dataclass(frozen=True, slots=True)
class ServiceConfig:
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
    max_concurrent_agents_by_state: dict[str, int]   # keys normalized
    codex: CodexConfig
    server_port: int | None
    ssh_hosts: tuple[str, ...]
    max_concurrent_agents_per_host: int | None
    raw: dict[str, Any]

    def is_active(self, state: str) -> bool
    def is_terminal(self, state: str) -> bool
    def slot_limit_for_state(self, state: str) -> int

@dataclass(frozen=True, slots=True)
class HookConfig:
    after_create: str | None
    before_run: str | None
    after_run: str | None
    before_remove: str | None
    timeout_ms: int

@dataclass(frozen=True, slots=True)
class CodexConfig:
    command: str
    approval_policy: str
    thread_sandbox: str
    turn_sandbox_policy: str
    turn_timeout_ms: int
    read_timeout_ms: int
    stall_timeout_ms: int

def build_config(defn: WorkflowDefinition) -> ServiceConfig
def validate_dispatch_config(cfg: ServiceConfig) -> None   # 6.3; raises ConfigValidationError
def expand_value(value: str, *, base_dir: Path | None = None) -> str   # ~ and $VAR
```

### `symphony.workflow.template`

```python
def render_prompt(template: str, issue: Issue, attempt: int | None) -> str
def render_continuation_prompt(issue: Issue, turn_number: int, max_turns: int) -> str
```
Strict mode: unknown variable **or** unknown filter raises `TemplateRenderError`;
a malformed template raises `TemplateParseError`. Empty body -> the SPEC 5.4
fallback prompt.

### `symphony.workflow.watcher`

```python
class WorkflowWatcher:
    def __init__(self, path: Path, on_change: Callable[[], Awaitable[None]]) -> None
    async def start(self) -> None
    async def stop(self) -> None
```

### `symphony.workspace.safety`

```python
def assert_within_root(path: Path, root: Path) -> Path      # 9.5 Invariant 2
def assert_launch_cwd(cwd: Path, workspace_path: Path) -> None   # 9.5 Invariant 1
```

### `symphony.workspace.manager`

```python
class WorkspaceManager:
    def __init__(self, root: Path, hooks: HookRunner) -> None
    async def create_for_issue(self, identifier: str) -> Workspace
    async def cleanup(self, identifier: str) -> bool
    def path_for(self, identifier: str) -> Path
```

### `symphony.workspace.hooks`

```python
class HookRunner:
    def __init__(self, cfg: HookConfig) -> None
    async def run(self, name: str, cwd: Path, *, fatal: bool) -> None
```
`fatal=True` raises `HookError`/`HookTimeout`; `fatal=False` logs and returns.

### `symphony.agent.events`

```python
@dataclass(frozen=True, slots=True)
class AgentEvent:
    event: str
    timestamp: datetime
    codex_app_server_pid: str | None = None
    usage: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)

def extract_token_totals(payload: dict) -> tuple[int, int, int] | None   # 13.5 absolute only
def extract_rate_limits(payload: dict) -> dict | None
```
Event names are the SPEC 10.4 strings verbatim.

### `symphony.agent.app_server`

```python
class AppServerSession:
    thread_id: str
    async def start_turn(self, prompt: str, *, title: str | None) -> None
    async def stop(self) -> None

class AppServerClient:
    def __init__(self, cfg: CodexConfig, *, workspace: Path,
                 tool_specs: list[ToolSpec], tool_executor, on_event) -> None
    async def start_session(self) -> AppServerSession
    async def run_turn(self, session, prompt, *, title=None) -> None
```
`on_event: Callable[[AgentEvent], None]`. Launch is `bash -lc <codex.command>` with
`cwd=workspace` (SPEC 10.1). Max line size 10 MB.

### `symphony.agent.runner`

```python
class AgentRunner:
    async def run_attempt(self, issue: Issue, attempt: int | None) -> None
```
Implements SPEC 16.5 exactly, including the in-worker continuation turn loop.

### `symphony.orchestrator.scheduling`

```python
def sort_for_dispatch(issues: Iterable[Issue]) -> list[Issue]           # 8.2
def issue_routable(issue: Issue, cfg: ServiceConfig) -> bool            # 8.2
def should_dispatch(issue, state, cfg) -> bool                          # 8.2
def available_slots(state_, cfg) -> int                                 # 8.3
def has_state_slot(issue, state_, cfg) -> bool                          # 8.3
```

### `symphony.orchestrator.retry`

```python
CONTINUATION_DELAY_MS = 1000
def backoff_delay_ms(attempt: int, max_backoff_ms: int) -> int          # 8.4
```

### `symphony.observability.logging`

```python
def get_logger(name: str) -> StructuredLogger
class StructuredLogger:
    def info(self, msg: str, **fields) -> None      # renders key=value (13.1)
    def warning(...); def error(...); def debug(...)
    def bind(self, **fields) -> StructuredLogger    # issue_id / issue_identifier / session_id
```

### `symphony.observability.snapshot`

```python
def build_snapshot(state: OrchestratorState) -> dict[str, Any]   # 13.3 / 13.7.2 shape
def build_issue_detail(state, identifier) -> dict[str, Any] | None
```

---

## 4. House rules

1. **Async-first.** Public I/O methods are `async def`. Blocking filesystem work
   goes through `asyncio.to_thread`.
2. **No secrets in logs, errors, or `native_ref`** (SPEC 15.3). Validate presence
   without printing the value.
3. **Type hints on everything public.** `from __future__ import annotations` at
   the top of every module.
4. **Docstrings cite spec sections** as `SPEC 8.4` so conformance stays auditable.
5. **Comments explain *why*, not *what*.** Prefer none over restating the code.
6. **Tests are `pytest`**, async via `asyncio_mode = "auto"`. No network in the
   default suite; mark real-integration tests `@pytest.mark.integration`.
7. **Windows-aware.** Paths via `pathlib`. Where the spec says `bash -lc`, honor
   it and document the Windows fallback rather than silently changing behavior.
8. **Determinism.** No wall-clock sleeps in tests; inject clocks and timers.
9. **`ruff` clean** at line-length 100.

## 5. Documented policy positions (SPEC requires these be stated)

This implementation targets **trusted environments** and documents:

- **Approval policy (10.5):** auto-approve command-execution and file-change
  approvals for the session; treat user-input-required turns as hard failure.
- **Sandbox (10.5, 15.1):** pass-through to Codex; defaults set in `CodexConfig`.
- **Non-directory at a workspace path (17.2):** fail the attempt; never unlink.
- **Reused-workspace population failure (9.3):** surface the error; never
  destructively reset.

---

## 6. As-built deltas

Section 3 was written before the modules existed, so it names signatures the
authors were told to build against. Where the landed code extends that contract,
the addition is recorded here. **No signature in section 3 was changed** — every
delta below is additive, so code written against section 3 still composes.

### `workflow.watcher`

Section 3 lists only `__init__`, `start`, `stop`. SPEC 6.2 also requires
defensive re-validation in case filesystem events are missed, and SPEC 6.3
requires per-tick validation, so the consumer needs a pull path:

```python
async WorkflowWatcher.prime() -> ReloadOutcome     # initial last-known-good, no notify
async WorkflowWatcher.reload(*, force=False) -> ReloadOutcome
async WorkflowWatcher.is_stale() -> bool           # digest compare, no event stream
WorkflowWatcher.current() -> EffectiveWorkflow | None
WorkflowWatcher.generation / .healthy / .last_error
```

An invalid reload never displaces `current()`.

### `agent.app_server`

```python
AppServerClient(cfg, *, workspace, tool_specs, tool_executor, on_event,
                approval_decider=None, secret_env_names=(), protocol=PROTOCOL,
                spawn=None, now=None)
```

`secret_env_names` and `approval_decider` are the two the runner must supply —
see section 5 below. `protocol` isolates every version-specific Codex string in
one `ProtocolNames` dataclass; correcting it against a real
`codex app-server generate-json-schema` is a one-block edit.

### `agent.runner`

The factory it calls gained the two parameters above:

```python
AppServerFactory(cfg, *, workspace, tool_specs, tool_executor, on_event,
                 secret_env_names=(), approval_decider=None)
```

`TrackerLike` correspondingly requires `secret_environment_names()`.

### `orchestrator.core`

```python
Orchestrator(*, config, tracker, runner, workspaces, deps=None,
             reconciler=_UNSET, validate=None, clock=None, logger=None)
async Orchestrator.tick(*, reschedule=False) / .start() / .stop() / .run_forever()
async Orchestrator.invoke(fn) / .drain() / .apply_config(config)
```

`reconciler` omitted delegates to `module_reconciler`, which calls
`orchestrator.reconcile`. An explicit `None` selects the built-in fallback.
All state mutation is serialized through one mailbox task.

### `orchestrator.reconcile`

```python
async reconcile_running_issues(state, *, cfg, tracker, deps) -> OrchestratorState
async startup_terminal_workspace_cleanup(*, cfg, tracker, workspaces, logger=None)
plan_reconciliation(running_ids, refreshed, cfg, *, routable=None)  # pure
ReconcileDeps(terminate_running_issue, schedule_retry, now=..., routable=None, logger=None)
```

The module *decides*; the orchestrator *mutates* through the injected callbacks.
That split is what lets SPEC 8.5 live here without breaking SPEC 7's
sole-mutator invariant.

### `http`

Enablement is `build_http_server(source, *, cli_port=None, config_port=None, ...)`,
returning `None` when neither port is set — the normal not-enabled result, not an
error. `resolve_port` tests `is not None` rather than truthiness, so `--port 0`
overrides `server.port: 9000` and still requests an ephemeral port.

### `rlm`

Not in section 3 at all; this implementation's own extension. Entry points are
`open_repl()`, `system_map()`, `describe_component()`, `find_symbol()`, and
`recursive_query()`. Every introspection result carries `_meta.chars` equal to
`len(json.dumps(result))`, so a caller can price a descent before taking it.
Note that symbol records key on `symbol`, not `name`.

### Cross-module composition owned by no single module

Three requirements sit between modules and were wired during integration rather
than by any one author:

1. **Adapter registration** — `trackers/__init__` imports `github`, `linear`, and
   `memory` for their `@register_adapter` side effect. Without it
   `build_adapter` raises `UnsupportedTrackerKind` for every bundled adapter.
2. **Credential isolation (SPEC 15.3)** — `runner` reads
   `tracker.secret_environment_names()` and passes it to the launcher as
   `secret_env_names`. The adapter cannot strip what it does not launch; the
   client cannot know which names matter without being told.
3. **Approval policy (SPEC 10.5)** — `runner` passes
   `_canonical_approval_decider`, which bridges `agent.approvals` to the
   app-server's flat `(approved, reason)` verdict. Without it the client uses a
   local copy of the posture and a policy swap silently has no effect.
