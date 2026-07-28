# Security

Written for an operator deciding whether to point this at a real repository.

SPEC 15 imposes documentation duties rather than a single mandated posture:
[15.1](../SPEC.md) requires a stated trust boundary, [10.5](../SPEC.md) requires
a documented approval/sandbox/user-input posture, [15.3](../SPEC.md) requires
stated secret handling including which environment names are stripped from the
child, [15.4](../SPEC.md) covers hook trust, and [15.5](../SPEC.md) asks that
harness hardening be treated as part of the core safety model and documented.
This document discharges those duties — including the parts that are unflattering.

> [!WARNING]
> **Short version.** This implementation targets **trusted environments**. By
> default it runs a coding agent with **no Codex-side sandbox**
> (`danger-full-access`) and **no approval gate** (`approval_policy: never`),
> driven by prompts built from issue-tracker text you may not control. Workspace
> containment binds where the agent *starts*, not what it can *reach*. Do not
> point this at a repository, a host, or a tracker whose compromise you could not
> absorb.

**Verification basis.** Claims here were read against module source in the
working tree on 2026-07-28, with `pytest` reporting 1315 passed / 3 skipped. The
tree was still being modified while this was written, so a module may have
changed after the claim about it was recorded. §9 lists what could not be
verified. Per-requirement traceability is in [`CONFORMANCE.md`](CONFORMANCE.md).

---

## 1. Trust boundary (SPEC 15.1)

SPEC 15.1 asks implementations to state clearly whether they target trusted
environments, more restrictive environments, or both, and whether they rely on
auto-approval, operator approvals, stricter sandboxing, or a combination.

**This implementation targets trusted environments and relies on auto-approval.**
It ships no operator-approval channel and adds no sandbox of its own.

Everything in the left column is **trusted input** — it is treated as if an
authorized operator wrote it, and it can cause code execution on the host:

| Inside the boundary (trusted) | Why it is trusted |
|---|---|
| `WORKFLOW.md` front matter | Selects the tracker, the workspace root, and the Codex command line |
| `WORKFLOW.md` hook scripts | Arbitrary shell, executed as the service user (§6) |
| `WORKFLOW.md` prompt body | Becomes the agent's instructions verbatim |
| `codex.command` | Launched via `bash -lc`, un-sanitized by design (SPEC 6.1 forbids rewriting command strings) |
| Host environment variables | Source of tracker credentials, inherited by the orchestrator |
| The Codex binary on `PATH` | Executes with the service user's full privileges |
| The `ssh_hosts` in `worker.*` | Remote command execution targets |

Everything in the left column below is **outside** the boundary — it originates
elsewhere, reaches the agent, and is **not** validated as safe:

| Outside the boundary (untrusted) | Where it lands |
|---|---|
| Issue `title`, `description`, `labels`, `branch_name`, `url` | Rendered into the prompt (SPEC 12.1) and shown on the dashboard |
| Blocker metadata, assignee IDs, `native_ref` | Prompt/tool context |
| Repository contents in the workspace | Read and executed by the agent |
| Tool-call arguments proposed by the agent | Executed host-side against the tracker |

SPEC 15.5 states the rule this implementation follows: do **not** assume tracker
data, repository contents, prompt inputs, or tool arguments are trustworthy just
because they arrived through a normal workflow. Nothing here sanitizes them; the
containment strategy is the deployment's, not the orchestrator's.

**The service user is the real blast radius.** Every control below is scoped
inside one OS user account. Anything that account can read, write, or reach on
the network is inside the blast radius of a single successful prompt injection.

## 2. Approval, sandbox, and user-input posture (SPEC 10.5)

SPEC 10.5 requires each implementation to document its posture and to guarantee
that approvals and user-input requests never stall a run indefinitely. The
posture recorded in [`README.md`](../README.md) and [`CONTRACTS.md`](../CONTRACTS.md) §5
is implemented as follows.

### 2.1 What is actually configured

| Setting | Default | Where | Meaning |
|---|---|---|---|
| Command-execution approvals | **auto-approve for the session** | `agent/approvals.py::TRUSTED_AUTO_APPROVE` | The agent may run any command it proposes; after the first, the client stops round-tripping |
| File-change approvals | **auto-approve for the session** | same | The agent may write any file it proposes |
| User-input-required | **hard failure** | same | The turn fails, the attempt fails, the orchestrator retries with backoff |
| Unclassifiable approval requests | **deny** | same (`ApprovalKind.UNKNOWN`) | A request that cannot be classified is never assumed benign |
| Unsupported dynamic tool calls | **structured failure, session continues** | `agent/app_server.py` | SPEC 10.5's anti-stall rule |
| `codex.approval_policy` | `"never"` | `workflow/config.py::DEFAULT_APPROVAL_POLICY` | The Codex `AskForApproval` value meaning "do not ask" |
| `codex.thread_sandbox` | `"danger-full-access"` | `workflow/config.py::DEFAULT_THREAD_SANDBOX` | No Codex-side filesystem/network sandbox |
| `codex.turn_sandbox_policy` | `"danger-full-access"` | `workflow/config.py::DEFAULT_TURN_SANDBOX_POLICY` | Same, per turn |

The no-stall guarantee is structural rather than a matter of care: every
`ApprovalDecision` is immediately actionable, and there is deliberately **no**
`ESCALATE` member, because this implementation ships no operator channel and a
decision with nowhere to go is exactly the indefinite stall SPEC 10.5 forbids.
Covered by `test_agent_events.py::test_no_shipped_policy_can_stall_a_run`.

### 2.2 What that posture costs

Stated plainly, because the defaults are the permissive end of every axis SPEC
15.5 names:

1. **There is no sandbox.** `danger-full-access` is a deliberate pass-through, not
   an oversight: the trust model assumes the operator supplies isolation
   externally. If you do not add it, the agent has the service user's full
   filesystem and network access. It can read `~/.ssh`, `~/.aws`, `.env` files,
   other repositories on the host, and anything else that user can open.
2. **There is no approval gate.** Auto-approve *for the session* means one
   approved command establishes the posture for the remainder of the session; no
   later command in that session is reviewed. Nothing in this implementation
   inspects the command before approving it — `StaticApprovalPolicy` is static by
   intent, and its decision does not depend on the request contents.
3. **Workspace containment does not contain the agent.** SPEC 9.5's invariants
   guarantee the agent is *launched* in the per-issue workspace with a validated
   cwd. They say nothing about where it goes next. With `danger-full-access` a
   first command of `cd / && cat /etc/shadow` is neither blocked nor reviewed.
   SPEC 15.1 says this in the spec's own words: path validation is "not a
   substitute for whatever approval and sandbox policy an implementation chooses."
4. **Prompt injection is a direct path to command execution.** Issue text is
   untrusted, is rendered into the prompt, and the agent then acts with the
   privileges in (1) and no review from (2). Anyone who can file or edit a ticket
   in the configured scope — including an external reporter on a public tracker —
   can attempt to steer the agent. This is the single most important risk in this
   document.
5. **Failing user-input-required turns costs work, not safety.** The attempt fails
   and is retried with backoff, so an agent that genuinely needs a human decision
   burns retries rather than pausing. That is the documented trade.

### 2.3 Tightening it

Two levers exist and neither requires forking:

- **Swap the approval posture.** `agent/approvals.py` ships `DENY_ALL`, and the
  posture is a swappable object: `set_approval_policy(DENY_ALL)` at startup, a
  per-call `policy=` argument, or any object satisfying the `ApprovalPolicy`
  protocol (`name` plus a *total* `decide`). A deployment needing
  content-sensitive rules — allowlisted commands, path-scoped writes — supplies
  its own object rather than growing conditionals in the client. Verified by
  `test_agent_events.py::test_policy_is_swappable_without_touching_call_sites`,
  `::test_custom_policy_object_is_honored`. A custom policy MUST stay total and
  non-blocking or it reintroduces the SPEC 10.5 stall.
- **Set the Codex sandbox in `WORKFLOW.md`.** `codex.thread_sandbox` and
  `codex.turn_sandbox_policy` are pass-through values; SPEC 5.3.6 declines to
  enumerate them, so this implementation does not validate them against a
  hand-maintained enum. Consult
  `codex app-server generate-json-schema` for the values your Codex version
  accepts, and set them to something stricter than the shipped default.

Neither lever is a substitute for OS-level isolation (§7).

## 3. Filesystem safety (SPEC 15.2, 9.5)

SPEC 15.2 makes three things mandatory. All three are enforced, and the
enforcement is stricter than the letter of the spec:

| Requirement | Implementation | Test |
|---|---|---|
| Workspace path MUST stay under the configured root | `workspace/safety.py::assert_within_root` | `test_workspace.py::test_within_root_rejects_parent_traversal`, `::test_within_root_rejects_symlink_escape`, `::test_within_root_rejects_sibling_sharing_a_string_prefix` |
| Agent cwd MUST be the per-issue workspace | `workspace/safety.py::assert_launch_cwd`, called before subprocess launch | `test_app_server.py::test_child_process_really_runs_in_the_workspace`, `::test_relative_workspace_is_rejected_before_launch` |
| Workspace directory names MUST use sanitized identifiers | `models.py::workspace_key` | `test_workspace.py::test_path_for_neutralizes_separators_in_the_identifier`, `::test_path_for_distinguishes_identifiers_that_sanitize_alike` |

Implementation notes an auditor should check rather than assume:

- Both operands are made absolute **and symlink-resolved**, then compared as
  `os.path.normcase`'d path *components* — never as strings. A `startswith`
  comparison would accept `/base/root-evil` under `/base/root`; a lexical
  normalization that never resolves symlinks would accept a workspace symlinked
  anywhere on the filesystem.
- Containment is **strict**: the root itself is not "inside" the root. That closes
  the case where a degenerate workspace key resolves onto the root and makes
  cleanup delete every workspace.
- Removal never follows a symlink and never unlinks a non-directory
  (`workspace/manager.py::_removable_directory` uses `os.lstat`). A non-directory
  occupying a workspace path **fails the attempt**; nothing is unlinked to make
  room. This is the documented SPEC 17.2 policy choice.
- Remote paths get the same treatment. `ssh/worker.py` quotes every token for a
  POSIX shell and then **re-parses the finished command line with `shlex.split`**,
  requiring a round-trip match before anything is sent — because a remote path is
  a string on the wire, and quoting that "looks fine" is the classic remote
  command-injection foothold.

**What this does not do:** none of it constrains the agent process after launch
(§2.2 item 3). Filesystem safety here protects *Symphony's own* operations —
which directory it creates, which it removes — not the agent's.

SPEC 15.2's RECOMMENDED hardening for ports (dedicated OS user, restricted
workspace-root permissions, dedicated volume) is **not** implemented and cannot
be: it is deployment configuration. It is in the checklist in §8.

## 4. Secret handling (SPEC 15.3)

### 4.1 What is enforced

| SPEC 15.3 requirement | Implementation | Test |
|---|---|---|
| Support `$VAR` indirection in workflow config | `workflow/config.py::expand_value`; adapter token resolution | `test_config.py::test_expand_value_expands_bare_and_braced_variables`; `test_tracker_github.py::test_dollar_var_indirection_declares_the_referenced_name` |
| Do not log API tokens or secret env values | `observability/logging.py`, redaction **by key name** | `test_observability.py::test_secret_shaped_fields_are_redacted`, `::test_record_to_dict_is_json_safe_and_redacted` |
| Validate presence of secrets without printing them | `expand_value` raises naming the *variable*, never its value | `test_config.py::test_expand_value_error_does_not_leak_the_variable_value`; `test_tracker_github.py::test_empty_dollar_var_is_a_missing_secret_and_never_echoes_the_value` |
| Execute provider-native tools in the host process with the configured credential | `trackers/*::execute_agent_tool`; `agent/runner.py` passes only `ToolContext(issue)` | `test_app_server.py::test_advertised_tool_executes_host_side_and_returns_its_result`; `test_agent_runner.py::test_tool_execution_carries_the_current_issue_as_context` |
| Adapters MUST declare secret environment names so launchers can remove them | `TrackerAdapter.secret_environment_names()` | see §4.2 |
| Do not pass tracker credentials through the child environment | `agent/app_server.py::_child_env` pops each declared name from a copy of `os.environ` | `test_app_server.py::test_declared_secret_env_names_are_stripped_from_child`; `test_agent_runner.py::test_tracker_secret_env_names_reach_the_app_server_launcher` |

Redaction is deliberately **by key name**, not by value shape: a value-shaped
heuristic both misses real secrets and redacts innocent fields. Matched keys
render as `[redacted]`. Counter-shaped names are explicitly *not* treated as
secrets (`test_observability.py::test_counter_fields_are_not_mistaken_for_secrets`).

`native_ref` is constrained by SPEC 11.3 to non-secret JSON-safe values, and the
`memory` adapter drops entries it cannot retain safely
(`test_tracker_memory.py::test_native_ref_drops_secretish_and_non_json_safe_entries`,
`::test_native_ref_becomes_null_when_nothing_can_be_retained_safely`). The runtime
snapshot never echoes it at all
(`test_observability.py::test_snapshot_never_echoes_opaque_native_ref`).

### 4.2 Which environment names are stripped from the child

SPEC 15.3 requires this list be stated. The stripped set is **adapter-declared,
not a fixed list**, and it is exactly what the selected adapter returns from
`secret_environment_names()`:

| Adapter | Names removed from the coding-agent child environment |
|---|---|
| `linear` | `LINEAR_API_KEY` **always**, plus the configured `api_key_env` name if different — declared even when the credential came from a literal, because if the variable is set in the host environment the child still must not inherit it |
| `github` | The environment name the adapter actually reads: `provider.token_env` if set, otherwise `GITHUB_TOKEN`. A `$VAR` in `provider.token` declares the referenced name. A **literal** `provider.token` declares **nothing**, because nothing is read from the environment |
| `memory` | The configured secret env name if one is set; otherwise nothing |
| Base default | `[]` — an adapter that declares nothing strips nothing (`test_agent_runner.py::test_an_adapter_declaring_no_secrets_strips_nothing`) |

The chain is: adapter declares → `agent/runner.py` passes
`secret_env_names=tuple(tracker.secret_environment_names())` → `app_server`
removes each name from the child's environment copy at spawn. All three links
verified in source.

The SSH launcher is stricter than SPEC 15.3 requires: the remote command line
carries **no environment assignments at all**. The local environment is passed to
the `ssh` client (it needs `HOME` and `SSH_AUTH_SOCK`) but is not forwarded to
the remote command, which removes the question of tracker credentials reaching a
remote child entirely.

### 4.3 What secret handling does *not* protect

1. **Everything not declared.** Stripping is a per-adapter allowlist of *tracker*
   credentials. `AWS_SECRET_ACCESS_KEY`, `OPENAI_API_KEY`, `NPM_TOKEN`,
   `SSH_AUTH_SOCK`, and every other secret in the service user's environment are
   inherited by the coding-agent child **by design** — Codex needs its own auth.
   Do not run this as a user whose environment holds secrets the agent should not
   have.
2. **Literal credentials in `WORKFLOW.md`.** SPEC 15.3 warns against them and the
   adapters warn again: a literal token in a repo-owned file is readable by any
   child with workspace access, and no amount of env-stripping helps. Use
   `$VAR_NAME` indirection.
3. **Secrets on disk in the workspace.** `.env` files, `~/.netrc`, checked-out
   credentials — the agent reads the filesystem with the service user's rights.
4. **Secrets in issue text.** If someone pastes a token into a ticket
   description, it is rendered into the prompt and appears on the dashboard.
   Nothing scans for it.

## 5. Hook trust (SPEC 15.4)

SPEC 15.4 states the position bluntly: workspace hooks are arbitrary shell
scripts from `WORKFLOW.md` and are **fully trusted configuration**. This
implementation agrees and adds the operational controls SPEC 15.4 asks for:

| SPEC 15.4 point | Status |
|---|---|
| Hooks are fully trusted configuration | Accepted. `WORKFLOW.md` is inside the trust boundary (§1) |
| Hooks run inside the workspace directory | Enforced — the workspace is `cwd` (`test_hooks.py::test_hook_runs_with_the_workspace_directory_as_cwd`) |
| Hook output SHOULD be truncated in logs | `truncate_for_log` keeps head and tail (`test_hooks.py::test_hook_output_is_truncated_in_logs_and_error_details`) |
| Hook timeouts are REQUIRED | `hooks.timeout_ms`, default 60000, enforced by killing the **whole process tree** — a Windows Job Object or a POSIX process group — not by abandoning the subprocess (`test_hooks.py::test_timeout_kills_the_whole_process_tree`) |

Two additional hardening details, both verified:

- **stdin is closed**, so an interactive hook cannot hang the orchestrator waiting
  for input (`test_hooks.py::test_stdin_is_closed_so_an_interactive_hook_cannot_hang`).
- **The shell is reported, never silently substituted.** SPEC 9.4's `sh -lc` is
  honored wherever a POSIX shell exists, including Windows via Git Bash/MSYS2/
  Cygwin. Only with no POSIX shell on `PATH` does it fall back to
  `%COMSPEC% /d /s /c`, and that fallback is surfaced on `HookShell.kind` and
  logged on every hook start — so an operator can see that hook scripts on that
  host are being interpreted by `cmd.exe` rather than discovering it from
  behavior.

**The cost:** anyone who can land a change to `WORKFLOW.md` gets shell execution
as the service user, before any agent runs, on every workspace creation and every
attempt. Treat `WORKFLOW.md` with the same review discipline as CI configuration
— because that is exactly what it is. A hook that backgrounds a process without
redirecting output (`mydaemon &`) holds the stdout pipe open, is treated as a
timeout, and is killed with its tree; write `mydaemon >/dev/null 2>&1 &`.

## 6. The HTTP extension (SPEC 13.7)

Shipped and OPTIONAL — it starts only when `--port` or `server.port` is set.

**There is no authentication and no authorization on any endpoint.** The
dashboard at `/` and the JSON API at `/api/v1/*` are open to anyone who can reach
the socket, and `POST /api/v1/refresh` is a state-changing trigger with no token
or origin check. The only control is the bind address.

| Control | Status |
|---|---|
| Loopback by default | `DEFAULT_BIND_HOST = "127.0.0.1"` (`test_http.py::test_default_bind_host_is_loopback`, `::test_blank_host_falls_back_to_loopback`) |
| Explicit host overrides it | Yes — configuring a non-loopback host removes the only protection (`::test_explicit_host_overrides_the_loopback_default`) |
| Output escaping | Snapshot values are HTML-escaped (`::test_dashboard_escapes_snapshot_values`) |
| No external asset loading | Verified (`::test_dashboard_fetches_no_external_assets`) |
| No hot-rebind on reload | Listener settings require restart, which SPEC 6.2 permits for extension-owned resources |

What is exposed: issue identifiers, titles, tracker states, URLs, session IDs,
recent agent messages, token totals, and error strings. On a non-loopback bind
that is unauthenticated disclosure of tracker content and agent activity. Put it
behind a reverse proxy with authentication, or leave it on loopback and use an
SSH tunnel.

## 7. Harness hardening (SPEC 15.5)

SPEC 15.5 asks implementations to evaluate their own risk profile, harden where
appropriate, and **document their controls, treating hardening as part of the
core safety model**. Against SPEC 15.5's own list of possible measures:

| SPEC 15.5 measure | This implementation |
|---|---|
| Tighten Codex approval and sandbox settings instead of running maximally permissive | ⛔ **Ships maximally permissive.** `never` + `danger-full-access` are the defaults (§2). Both are configurable; neither is configured tighter out of the box. |
| External isolation: OS/container/VM sandboxing, network restrictions, separate credentials | ⛔ **None provided.** This is deployment configuration and is left entirely to the operator (§8). |
| Filter which issues/projects/boards/teams/labels are eligible for dispatch | ✅ **Available and enforced.** `tracker.required_labels` (case-insensitive, every label must match), `tracker.active_states`, adapter-owned scope selection (GitHub project/owner, Linear team), and adapter `dispatchable` rules — `require_assignee`, assignee allowlists, blocker resolution, draft/PR/archived exclusion. These are the strongest control in the box; use them. |
| Narrow provider-native tools to the intended tracker scope | ✅ **Enforced by the adapters.** Tools respect the configured scope as an authorization boundary (`test_tracker_memory.py::test_tools_respect_the_configured_scope_as_an_authorization_boundary`), tool specs declare mutation capability, and only the selected adapter's tools are advertised. |
| Reduce client-side tools, credentials, paths, and network destinations to the minimum | ⚠️ **Partly.** Tracker credentials are stripped from the child (§4.2) and tool surface is adapter-scoped. Filesystem paths and network destinations are **not** reduced — `danger-full-access` leaves both wide open. |

**Honest summary:** the controls that exist are at the *dispatch* boundary —
which work reaches an agent at all — and at the *credential* boundary. The
controls at the *execution* boundary are configuration hooks that this
implementation ships open. SPEC 15.5 says the correct controls are
deployment-specific; that is true, and it also means an operator who deploys the
defaults unchanged has chosen the permissive end of every execution-side axis.

## 8. What this does not defend against

Read this as the threat list, not as a disclaimer:

1. **Prompt injection from tracker content.** Untrusted issue text → prompt →
   agent with unsandboxed, unreviewed command execution. No input filtering, no
   command allowlist, no output review. This is the primary risk.
2. **Prompt injection from repository content.** The agent reads the workspace.
   A malicious file comment, README, or test fixture is read with the same
   credulity as the ticket.
3. **A malicious or compromised `WORKFLOW.md`.** Shell execution as the service
   user via hooks, arbitrary process launch via `codex.command`, arbitrary
   workspace root via `workspace.root` (which is only constrained relative to
   *itself*, not to any allowlist).
4. **A compromised or hostile tracker.** Adapters validate shape, not intent.
   A tracker that returns attacker-chosen titles, labels, and `native_ref` values
   feeds them straight into prompts and tool context.
5. **Data exfiltration.** No egress control. An agent with network access can send
   anything it can read anywhere it can reach.
6. **Lateral movement from the workspace.** Path invariants bind Symphony's
   operations, not the agent's process (§3).
7. **Multi-tenancy.** There is no per-issue privilege separation. Every workspace
   runs as the same OS user with the same environment; issue `A` and issue `B` can
   read each other's workspaces trivially.
8. **Runaway cost and unbounded change.** `agent.max_turns` bounds the *in-worker*
   turn loop only. The orchestrator's post-exit continuation retry re-dispatches
   about a second after every clean exit for as long as the issue stays active and
   routable (SPEC 7.1, and [`ARCHITECTURE.md`](ARCHITECTURE.md) §4). An issue that
   never leaves an active state is worked continuously — tokens spent, commits
   made — and the stop button is a **tracker state transition**, not a limit in
   this service. There is no global token budget, spend cap, or maximum
   dispatches-per-issue.
9. **Denial of service on the HTTP extension.** No rate limiting; `POST /refresh`
   coalesces but is unauthenticated.
10. **Supply chain.** The Codex binary, this package's dependencies, and anything a
    hook installs are trusted implicitly.
11. **Restart as a security boundary.** It is not one. Workspaces persist across
    restarts by design (SPEC 9.1, 14.3), including whatever an agent left in them.

## 9. What could not be verified

1. **Whether the sandbox and approval values actually reach Codex.** The
   JSON-RPC method and field names in `agent/app_server.py::ProtocolNames` were
   **not confirmed against a running `codex` binary** — it is not installed on
   this host. Only `thread/tokenUsage/updated` appears verbatim in SPEC 13.5. If
   the field carrying `approval_policy` or `thread_sandbox` is misnamed, Codex
   would apply *its own* defaults instead of the configured ones. Those defaults
   may be stricter or looser than what this document describes, and no test here
   can tell you which. **Verify empirically against your Codex version before
   relying on any sandbox setting.**
2. **Adapter behavior against live APIs.** No live GitHub or Linear call has ever
   been made from this tree; the 3 skipped tests are the SPEC 17.8 profile.
   Credential handling is verified against stubbed transports only.
3. **Behavior on hosts other than this one.** Hook shell selection, the Windows
   `cmd.exe` fallback, and process-tree kill were exercised on Windows 10 with Git
   Bash. SPEC 18.3 requires per-host verification.
4. **No independent security review or penetration test has been performed.**
   This document is a self-assessment by the author of the documentation, derived
   from source reading.

## 10. Operator checklist

Before pointing this at a real repository:

- [ ] **Run as a dedicated OS user** with no ambient credentials — no `~/.aws`,
      no `~/.ssh` keys you care about, no cloud metadata access. SPEC 15.2
      RECOMMENDED; not implemented here because it cannot be.
- [ ] **Add external isolation.** Container, VM, or jail, with an egress
      allowlist. This is the control that substitutes for `danger-full-access`.
- [ ] **Restrict `workspace.root` permissions** and put it on a dedicated volume
      if you can (SPEC 15.2).
- [ ] **Decide the sandbox deliberately.** Run
      `codex app-server generate-json-schema`, pick real values for
      `codex.thread_sandbox` and `codex.turn_sandbox_policy`, set them in
      `WORKFLOW.md`, and **verify empirically** that they took effect (§9 item 1).
- [ ] **Consider `set_approval_policy(DENY_ALL)`** or a custom content-sensitive
      policy if the environment is not fully trusted (§2.3).
- [ ] **Narrow dispatch eligibility.** Use `tracker.required_labels` so a ticket
      must be explicitly opted in, plus a tight `active_states` list and adapter
      scope selection. Prefer a private board over a public issue tracker; on a
      public tracker, assume every reporter can write your agent's prompt.
- [ ] **Use `$VAR` indirection for every credential.** Never a literal token in
      `WORKFLOW.md` (§4.3).
- [ ] **Review `WORKFLOW.md` changes like CI configuration** — branch protection,
      required review. Hooks are shell (§5).
- [ ] **Keep the HTTP extension on loopback**, or put it behind an authenticating
      proxy. Do not bind `0.0.0.0` (§6).
- [ ] **Set a cost and change budget outside this service.** Provider-side spend
      limits, plus a tracker workflow that moves issues out of active states.
      There is no budget control here (§8 item 8).
- [ ] **Run the SPEC 17.8 Real Integration Profile** with real credentials in a
      throwaway scope before trusting it with a real one (SPEC 18.3).
- [ ] **Plan for the agent's output being wrong or hostile.** Require human review
      of the pull requests it opens; never auto-merge.

## 11. Reporting a security issue

This is an engineering preview built from the [openai/symphony](https://github.com/openai/symphony)
specification. Report issues through this repository's issue tracker, and do not
include credentials, tokens, or live tracker payloads in a report.

---

## 12. Adversarial audit, 2026-07-28

An audit arm was tasked with *disproving* the four claims above rather than
confirming them, and verified by causing behavior rather than reading code.
Claim 3 (secrets never reach logs, errors, or prompt context) survived every
attack. The other three did not. All findings below are fixed and covered by
regression tests; they are recorded rather than quietly patched because the
reasoning matters more than the diff.

### 12.1 Workspace keys collided on case-insensitive filesystems — fixed

`ENG-42` and `eng-42` produced two distinct keys that resolved to **one
directory** on Windows and macOS, placing two coding agents in the same
workspace. The second issue also saw `created_now=False`, so its `after_create`
hook never ran. Sanitization was not the last normalizer between an identifier
and a directory — the filesystem folds case, and Windows additionally strips
trailing dots and spaces.

SPEC 4.2 mandates the hash suffix only on *sanitization* change, which is a
POSIX-first reading; SPEC 9.5 Invariant 3's actual requirement is collision
resistance. `models.workspace_key` now also appends the hash when the key could
fold — while leaving `.` and `..` untouched so they still reach the containment
check that rejects them. Already-safe lowercase keys stay plain and readable.

### 12.2 `bash -lc` restored the stripped credential — fixed

The strip chain (adapter declares → runner passes → client removes from the
environment copy) was correct and provably insufficient. SPEC 10.1 mandates
`-l`, which sources the login profile **after** the strip — and exporting a
tracker token from `~/.bash_profile` is a normal way for one to be set at all.
A real child process was observed receiving the credential the strip had
removed.

`codex.command` is now prefixed with `unset -v` for each declared secret name,
which runs after profile sourcing and before the agent. The environment strip
is retained as defense in depth. Verified by reproduction: without the prefix
the child prints the token, with it the child prints nothing.

### 12.3 A literal GitHub token declared nothing — fixed

`GITHUB_TOKEN` is set host-side by the `gh` CLI and by every CI runner
regardless of what `WORKFLOW.md` says, so declaring nothing on the grounds that
this adapter used a literal left that host token readable by the agent. The
GitHub adapter now always declares its configured token env name, matching what
the Linear adapter already did.

### 12.4 Two paths could stall a run indefinitely — fixed

SPEC 10.5 states a run MUST NOT stall waiting for user input. Two reachable
paths did:

1. **An unrecognized user-input method name.** Dispatch was exact string
   equality against `ProtocolNames.user_input`, whose spelling is unverified
   against a real Codex. A variant spelling was answered and discarded in a
   loop observed running past **16×** its configured turn timeout. Nothing
   bounded it: the turn-silence timeout and the orchestrator stall timeout are
   both activity-based, and re-requesting is activity. `app_server` now also
   consults `approvals.classify_approval`, which is lenient about spelling by
   design and was simply never asked.
2. **A `FAIL_RUN` verdict could not end a run.** The bridge between
   `agent.approvals` (four outcomes) and the app-server wire (two) collapsed
   them to a boolean, discarding exactly the one SPEC 10.5 depends on. A denied
   request left the agent free to ask again forever. `ApprovalDecision` now
   carries `ends_run`, and the client finishes the turn when it is set.

### 12.5 Open, unresolved

- **`workspace.safety.assert_launch_cwd` is not called from production code.**
  The client's own check validates absolute + `is_dir()` but does not verify
  the workspace against the configured root. No reachable state was found where
  an out-of-root workspace arrives at the client under the default wiring
  (`create_for_issue` gates it), so this is an untested and previously
  mis-documented control rather than a demonstrated hole.
- **A `mkdir` on a Windows device name** (`NUL`) succeeds while creating
  nothing, so `create_for_issue("NUL")` reports a workspace that does not
  exist. Fail-safe — the launch is rejected downstream — but the reported
  success is wrong.
- **Hook output is truncated but not scrubbed.** A hook that echoes a token
  puts it in a log. Hooks are fully trusted configuration per SPEC 15.4, but
  operators should know.
- The `bash -lc` restore applies equally to a **remote** host's login profile
  under the SSH extension; the same `unset` prefix is not yet applied there.
