---
tracker:
  kind: memory
  provider:
    seed: []
  required_labels: []
  active_states: ["Todo", "In Progress"]
  terminal_states: ["Done", "Canceled"]

polling:
  interval_ms: 30000

workspace:
  root: ./symphony_workspaces

hooks:
  timeout_ms: 60000
  after_create: |
    echo "workspace created: $PWD"
  before_run: |
    echo "starting attempt in $PWD"

agent:
  max_concurrent_agents: 4
  max_turns: 20
  max_retry_backoff_ms: 300000
  max_concurrent_agents_by_state:
    "in progress": 2

codex:
  command: codex app-server
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000

server:
  port: 8787
---

You are working on `{{ issue.identifier }}`: {{ issue.title }}.

## Context

- State: `{{ issue.state }}`
- Priority: {% if issue.priority %}P{{ issue.priority }}{% else %}unset{% endif %}
{% if issue.url %}- Link: {{ issue.url }}{% endif %}
{% if issue.branch_name %}- Suggested branch: `{{ issue.branch_name }}`{% endif %}
{% if issue.labels.size > 0 %}- Labels: {% for label in issue.labels %}`{{ label }}`{% unless forloop.last %}, {% endunless %}{% endfor %}{% endif %}
{% if attempt %}- This is continuation/retry attempt {{ attempt }}.{% endif %}

## Description

{{ issue.description | default: "(no description provided)" }}

{% if issue.blocked_by.size > 0 %}
## Blocked by

{% for blocker in issue.blocked_by %}- {{ blocker.identifier }} ({{ blocker.state }})
{% endfor %}
{% endif %}

## What to do

1. Understand the issue and the surrounding code before changing anything.
2. Implement the change in this workspace.
3. Run the project's tests and linters; fix what you break.
4. Open a pull request and move the ticket to its next handoff state.
5. Leave the ticket with proof of work: what changed, what you verified, what
   you deliberately left alone.

Stop and hand off rather than guessing when the requirements are ambiguous.
