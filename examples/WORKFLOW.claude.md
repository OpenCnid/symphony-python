---
# Symphony driving Claude Code.
#
# Copy this to WORKFLOW.md at your repository root and edit the tracker block.
# The only line that selects the backend is `agent.kind`; everything under
# `claude:` is that backend's own settings. A workflow may carry both a
# `claude:` and a `codex:` block and switch between them with that one line.

tracker:
  kind: linear
  provider:
    api_key: $LINEAR_API_KEY
    team_key: ENG
  required_labels: ["agent"]
  active_states: ["Todo", "In Progress"]
  terminal_states: ["Done", "Canceled"]

polling:
  interval_ms: 30000

workspace:
  root: ~/symphony_workspaces

hooks:
  timeout_ms: 120000
  # Runs once, when the workspace directory is first created.
  after_create: |
    git clone --depth 1 "$REPO_URL" . 2>/dev/null || true
  # Runs before every attempt, including retries and continuations.
  before_run: |
    git fetch --all --prune 2>/dev/null || true

agent:
  kind: claude
  max_concurrent_agents: 3
  # Symphony's own turn cap: how many times the worker re-checks the tracker
  # and continues on the same session before exiting (SPEC 7.1). Distinct from
  # claude.max_turns below.
  max_turns: 12
  max_retry_backoff_ms: 300000
  max_concurrent_agents_by_state:
    "in progress": 2

claude:
  # Executable. Resolved through PATH; an absolute path also works.
  command: claude

  # Model alias or full id: opus, sonnet, haiku, fable, or claude-fable-5.
  model: sonnet

  # acceptEdits | auto | bypassPermissions | dontAsk | plan | manual
  #
  # `manual` is accepted but NOT recommended: how an interactive permission
  # prompt surfaces in stream-json is unverified, and SPEC 10.5 forbids a run
  # that stalls waiting for one. See docs/SECURITY.md.
  permission_mode: bypassPermissions

  # Fine-grained tool policy. Supports Claude Code's pattern syntax, so this is
  # the practical lever for SPEC 15.5 harness hardening — narrow the agent to
  # what the workflow actually needs rather than running wide open.
  allowed_tools:
    - "Read"
    - "Edit"
    - "Write"
    - "Glob"
    - "Grep"
    - "Bash(git *)"
    - "Bash(npm run *)"
    - "Bash(pytest *)"
  disallowed_tools:
    - "Bash(rm -rf *)"
    - "Bash(curl *)"
    - "WebFetch"

  # Hard ceiling on Claude's own agentic turns inside a single invocation.
  max_turns: 40

  # Hard spend ceiling per invocation, in USD. Claude Code stops if exceeded.
  # No equivalent exists on the Codex backend.
  max_budget_usd: 5.00

  # Appended to Claude Code's default system prompt; the repository's own
  # CLAUDE.md still applies on top of this.
  append_system_prompt: |
    You are running unattended inside a Symphony workspace. Prefer small,
    verifiable changes. Run the project's tests before you finish, and when a
    requirement is ambiguous, leave the ticket in its handoff state with a note
    rather than guessing.

  # low | medium | high | xhigh | max
  effort: high

  # Session ids are derived from the issue identifier (uuid5), so a given issue
  # always resumes the same conversation -- including across an orchestrator
  # restart, which the Codex backend's in-memory thread cannot do.
  deterministic_session_id: true
  session_persistence: true
  fork_session: false

  # SPEC 10.6 timeouts. `read_timeout_ms` bounds startup; `turn_timeout_ms`
  # bounds silence once output has begun and resets on every event;
  # `stall_timeout_ms` is enforced by the orchestrator (<= 0 disables it).
  read_timeout_ms: 120000
  turn_timeout_ms: 3600000
  stall_timeout_ms: 600000

server:
  port: 8787
---

You are working on `{{ issue.identifier }}`: {{ issue.title }}.

## Context

- State: `{{ issue.state }}`
- Priority: {% if issue.priority %}P{{ issue.priority }}{% else %}unset{% endif %}
{% if issue.url %}- Ticket: {{ issue.url }}{% endif %}
{% if issue.branch_name %}- Suggested branch: `{{ issue.branch_name }}`{% endif %}
{% if issue.labels.size > 0 %}- Labels: {% for label in issue.labels %}`{{ label }}`{% unless forloop.last %}, {% endunless %}{% endfor %}{% endif %}
{% if attempt %}
> This is continuation/retry attempt {{ attempt }}. Previous work in this
> workspace is still present — read it before starting over.
{% endif %}

## Description

{{ issue.description | default: "(no description provided)" }}

{% if issue.blocked_by.size > 0 %}
## Blocked by

{% for blocker in issue.blocked_by %}- {{ blocker.identifier }} ({{ blocker.state }})
{% endfor %}
{% endif %}

## What to do

1. Read the surrounding code before changing anything.
2. Implement the change in this workspace.
3. Run the project's tests and linters; fix what you break.
4. Open a pull request and move the ticket to its next handoff state.
5. Leave proof of work on the ticket: what changed, what you verified, and what
   you deliberately left alone.

Stop and hand off rather than guessing when the requirements are ambiguous.
