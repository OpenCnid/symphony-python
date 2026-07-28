# Claude Code stream-json protocol — observed contract

**Status: empirically verified** against `claude 2.1.214` on Windows, 2026-07-28,
by running the CLI and capturing stdout. Every shape below was observed, not
inferred from documentation.

This is the counterpart to `src/symphony/agent/app_server.py`'s
`ProtocolNames` block, which remains *unverified* because no `codex` binary was
available. Where this document and the Claude Code docs disagree, this document
records what the binary actually did.

Re-verify after a Claude Code upgrade with:

```bash
claude -p "Reply with exactly: OK" --output-format stream-json --verbose \
  --permission-mode bypassPermissions --model haiku
```

---

## 1. Invocation

```
claude -p <prompt> --output-format stream-json --verbose [flags...]
```

- `--verbose` is **required** with `--output-format stream-json`; without it the
  stream is suppressed.
- `--print`/`-p` is required for every non-interactive flag below.
- Working directory is the process cwd. There is no `--cwd`; the launcher must
  set it, which is what makes SPEC 9.5 Invariant 1 enforceable.

### Flags this implementation uses

| Flag | Purpose |
|---|---|
| `--output-format stream-json` | NDJSON event stream on stdout |
| `--verbose` | Required for the above to emit |
| `--permission-mode <mode>` | `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan` |
| `--allowedTools` / `--disallowedTools` | Space- or comma-separated; supports `Bash(git *)`, `Read(src/**)`, `mcp__server__*` |
| `--model <alias-or-id>` | `opus`, `sonnet`, `haiku`, `fable`, or a full model id |
| `--session-id <uuid>` | **Pre-assigns** the session id; must be a valid UUID |
| `--resume <session-id>` | Resume a persisted session |
| `--fork-session` | With resume, branch to a new id instead of reusing |
| `--max-turns <n>` | Cap agentic turns inside one invocation |
| `--max-budget-usd <amount>` | Hard spend ceiling; `--print` only |
| `--append-system-prompt <text>` | Add to the default system prompt |
| `--add-dir <dirs...>` | Additional readable directories |
| `--mcp-config <configs...>` | MCP servers from JSON files or strings |
| `--settings <file-or-json>` | Settings file or inline JSON |
| `--no-session-persistence` | Do not write the transcript to disk |

> [!WARNING]
> `--bare` sets `CLAUDE_CODE_SIMPLE=1` and restricts Anthropic auth to
> `ANTHROPIC_API_KEY` / `apiKeyHelper` — **OAuth and keychain are never read**.
> A host authenticated interactively fails with `Not logged in · Please run
> /login`. Observed directly. This implementation does not pass `--bare`;
> `claude.bare: true` is available for API-key deployments that want it.

---

## 2. Event stream

One JSON object per line on stdout. The discriminator is `type`.

### `system` / `init` — first event

```json
{"type":"system","subtype":"init","cwd":"...","session_id":"<uuid>",
 "tools":[...],"mcp_servers":[],"model":"claude-opus-4-8",
 "permissionMode":"dontAsk","claude_code_version":"2.1.214",
 "capabilities":["interrupt_receipt_v1","msg_lifecycle_v1"],
 "apiKeySource":"none","output_style":"default","uuid":"<uuid>"}
```

`session_id` appears here and on **every** subsequent event.

### `assistant`

```json
{"type":"assistant",
 "message":{"id":"...","model":"...","role":"assistant","stop_reason":"...",
   "usage":{"input_tokens":18,"output_tokens":302,
     "cache_creation_input_tokens":29344,"cache_read_input_tokens":28974,
     "server_tool_use":{...},"service_tier":"standard",
     "cache_creation":{"ephemeral_1h_input_tokens":29344,"ephemeral_5m_input_tokens":0}},
   "content":[{"type":"thinking"|"text"|"tool_use", ...}]},
 "parent_tool_use_id":null,"session_id":"<uuid>","uuid":"<uuid>",
 "timestamp":"2026-07-28T16:53:51.814Z"}
```

An `error` key may be present, e.g. `"error":"authentication_failed"`.

A `tool_use` content block:

```json
{"type":"tool_use","id":"toolu_...","name":"Write",
 "input":{"file_path":"...","content":"..."},"caller":{"type":"direct"}}
```

### `user` — tool results

```json
{"type":"user","message":{"content":[
  {"type":"tool_result","tool_use_id":"toolu_...","content":"File created ..."}]}}
```

### `rate_limit_event` — a top-level type, not a `system` subtype

```json
{"type":"rate_limit_event",
 "rate_limit_info":{"status":"allowed","resetsAt":1785274800,
   "rateLimitType":"five_hour","overageStatus":"rejected",
   "overageDisabledReason":"org_level_disabled_until","isUsingOverage":false},
 "session_id":"<uuid>","uuid":"<uuid>"}
```

`resetsAt` is a Unix epoch **in seconds**. This is the SPEC 13.3 `rate_limits`
source and is strictly richer than anything the Codex path exposes.

### `system` / other subtypes

- `thinking_tokens` — emitted repeatedly during extended thinking.
- `post_turn_summary` — `{"summarizes_uuid","status_category","status_detail","needs_action"}`.
  Useful as a humanized status line (SPEC 13.6).

### `result` — final event, exactly one

```json
{"type":"result","subtype":"success","is_error":false,"api_error_status":null,
 "duration_ms":7388,"duration_api_ms":8909,"ttft_ms":5132,"num_turns":2,
 "result":"DONE","stop_reason":"end_turn","session_id":"<uuid>",
 "total_cost_usd":0.0637264,
 "usage":{"input_tokens":18,"output_tokens":302,
   "cache_creation_input_tokens":29344,"cache_read_input_tokens":28974, ...},
 "modelUsage":{},"permission_denials":[],"terminal_reason":"api_error"|null,
 "uuid":"<uuid>"}
```

---

## 3. Traps confirmed by observation

### 3.1 `subtype: "success"` does **not** mean success

An authentication failure produced:

```json
{"type":"result","subtype":"success","is_error":true,
 "result":"Not logged in · Please run /login","terminal_reason":"api_error"}
```

**Success is `is_error == false`.** Keying on `subtype` alone marks a failed run
successful, and the orchestrator would then schedule a continuation retry
(SPEC 7.1) instead of a backoff retry — spinning at 1-second intervals against a
permanently broken credential. This implementation checks `is_error` first and
treats `subtype` as advisory.

### 3.2 Token counts are per-message, not cumulative

`assistant.message.usage` is that message's own usage; `result.usage` is the
turn total. SPEC 13.5 requires absolute thread totals with delta tracking, so
this driver reports **only the `result` usage** upward and ignores per-message
usage — the same discipline `agent/events.py` applies to Codex payloads.

### 3.3 Cost is only on the final event

`total_cost_usd` appears on `result` and nowhere else. A run killed mid-turn
(stall, reconciliation cancel) yields no cost figure for that attempt.

### 3.4 Exit code 143 is SIGTERM

Expected when the orchestrator terminates a run; it is a cancellation, not a
crash, and is mapped to `TurnCancelled` rather than `PortExit`.

---

## 4. Not verified

- **Permission prompts under `--permission-mode manual`.** Every probe ran with
  `bypassPermissions` or `dontAsk`. How an interactive prompt surfaces in
  stream-json — and whether a programmatic caller can answer it — is unobserved.
  This implementation therefore does not offer `manual`; see
  `docs/SECURITY.md`.
- **`--input-format stream-json`.** Officially underdocumented
  ([claude-code#24594](https://github.com/anthropics/claude-code/issues/24594)).
  This driver uses one process per turn with `--resume` for continuation rather
  than a long-lived stdin conversation, so it does not depend on that format.
- **`permission_denials` populated.** The array was observed present but always
  empty; its element shape is unknown.
