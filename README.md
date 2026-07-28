# Symphony (Python)

A conforming implementation of the [Symphony service specification](https://github.com/openai/symphony/blob/main/SPEC.md),
built via **Option 1** of the upstream README — "tell your favorite coding agent
to build Symphony in a programming language of your choice."

Symphony turns project work into isolated, autonomous implementation runs. It
polls an issue tracker, creates a per-issue workspace, runs a coding-agent
session inside that workspace, and reconciles the run against tracker state —
so teams manage *work* instead of supervising agents.

The coding agent is pluggable. Two backends ship: **Codex** (`agent.kind: codex`,
the default and the specification's worked example) and **Claude Code**
(`agent.kind: claude`). They differ in capability and in how much of each has
been verified against a real binary — see
[`docs/agent-backends.md`](docs/agent-backends.md) before choosing.

> [!WARNING]
> Like upstream, this is an engineering preview intended for trusted
> environments. Read [`docs/SECURITY.md`](docs/SECURITY.md) before pointing it
> at anything you care about.

---

## Why Python

The upstream reference implementation is Elixir, and for good reason — the BEAM's
supervised-process model is a natural fit for per-issue workers. This
implementation optimizes for a different constraint: **being driven by a
Recursive Language Model.**

An RLM ([Zhang & Khattab, 2025](https://arxiv.org/abs/2510.04871)) does not read
its context as a flat prompt. It stores context as a *variable in a REPL* and
emits code to slice, chunk, and recurse over it, calling sub-models on the
pieces. In the canonical formulation that REPL is a Python REPL, and the root
model's action space is Python code.

That reframes "most performant." Symphony's hot path is I/O — tracker polling,
app-server stdio streaming, filesystem lifecycle — so a systems language would
win a throughput benchmark that the workload never runs. The cost center is the
agent subprocess, not the orchestrator loop. What actually needs optimizing is
**how cheaply a recursive model can decompose and act on the running system**:

| Property | Why it dominates for RLM use |
|---|---|
| REPL-native | The RLM's action space *is* Python. `import symphony` and recurse over live objects — no FFI, no serialization boundary, no compile step between thought and execution. |
| Introspectable | `dataclass`, `inspect`, and `__doc__` make every component self-describing at runtime, so a sub-model can query structure instead of being handed it. |
| Flat, named surfaces | Low tokens-per-semantic-unit. An RLM budgets context per recursion level; dense, addressable modules recurse cheaply. |
| `asyncio` | Thousands of concurrent I/O-bound sessions on one loop, which is the actual concurrency shape here. |
| Free-threaded 3.13+ | Removes the GIL ceiling for the parallel-fanout that RLM recursion generates. |

The honest trade: Rust or Go would beat this on raw throughput, and Elixir would
beat it on supervision ergonomics. Neither is the objective function when the
consumer is a model that thinks in Python.

The RLM addressability surface itself lives in
[`src/symphony/rlm/`](src/symphony/rlm/) and is documented in
[`docs/RLM.md`](docs/RLM.md).

---

## Install

```bash
python -m venv .venv && . .venv/Scripts/activate  # Windows
pip install -e ".[dev]"
```

## Run

```bash
symphony ./WORKFLOW.md
```

With no argument, Symphony uses `./WORKFLOW.md` from the current working
directory (SPEC 5.1). Add `--port 8787` to enable the optional HTTP dashboard
and JSON API (SPEC 13.7).

## Configure

Everything lives in a repository-owned `WORKFLOW.md`: YAML front matter for
runtime settings, Markdown body for the per-issue prompt template. Changes are
detected and re-applied without a restart (SPEC 6.2). See the example at the
repository root.

```yaml
---
tracker:
  kind: linear
  provider:
    api_key: $LINEAR_API_KEY
    team_key: ENG
  active_states: ["Todo", "In Progress"]
  terminal_states: ["Done", "Canceled"]
agent:
  kind: codex          # or: claude
  max_concurrent_agents: 4
  max_turns: 20
codex:
  command: codex app-server
---

You are working on {{ issue.identifier }}: {{ issue.title }}.
```

`agent.kind` selects the coding-agent backend, and each backend reads its own
front-matter block — `codex:` or `claude:`. Only the selected block is parsed, so
one file can carry both and switch with a single line. Full field reference in
[`docs/agent-backends.md`](docs/agent-backends.md).

## Documentation

| Document | Contents |
|---|---|
| [`SPEC.md`](SPEC.md) | Verbatim upstream specification (the authority) |
| [`CONTRACTS.md`](CONTRACTS.md) | Module ownership map and cross-module signatures |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layer-by-layer design |
| [`docs/CONFORMANCE.md`](docs/CONFORMANCE.md) | Spec requirement → module → test traceability |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Trust boundary and documented policy positions |
| [`docs/agent-backends.md`](docs/agent-backends.md) | Choosing and configuring the Codex and Claude Code backends |
| [`docs/RLM.md`](docs/RLM.md) | The Recursive Language Model surface |
| [`docs/adapters/`](docs/adapters/) | Per-adapter profiles required by SPEC 11.2 |

## Implementation-defined behavior

The spec requires implementations to document their chosen policies. This one:

- **Trust posture** — targets trusted environments (SPEC 15.1).
- **Approvals** — auto-approves command-execution and file-change approvals for
  the session; treats user-input-required turns as hard failure (SPEC 10.5).
- **Sandbox** — pass-through to the targeted Codex app-server version.
- **Non-directory at a workspace path** — fails the attempt rather than
  unlinking (SPEC 17.2).
- **Reused-workspace population failure** — surfaces the error without
  destructively resetting the workspace (SPEC 9.3).

## License

[Apache 2.0](LICENSE), matching upstream. `SPEC.md` is reproduced from
[openai/symphony](https://github.com/openai/symphony) under the same license;
see [`NOTICE`](NOTICE).
