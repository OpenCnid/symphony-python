# Conformance

This document exists to make the conformance claim **auditable**. It maps every
REQUIRED item in [SPEC 18.1](../SPEC.md) and every bullet in the [SPEC 17](../SPEC.md)
test matrix to the module that implements it and the test that covers it, with an
honest status per row.

## Method, and the limits of this audit

**Snapshot.** Working tree of `D:\symphony-python`, 2026-07-28. Every path in the
[`CONTRACTS.md`](../CONTRACTS.md) ownership map existed. Full-suite result:

```
1315 passed, 3 skipped in 111.21s
```

across 21 files under `tests/`. The 3 skips are the SPEC 17.8 Real Integration
Profile, each reporting its missing credential (`SYMPHONY_TEST_SSH_HOST` /
`SYMPHONY_TEST_SSH_ROOT`, `SYMPHONY_GH_OWNER` / `SYMPHONY_GH_PROJECT`,
`LINEAR_API_KEY` / `LINEAR_TEAM_KEY`) — reported as skipped, not passed, which is
itself a SPEC 17.8 requirement.

**The tree was still being modified during this audit.** Modules were landing in
parallel while it was written; an earlier run of the same command reported 1312
passed. Rows were mapped against source read at various points in that window, so
a module may have changed after its row was written. Re-run the suite and spot-check
before relying on this table for a release decision.

**What "covered" rests on.** Each ✅ row means: the named module exists and its
implementation of the requirement was located in source, *and* the named test
exists in the passing suite. It does **not** mean every cited test body was read
line by line — the mapping rests on test names, module source, and the suite
result. A test whose name promises more than its body delivers would be recorded
here as covered. That is the residual risk of this method, stated rather than
hidden.

**Status vocabulary**

| | Meaning |
|---|---|
| ✅ | Implemented in a module on disk and exercised by a named passing test. |
| ⚠️ | Implemented and tested, but a stated caveat limits what the test proves. |
| ⛔ | Cannot be verified on this host. The reason is always given. |
| ➖ | Extension Conformance for an extension this implementation ships, or a bullet that does not apply. |

**Profiles.** SPEC 17 distinguishes `Core Conformance` (deterministic, REQUIRED
of all implementations), `Extension Conformance` (REQUIRED only for OPTIONAL
features an implementation chooses to ship — the bullets beginning "If … is
implemented"), and the `Real Integration Profile` (environment-dependent,
RECOMMENDED before production). Sections 17.1–17.7 are Core Conformance except
where a bullet is marked Extension below; 17.8 is the Real Integration Profile.
Section 18.1 = Core, 18.2 = Extension, 18.3 = Real Integration.

---

## 1. SPEC 18.1 — REQUIRED for Conformance

| # | Requirement | Module | Test | Status |
|---|---|---|---|---|
| 1 | Workflow path selection supports explicit runtime path and cwd default | `workflow/loader.py::resolve_workflow_path`; `cli.py` | `test_workflow_loader.py::test_default_path_is_workflow_md_in_cwd`, `::test_explicit_path_wins_over_cwd_default`; `test_cli.py::test_parser_accepts_positional_workflow_path`, `::test_default_resolves_to_workflow_md_in_cwd` | ✅ |
| 2 | `WORKFLOW.md` loader with YAML front matter + prompt body split | `workflow/loader.py::load_workflow` | `test_workflow_loader.py::test_front_matter_and_body_are_split`, `::test_config_is_the_front_matter_root_not_nested`, `::test_prompt_body_is_trimmed` | ✅ |
| 3 | Typed config layer with defaults and `$` resolution | `workflow/config.py::build_config`, `::expand_value` | `test_config.py::test_every_spec_6_4_default_applies_when_front_matter_is_empty`, `::test_expand_value_expands_bare_and_braced_variables`, `::test_expand_value_treats_an_empty_variable_as_missing` | ✅ |
| 4 | Dynamic `WORKFLOW.md` watch/reload/re-apply for config and prompt | `workflow/watcher.py`; `cli.py` reload wiring; `orchestrator/core.py::apply_config` | `test_watcher.py::test_filesystem_event_triggers_reload`, `::test_invalid_reload_keeps_last_known_good_and_does_not_raise`; `test_cli.py::test_reload_applies_new_config_to_live_behavior`; `test_orchestrator.py::test_apply_config_updates_effective_poll_interval_and_limits` | ✅ |
| 5 | Polling orchestrator with single-authority mutable state | `orchestrator/core.py::Orchestrator` (mailbox loop) | `test_orchestrator.py::test_tick_dispatches_in_spec_sort_order`, `::test_worker_exit_is_not_applied_while_a_tick_is_in_flight`, `::test_simultaneous_worker_exits_each_produce_exactly_one_retry`, `::test_invoke_runs_inside_the_mailbox_and_returns_a_value` | ✅ |
| 6 | Issue tracker adapter with state-list + ID-refresh reads | `trackers/base.py`; `trackers/{memory,github,linear}.py` | `test_tracker_memory.py::test_state_fetch_applies_configured_active_states`, `::test_id_refresh_returns_full_normalized_snapshots`; equivalents in `test_tracker_github.py`, `test_tracker_linear.py` | ⚠️ Deterministic coverage is complete. GitHub/Linear are exercised against stubbed transports only — no live API call has ever been made from this tree (see §5). |
| 7 | Workspace manager with sanitized, collision-resistant per-issue workspaces | `models.py::workspace_key`; `workspace/manager.py`; `workspace/safety.py` | `test_workspace.py::test_path_for_is_deterministic_and_a_direct_child_of_root`, `::test_path_for_distinguishes_identifiers_that_sanitize_alike`, `::test_path_for_keeps_plain_key_when_sanitization_changes_nothing` | ✅ |
| 8 | Workspace lifecycle hooks (`after_create`, `before_run`, `after_run`, `before_remove`) | `workspace/hooks.py`; `workspace/manager.py`; `agent/runner.py` | `test_hooks.py::test_hook_names_are_the_four_spec_hooks`, `::test_default_fatal_matches_spec_9_4_table`, `::test_failure_disposition_follows_spec_9_4_per_hook`; `test_agent_runner.py::test_before_run_is_fatal_and_after_run_is_not` | ✅ |
| 9 | Hook timeout config (`hooks.timeout_ms`, default `60000`) | `workflow/config.py::DEFAULT_HOOK_TIMEOUT_MS`; `workspace/hooks.py::HookRunner.timeout_ms` | `test_config.py::test_invalid_hook_timeout_fails_configuration_validation`; `test_hooks.py::test_valid_timeout_is_taken_from_config`, `::test_invalid_timeout_degrades_to_the_spec_default`, `::test_timeout_kills_the_whole_process_tree` | ✅ |
| 10 | Coding-agent app-server subprocess client with the targeted transport/framing protocol | `agent/app_server.py` | `test_app_server.py` (46 tests), incl. `::test_launch_argv_is_bash_lc_with_workspace_cwd`, `::test_max_line_size_is_ten_megabytes`, `::test_stderr_is_kept_separate_from_the_protocol_stream`, `::test_whole_lifecycle_works_under_renamed_protocol_strings` | ⚠️ Transport, framing, lifecycle, and timeout behavior are covered against a **scripted stdio peer**. The JSON-RPC method names in `ProtocolNames` were never confirmed against a running `codex` binary (see §5). |
| 11 | Codex launch command config (`codex.command`, default `codex app-server`) | `workflow/config.py::DEFAULT_CODEX_COMMAND`; `agent/app_server.py` argv construction | `test_config.py::test_codex_command_is_preserved_verbatim`; `test_app_server.py::test_launch_argv_is_bash_lc_with_workspace_cwd` | ✅ |
| 12 | Strict prompt rendering with `issue` and `attempt` variables | `workflow/template.py::render_prompt` | `test_template.py::test_renders_issue_and_attempt`, `::test_unknown_top_level_variable_raises_render_error`, `::test_unknown_filter_raises_render_error` | ✅ |
| 13 | Exponential retry queue with continuation retries after normal exit | `orchestrator/retry.py`; `orchestrator/core.py` worker-exit handler | `test_retry.py::test_continuation_delay_is_the_spec_constant`, `::test_backoff_base_case_attempt_one_is_10000_not_20000`; `test_orchestrator.py::test_normal_exit_schedules_a_one_second_continuation_retry`, `::test_abnormal_exit_schedules_backoff_retry_with_error` | ✅ |
| 14 | Configurable retry backoff cap (`agent.max_retry_backoff_ms`, default 5m) | `workflow/config.py::DEFAULT_MAX_RETRY_BACKOFF_MS`; `orchestrator/retry.py::backoff_delay_ms` | `test_retry.py::test_backoff_cap_uses_configured_max_not_the_default`, `::test_backoff_series_with_default_cap`; `test_orchestrator.py::test_repeated_failures_escalate_backoff_and_respect_the_cap` | ✅ |
| 15 | Reconciliation that stops runs on terminal/non-active tracker states | `orchestrator/reconcile.py`; `orchestrator/core.py` | `test_orchestrator.py::test_reconcile_terminal_state_stops_worker_and_cleans_workspace`, `::test_reconcile_non_active_state_stops_worker_without_cleanup`, `::test_reconcile_active_but_unroutable_stops_worker_without_cleanup`; `test_reconcile.py` (42 tests) | ✅ |
| 16 | Workspace cleanup for terminal issues (startup sweep + active transition) | `orchestrator/reconcile.py::startup_terminal_workspace_cleanup`; `workspace/manager.py::cleanup` | `test_orchestrator.py::test_start_performs_startup_terminal_workspace_cleanup`, `::test_start_continues_when_terminal_fetch_fails`; `test_workspace.py::test_cleanup_removes_the_workspace_and_reports_true` | ✅ |
| 17 | Structured logs with `issue_id`, `issue_identifier`, and `session_id` | `observability/logging.py` | `test_observability.py::test_bound_issue_context_appears_as_key_value`, `::test_session_lifecycle_context_is_rendered`, `::test_required_context_fields_lead_the_line_in_stable_order` | ✅ |
| 18 | Operator-visible observability (structured logs; OPTIONAL snapshot/status surface) | `observability/{logging,snapshot,status}.py`; `http/` | `test_observability.py::test_sink_failure_does_not_crash_the_caller`, `::test_snapshot_top_level_shape_matches_spec_13_7_2`; `test_cli.py::test_startup_failure_prints_one_clean_line_without_traceback` | ✅ |

**18.1 tally: 18 of 18 mapped to code on disk. 16 ✅, 2 ⚠️, 0 design-only, 0 unmapped.**

---

## 2. SPEC 17.1 — Workflow and Config Parsing *(Core Conformance)*

| Bullet | Module | Test | Status |
|---|---|---|---|
| Workflow path precedence: explicit runtime path used when provided | `workflow/loader.py` | `test_workflow_loader.py::test_explicit_path_wins_over_cwd_default`, `::test_explicit_relative_path_is_made_absolute` | ✅ |
| Workflow path precedence: cwd default `WORKFLOW.md` when no explicit path | `workflow/loader.py` | `test_workflow_loader.py::test_default_path_is_workflow_md_in_cwd`, `::test_blank_explicit_path_falls_back_to_cwd_default` | ✅ |
| Workflow file changes detected → re-read/re-apply without restart | `workflow/watcher.py` | `test_watcher.py::test_filesystem_event_triggers_reload`, `::test_reload_applies_changed_content_and_notifies`, `::test_is_stale_detects_edits_without_any_event_stream` | ✅ |
| Invalid reload keeps last known good config + operator-visible error | `workflow/watcher.py` | `test_watcher.py::test_invalid_reload_keeps_last_known_good_and_does_not_raise`, `::test_invalid_reload_emits_operator_visible_error`, `::test_watcher_recovers_after_a_failed_reload`; `test_cli.py::test_reload_failure_is_not_fatal_and_keeps_last_known_good` | ✅ |
| Missing `WORKFLOW.md` returns typed error | `workflow/loader.py`; `errors.MissingWorkflowFile` | `test_workflow_loader.py::test_missing_file_raises_missing_workflow_file`, `::test_directory_at_workflow_path_raises_missing_workflow_file` | ✅ |
| Invalid YAML front matter returns typed error | `workflow/loader.py`; `errors.WorkflowParseError` | `test_workflow_loader.py::test_invalid_yaml_raises_workflow_parse_error`, `::test_unterminated_front_matter_raises_workflow_parse_error` | ✅ |
| Front matter non-map returns typed error | `workflow/loader.py`; `errors.WorkflowFrontMatterNotAMap` | `test_workflow_loader.py::test_non_map_front_matter_raises_typed_error` | ✅ |
| Config defaults apply when OPTIONAL values are missing | `workflow/config.py` | `test_config.py::test_every_spec_6_4_default_applies_when_front_matter_is_empty`, `::test_defaults_are_also_the_dataclass_defaults` | ✅ |
| `tracker.kind` validation enforces a supported adapter | `workflow/config.py::validate_dispatch_config`; `trackers/base.py::build_adapter` | `test_config.py::test_preflight_rejects_a_missing_tracker_kind`, `::test_preflight_rejects_an_unsupported_tracker_kind` | ✅ |
| `tracker.provider` preserves adapter-owned keys, validated through the adapter | `workflow/config.py` | `test_config.py::test_tracker_provider_preserves_adapter_owned_keys_verbatim`, `::test_tracker_provider_is_not_expanded_by_core`, `::test_preflight_reports_provider_rejection_from_the_adapter` | ✅ |
| `$VAR` resolution for documented adapter secret keys and path values | `workflow/config.py::expand_value`; adapter `_resolve_token` | `test_config.py::test_workspace_root_expands_a_variable`; `test_tracker_github.py::test_dollar_var_indirection_declares_the_referenced_name`, `::test_empty_dollar_var_is_a_missing_secret_and_never_echoes_the_value` | ✅ |
| `~` path expansion works | `workflow/config.py::expand_value` | `test_config.py::test_expand_value_expands_leading_tilde`, `::test_workspace_root_expands_tilde` | ✅ |
| `codex.command` preserved as a shell command string | `workflow/config.py` | `test_config.py::test_codex_command_is_preserved_verbatim` | ✅ |
| Per-state concurrency map normalizes names, ignores invalid values | `workflow/config.py::_by_state_limits`; `ServiceConfig.slot_limit_for_state` | `test_config.py::test_per_state_concurrency_keys_normalize_by_trim_and_lowercase`, `::test_per_state_concurrency_ignores_invalid_entries_without_failing`, `::test_slot_limit_for_state_prefers_the_override_then_the_global_limit` | ✅ |
| Prompt template renders `issue` and `attempt` | `workflow/template.py` | `test_template.py::test_renders_issue_and_attempt`, `::test_attempt_is_none_on_first_run`, `::test_nested_collections_are_iterable` | ✅ |
| Prompt rendering fails on unknown variables (strict mode) | `workflow/template.py` | `test_template.py::test_unknown_top_level_variable_raises_render_error`, `::test_unknown_issue_field_raises_render_error`, `::test_unknown_variable_does_not_render_empty` | ✅ |

## 3. SPEC 17.2 — Workspace Manager and Safety *(Core Conformance)*

| Bullet | Module | Test | Status |
|---|---|---|---|
| Deterministic workspace path per issue identifier | `workspace/manager.py::path_for` | `test_workspace.py::test_path_for_is_deterministic_and_a_direct_child_of_root` | ✅ |
| Missing workspace directory is created | `workspace/manager.py::create_for_issue` | `test_workspace.py::test_create_makes_the_missing_directory`, `::test_create_creates_the_root_on_demand` | ✅ |
| Existing workspace directory is reused | `workspace/manager.py` | `test_workspace.py::test_create_reuses_an_existing_directory`, `::test_reuse_never_resets_workspace_contents` | ✅ |
| Existing non-directory at workspace location handled safely per policy | `workspace/manager.py::_ensure_directory` | `test_workspace.py::test_non_directory_at_the_workspace_path_fails_and_is_not_unlinked`, `::test_create_fails_with_a_typed_error_when_the_root_is_a_file` | ✅ Policy: **fail the attempt, never unlink** (README, CONTRACTS §5). |
| OPTIONAL workspace population/sync errors are surfaced | `workspace/hooks.py` (population is hook-driven; SPEC 9.3 requires no built-in VCS) | `test_hooks.py::test_fatal_failure_carries_hook_name_cwd_and_output`; `test_agent_runner.py::test_before_run_failure_aborts_before_any_session_exists` | ⚠️ Extension-shaped: this implementation ships **no** built-in population step, so the bullet is discharged through hook failure propagation rather than a dedicated populate path. Reused workspaces are never destructively reset (CONTRACTS §5). |
| `after_create` runs only on new workspace creation | `workspace/manager.py` | `test_workspace.py::test_after_create_runs_fatally_inside_the_new_workspace`, `::test_concurrent_creates_run_after_create_exactly_once`, `::test_create_treats_a_directory_appearing_mid_call_as_reuse` | ✅ |
| `before_run` runs before each attempt; failure/timeout aborts the attempt | `agent/runner.py`; `workspace/hooks.py` | `test_agent_runner.py::test_before_run_is_fatal_and_after_run_is_not`, `::test_before_run_failure_aborts_before_any_session_exists`; `test_hooks.py::test_fatal_timeout_raises_hook_timeout` | ✅ |
| `after_run` runs after each attempt; failure/timeout logged and ignored | `agent/runner.py`; `workspace/hooks.py` | `test_agent_runner.py::test_every_exit_path_matches_the_declared_cleanup_pair`, `::test_after_run_failure_does_not_fail_a_successful_attempt`, `::test_after_run_failure_does_not_mask_the_original_error`; `test_hooks.py::test_non_fatal_timeout_is_logged_and_ignored` | ✅ |
| `before_remove` runs on cleanup; failures/timeouts ignored | `workspace/manager.py::cleanup` | `test_workspace.py::test_cleanup_runs_before_remove_non_fatally_while_the_workspace_exists`, `::test_cleanup_ignores_a_before_remove_failure` | ✅ |
| Sanitization, collision resistance, and root containment enforced before agent launch | `models.py::workspace_key`; `workspace/safety.py` | `test_workspace.py::test_within_root_rejects_parent_traversal`, `::test_within_root_rejects_sibling_sharing_a_string_prefix`, `::test_within_root_rejects_symlink_escape`, `::test_path_for_neutralizes_separators_in_the_identifier` | ✅ |
| Unchanged identifiers keep deterministic keys; distinct identifiers that sanitize alike get distinct keys | `models.py::workspace_key` | `test_workspace.py::test_path_for_keeps_plain_key_when_sanitization_changes_nothing`, `::test_path_for_distinguishes_identifiers_that_sanitize_alike` | ✅ |
| Agent launch uses the per-issue workspace as cwd and rejects out-of-root paths | `workspace/safety.py::assert_launch_cwd`; `agent/app_server.py` | `test_workspace.py::test_launch_cwd_accepts_the_workspace_path`, `::test_launch_cwd_rejects_a_sibling_workspace`; `test_app_server.py::test_child_process_really_runs_in_the_workspace`, `::test_relative_workspace_is_rejected_before_launch` | ✅ |

## 4. SPEC 17.3 — Issue Tracker Adapter *(Core Conformance)*

Coverage is claimed on three adapters. The `memory` adapter is the deterministic
reference and carries the Core Conformance claim; `github` and `linear` are
exercised against stubbed transports.

| Bullet | Module | Test | Status |
|---|---|---|---|
| Candidate fetch applies configured active states + adapter scope selection | `trackers/{memory,github,linear}.py` | `test_tracker_memory.py::test_state_fetch_applies_configured_active_states`, `::test_state_fetch_applies_provider_scope_selection`; `test_tracker_github.py::test_state_filter_excludes_items_in_other_board_states` | ✅ |
| Empty `fetch_issues_by_states([])` returns empty without a provider call | `trackers/base.py` contract; all three adapters | `test_tracker_memory.py::test_empty_state_list_returns_empty_without_a_provider_call`; `test_tracker_github.py::test_empty_state_list_returns_empty_without_a_provider_request` | ✅ |
| Empty `fetch_issues_by_ids([])` returns empty without a provider call | all three adapters | `test_tracker_memory.py::test_empty_id_list_returns_empty_without_a_provider_call`; `test_tracker_github.py::test_empty_id_list_returns_empty_without_a_provider_request` | ✅ |
| Pagination preserves order across multiple pages | `trackers/{memory,github,linear}.py` | `test_tracker_memory.py::test_pagination_preserves_order_across_pages`; `test_tracker_github.py::test_pagination_preserves_order_across_pages`, `::test_item_repeated_across_pages_is_kept_once_in_first_seen_order` | ✅ |
| Labels normalized to lowercase | `trackers/base.py::normalize_labels` | `test_tracker_memory.py::test_labels_are_lowercased_trimmed_deduped_and_blank_dropped`; `test_tracker_github.py::test_labels_are_lowercased_trimmed_and_deduplicated` | ✅ |
| Unusable optional metadata → null/empty without hiding valid required fields | adapters + `trackers/base.py` coercers | `test_tracker_memory.py::test_unusable_optional_metadata_normalizes_without_hiding_required_fields`; `test_tracker_github.py::test_timestamps_parse_and_unusable_values_normalize_to_null` | ✅ |
| State-list logs omitted malformed records; ID refresh **fails** on malformed requested records | adapters | `test_tracker_memory.py::test_state_fetch_omits_and_logs_malformed_records`, `::test_id_refresh_fails_on_a_malformed_requested_record`; `test_tracker_github.py::test_state_list_omits_malformed_records_and_keeps_valid_ones`, `::test_id_refresh_fails_on_a_malformed_requested_record` | ✅ |
| Refresh by opaque dispatch ID returns full normalized snapshots | adapters | `test_tracker_memory.py::test_id_refresh_returns_full_normalized_snapshots`, `::test_id_refresh_treats_input_as_a_set`; `test_tracker_github.py::test_refresh_returns_full_normalized_snapshots` | ✅ |
| Distinct provider ticket ID / project-item ID preserved in `native_ref` | adapters | `test_tracker_github.py::test_project_item_id_is_the_dispatch_identity`, `::test_native_ref_preserves_the_distinct_underlying_ids`; `test_tracker_memory.py::test_native_ref_preserves_the_distinct_ticket_id` | ✅ |
| Provider-specific routing/blocker/assignment rules become explicit `dispatchable` | adapters | `test_tracker_github.py::test_draft_item_is_not_dispatchable`, `::test_require_assignee_makes_unassigned_items_undispatchable`, `::test_open_issue_dependency_blocks_dispatch_and_populates_blocked_by`; `test_tracker_memory.py::test_unresolved_blocker_makes_the_issue_non_dispatchable` | ✅ |
| Adapter publishes the required compact profile (SPEC 11.2) | `docs/adapters/{memory,github,linear}.md` | `test_tracker_memory.py::test_adapter_publishes_the_required_profile_document` | ⚠️ A profile document exists for each adapter and is checked for `memory`. The profiles are owned by other authors; this audit did not read them against SPEC 11.2's seven required sections. |
| Error mapping covers config, request, non-success, malformed, pagination, rate limiting, with documented language-native mappings | `errors.py` `TrackerError` family; adapters | `test_tracker_github.py::test_transport_failure_maps_to_tracker_request`, `::test_unauthorized_maps_to_tracker_status`, `::test_primary_rate_limit_uses_the_reset_header`, `::test_repeated_cursor_raises_pagination_error_instead_of_looping`, `::test_non_json_body_maps_to_tracker_response`; `test_tracker_memory.py::test_injected_*` fault family | ✅ |

## 5. SPEC 17.4 — Orchestrator Dispatch, Reconciliation, and Retry *(Core Conformance)*

| Bullet | Module | Test | Status |
|---|---|---|---|
| Dispatch sort order is priority then oldest creation time | `orchestrator/scheduling.py::sort_for_dispatch` | `test_scheduling.py::test_priority_1_to_4_bucket_precedes_every_other_priority_including_zero`, `::test_created_at_oldest_first_within_the_same_priority`, `::test_identifier_breaks_ties_lexicographically`; `test_orchestrator.py::test_tick_dispatches_in_spec_sort_order` | ✅ |
| `dispatchable=false` issues are not eligible | `orchestrator/scheduling.py` | `test_scheduling.py::test_issue_routable_requires_dispatchable_true`, `::test_should_dispatch_rejects_dispatchable_false` | ✅ |
| Required-label filtering is case-insensitive, applied after normalization | `models.py::Issue.has_labels`; `scheduling.py::issue_routable` | `test_scheduling.py::test_issue_routable_label_match_ignores_case_and_surrounding_whitespace`, `::test_issue_routable_blank_configured_label_matches_no_issue` | ✅ |
| Active-state issue refresh updates running entry state | `orchestrator/reconcile.py`; `core.py` | `test_orchestrator.py::test_reconcile_refreshes_the_running_issue_snapshot` | ✅ |
| Non-active state stops running agent without workspace cleanup | `orchestrator/reconcile.py` | `test_orchestrator.py::test_reconcile_non_active_state_stops_worker_without_cleanup`, `::test_reconcile_active_but_unroutable_stops_worker_without_cleanup`, `::test_reconcile_missing_issue_stops_worker_without_cleanup` | ✅ |
| Terminal state stops running agent and cleans workspace | `orchestrator/reconcile.py` | `test_orchestrator.py::test_reconcile_terminal_state_stops_worker_and_cleans_workspace` | ✅ |
| Reconciliation with no running issues is a no-op | `orchestrator/reconcile.py` | `test_orchestrator.py::test_reconcile_is_a_noop_with_no_running_issues` | ✅ |
| Normal worker exit schedules a short continuation retry (attempt 1) | `orchestrator/retry.py::schedule_continuation`; `core.py` | `test_orchestrator.py::test_normal_exit_schedules_a_one_second_continuation_retry`; `test_retry.py::test_schedule_continuation_uses_attempt_one_and_no_error` | ✅ |
| Abnormal worker exit increments retries with 10s-based exponential backoff | `orchestrator/retry.py::backoff_delay_ms` | `test_orchestrator.py::test_abnormal_exit_schedules_backoff_retry_with_error`; `test_retry.py::test_backoff_base_case_attempt_one_is_10000_not_20000`, `::test_backoff_doubles_until_the_cap_engages` | ✅ |
| Retry backoff cap uses configured `agent.max_retry_backoff_ms` | `orchestrator/retry.py` | `test_retry.py::test_backoff_cap_uses_configured_max_not_the_default`, `::test_max_backoff_reload_affects_later_schedules`; `test_orchestrator.py::test_repeated_failures_escalate_backoff_and_respect_the_cap` | ✅ |
| Retry queue entries include attempt, due time, identifier, and error | `models.py::RetryEntry`; `orchestrator/retry.py` | `test_retry.py::test_schedule_failure_stores_every_required_field`, `::test_due_at_ms_is_read_from_the_injected_monotonic_clock` | ✅ |
| Stall detection kills stalled sessions and schedules retry | `orchestrator/reconcile.py` Part A | `test_orchestrator.py::test_stall_detection_terminates_and_queues_a_retry`, `::test_stall_clock_restarts_from_the_last_agent_event`, `::test_stall_detection_disabled_when_timeout_is_not_positive` | ✅ |
| Slot exhaustion requeues retries with explicit error reason | `orchestrator/core.py` retry handler | `test_orchestrator.py::test_retry_timer_requeues_with_explicit_error_when_slots_are_exhausted`, `::test_retry_timer_requeues_when_only_the_per_state_slot_is_full` | ✅ |
| *Extension:* snapshot API returns running rows, retry rows, token totals, rate limits | `observability/snapshot.py` | `test_observability.py::test_snapshot_top_level_shape_matches_spec_13_7_2`, `::test_snapshot_running_row_matches_spec_13_7_2`, `::test_snapshot_retry_row_derives_wall_clock_due_at` | ➖ Extension Conformance — shipped, covered. |
| *Extension:* snapshot timeout/unavailable cases are surfaced | `http/api.py` | `test_http.py::test_snapshot_failure_becomes_503_snapshot_unavailable`, `::test_snapshot_timeout_becomes_504_snapshot_timeout` | ➖ Extension Conformance — shipped, covered. |

## 6. SPEC 17.5 — Coding-Agent App-Server Client *(Core Conformance)*

This is the section carrying the implementation's largest unverified surface. Read
the ⛔ and ⚠️ rows before treating it as green.

| Bullet | Module | Test | Status |
|---|---|---|---|
| Launch uses workspace cwd and invokes `bash -lc <codex.command>` | `agent/app_server.py` | `test_app_server.py::test_launch_argv_is_bash_lc_with_workspace_cwd`, `::test_child_process_really_runs_in_the_workspace`, `::test_bash_is_resolved_through_path_not_createprocess_search_order` | ✅ |
| Session startup follows the targeted Codex app-server protocol | `agent/app_server.py::ProtocolNames` | `test_app_server.py::test_startup_initializes_then_creates_thread_with_cwd_and_policies` | ⛔ The startup *sequence* is covered against a scripted peer; the *method names it sends* are unconfirmed against a real `codex` binary. See §10. |
| Client identity/capability payloads valid when the protocol requires them | `agent/app_server.py` | `test_app_server.py::test_startup_initializes_then_creates_thread_with_cwd_and_policies` | ⛔ Same reason. |
| Policy-related startup payloads use the documented approval/sandbox settings | `agent/app_server.py`; `workflow/config.py::CodexConfig` | `test_app_server.py::test_turn_carries_prompt_title_cwd_and_sandbox_policy`, `::test_default_policy_is_the_documented_high_trust_posture` | ⚠️ The values sent are the documented ones; whether Codex accepts these exact field names is unconfirmed. |
| Thread and turn identities extracted and used to emit `session_started` | `agent/app_server.py`; `models.py::session_id` | `test_app_server.py::test_session_started_carries_composed_session_id`, `::test_identity_is_read_from_a_nested_wrapper_shape`, `::test_thread_response_without_identity_maps_to_response_error` | ✅ |
| Request/response read timeout is enforced | `agent/app_server.py` | `test_app_server.py::test_read_timeout_bounds_a_request_response_exchange` | ✅ |
| Turn timeout is enforced | `agent/app_server.py` | `test_app_server.py::test_turn_timeout_fires_on_stream_silence`, `::test_turn_timeout_is_a_silence_bound_not_a_total_runtime_cap` | ✅ |
| Transport framing handled correctly | `agent/app_server.py` | `test_app_server.py::test_non_json_line_emits_malformed_and_the_session_survives`, `::test_oversize_line_is_dropped_and_the_session_survives`, `::test_max_line_size_is_ten_megabytes` | ✅ (framing shape; see §10 caveat) |
| stdio transports keep diagnostic stderr separate from the protocol stream | `agent/app_server.py` | `test_app_server.py::test_stderr_is_kept_separate_from_the_protocol_stream` | ✅ |
| Command/file-change approvals handled per documented policy | `agent/approvals.py`; `agent/app_server.py` | `test_app_server.py::test_command_approval_is_auto_approved_without_stalling`, `::test_file_change_approval_is_auto_approved`; `test_agent_events.py::test_documented_posture_auto_approves_commands_and_file_changes_for_the_session` | ✅ |
| Unsupported dynamic tool calls rejected without stalling | `agent/app_server.py`; `trackers/base.py` | `test_app_server.py::test_unsupported_tool_call_fails_structurally_without_stalling`, `::test_unknown_server_request_is_answered_not_ignored` | ✅ |
| User-input requests handled per documented policy, never stalling | `agent/approvals.py` | `test_app_server.py::test_user_input_request_fails_fast_and_answers_the_request`; `test_agent_events.py::test_documented_posture_fails_the_run_on_user_input`, `::test_no_shipped_policy_can_stall_a_run` | ✅ Policy: **hard failure** (README, CONTRACTS §5). |
| Usage and rate-limit telemetry extracted | `agent/events.py` | `test_app_server.py::test_token_usage_notification_surfaces_the_usage_map`, `::test_rate_limit_payload_is_passed_through`; `test_agent_events.py::test_extract_rate_limits_finds_nested_payload` | ✅ |
| Approval, user-input, usage, and rate-limit signals interpreted per the targeted protocol | `agent/{app_server,events,approvals}.py` | as above | ⛔ Interpretation logic is covered; the mapping from *real* Codex signal names to these handlers is unconfirmed. |
| *Extension:* session startup advertises supported client-side tool specs | `agent/app_server.py` | `test_app_server.py::test_tool_specs_are_advertised_at_thread_start`, `::test_no_tools_key_when_no_specs_are_configured` | ➖ Extension — shipped, covered. |
| *Extension:* only the selected adapter's tools are advertised | `agent/runner.py` | `test_agent_runner.py::test_app_server_client_is_built_with_workspace_and_tracker_tools` | ➖ Extension — shipped, covered. |
| *Extension:* valid inputs execute host-side with configured adapter auth | `trackers/*::execute_agent_tool` | `test_app_server.py::test_advertised_tool_executes_host_side_and_returns_its_result`; `test_tracker_memory.py::test_add_comment_mutates_and_is_visible_to_a_later_read`; `test_tracker_github.py::test_set_project_status_resolves_the_option_and_mutates` | ➖ Extension — shipped, covered (stubbed transport for GitHub/Linear). |
| *Extension:* current normalized issue and `native_ref` available as tool context | `trackers/base.py::ToolContext`; `agent/runner.py` | `test_agent_runner.py::test_tool_execution_carries_the_current_issue_as_context`; `test_tracker_memory.py::test_get_issue_defaults_to_the_context_issue` | ➖ Extension — shipped, covered. |
| *Extension:* tracker secrets not inherited by the child | `agent/app_server.py::_child_env`; adapter `secret_environment_names` | `test_app_server.py::test_declared_secret_env_names_are_stripped_from_child`; `test_agent_runner.py::test_tracker_secret_env_names_reach_the_app_server_launcher`, `::test_an_adapter_declaring_no_secrets_strips_nothing` | ➖ Extension — shipped, covered. See [`SECURITY.md`](SECURITY.md) §4. |
| *Extension:* invalid arguments, missing auth, transport failures return structured failures | adapters | `test_tracker_memory.py::test_invalid_tool_arguments_return_structured_failures`, `::test_tool_calls_surface_injected_faults_without_raising`; `test_tracker_github.py::test_tool_errors_are_returned_not_raised` | ➖ Extension — shipped, covered. |
| *Extension:* unsupported tool names fail without stalling | `trackers/base.py::execute_agent_tool` default | `test_tracker_memory.py::test_unsupported_tool_name_returns_structured_failure`; `test_agent_events.py::test_unsupported_tool_call_returns_a_failure_result_and_does_not_raise` | ➖ Extension — shipped, covered. |

## 7. SPEC 17.6 — Observability *(Core Conformance)*

| Bullet | Module | Test | Status |
|---|---|---|---|
| Validation failures are operator-visible | `cli.py`; `orchestrator/core.py` | `test_cli.py::test_startup_failure_prints_one_clean_line_without_traceback`, `::test_startup_failure_surfaces_the_spec_error_category`; `test_orchestrator.py::test_validation_failure_skips_dispatch_but_reconciliation_already_ran` | ✅ |
| Structured logging includes issue/session context fields | `observability/logging.py` | `test_observability.py::test_bound_issue_context_appears_as_key_value`, `::test_required_context_fields_lead_the_line_in_stable_order`, `::test_bind_is_immutable_and_layered` | ✅ |
| Logging sink failures do not crash orchestration | `observability/logging.py` | `test_observability.py::test_sink_failure_does_not_crash_the_caller`, `::test_sink_failure_is_reported_through_a_remaining_sink`, `::test_persistently_failing_sink_is_disabled_after_a_threshold` | ✅ |
| Token/rate-limit aggregation remains correct across repeated updates | `agent/events.py`; `orchestrator/core.py` | `test_agent_events.py::test_monotonic_absolute_totals_are_not_double_counted`, `::test_repeated_identical_totals_credit_nothing`, `::test_decreasing_total_never_drives_the_aggregate_backwards`; `test_orchestrator.py::test_absolute_token_totals_accumulate_as_deltas`, `::test_delta_style_payloads_are_ignored_for_totals` | ✅ |
| *Extension:* status surface driven from orchestrator state, no correctness impact | `observability/status.py` | `test_observability.py::test_status_renders_running_and_retry_rows`, `::test_status_is_read_only` | ➖ Extension — shipped, covered. |
| *Extension:* humanized summaries cover key event classes without changing behavior | `observability/humanize.py` | `test_observability.py::test_every_spec_10_4_event_has_a_humanized_summary`, `::test_summaries_never_dump_payloads_or_secrets` | ➖ Extension — shipped, covered. |

## 8. SPEC 17.7 — CLI and Host Lifecycle *(Core Conformance)*

| Bullet | Module | Test | Status |
|---|---|---|---|
| CLI accepts a positional workflow path argument | `cli.py` | `test_cli.py::test_parser_accepts_positional_workflow_path`, `::test_explicit_path_is_used_verbatim` | ✅ |
| CLI uses `./WORKFLOW.md` when no path argument is provided | `cli.py` | `test_cli.py::test_parser_defaults_positional_to_none`, `::test_start_service_defaults_to_cwd_workflow` | ✅ |
| CLI errors on nonexistent explicit path or missing default | `cli.py` | `test_cli.py::test_nonexistent_explicit_path_raises_missing_workflow_file`, `::test_missing_default_workflow_raises_missing_workflow_file`, `::test_directory_at_workflow_path_is_not_accepted` | ✅ |
| CLI surfaces startup failure cleanly | `cli.py` | `test_cli.py::test_startup_failure_prints_one_clean_line_without_traceback`, `::test_non_symphony_startup_failure_is_still_surfaced_cleanly` | ✅ |
| CLI exits with success when the app starts and shuts down normally | `cli.py` | `test_cli.py::test_serve_returns_on_requested_shutdown`, `::test_run_exits_zero_on_sigint`, `::test_graceful_shutdown_stops_dispatch_before_unwinding` | ✅ |
| CLI exits nonzero when startup fails or the host exits abnormally | `cli.py` | `test_cli.py::test_startup_validation_failure_exits_nonzero`, `::test_abnormal_host_exit_exits_nonzero`, `::test_exit_codes_are_distinct_and_only_success_is_zero` | ✅ |

## 9. SPEC 17.8 — Real Integration Profile *(RECOMMENDED)*

| Bullet | Mechanism | Status |
|---|---|---|
| A real tracker smoke test runnable with valid credentials via the adapter's documented secret mechanism | `test_tracker_github.py::test_live_project_read`; the Linear equivalent; both `@pytest.mark.integration` | ⛔ **Never run.** Skipped for missing `SYMPHONY_GH_OWNER`/`SYMPHONY_GH_PROJECT` and `LINEAR_API_KEY`/`LINEAR_TEAM_KEY`. The test exists; its result is unknown. |
| Real integration tests use isolated test identifiers/workspaces and clean up tracker artifacts | test bodies + `SYMPHONY_TEST_SSH_ROOT` convention | ⛔ Not verifiable without running them. This audit did not read the integration test bodies to confirm cleanup. |
| A skipped real-integration test is reported as skipped, not silently passed | `pyproject.toml` `markers = ["integration: ..."]`; pytest skip reporting | ✅ Observed directly: the suite reports `3 skipped` with named reasons. |
| If the profile is explicitly enabled in CI/release validation, failures fail that job | — | ⛔ **Not discharged.** No CI configuration exists in this repository at the snapshot. See §10. |

## 10. Gaps, stated plainly

1. **The Codex app-server wire protocol is unverified.** Every JSON-RPC method
   and field name lives in `ProtocolNames` in `agent/app_server.py`, whose own
   header says so: only `thread/tokenUsage/updated` appears verbatim in SPEC
   13.5, and the rest follow that convention as best-effort. `codex` is not
   installed on this host, so no message this client sends has ever been accepted
   by a real app-server. Consequence: the suite can be fully green while session
   startup fails against a real Codex. Correction path is contained — run
   `codex app-server generate-json-schema`, diff, and edit that one dataclass;
   `test_app_server.py::test_whole_lifecycle_works_under_renamed_protocol_strings`
   exists to prove the rest of the module does not depend on the current names.
2. **No live tracker call has ever been made.** GitHub Projects v2 and Linear
   normalization, pagination, and error mapping are covered against stubbed
   transports. Real payload shapes, real rate-limit headers, and real GraphQL
   error bodies are untested. Run the SPEC 17.8 profile before production
   (SPEC 18.3).
3. **No CI configuration exists.** SPEC 17.8's last bullet asks that an
   explicitly-enabled integration profile fail its job on failure. There is no
   job. This is a real, undischarged gap, not a technicality.
4. **Adapter profile documents were not audited against SPEC 11.2.** Files exist
   at `docs/adapters/{memory,github,linear}.md`, and `memory`'s presence is
   asserted by a test, but this audit did not check any of them against SPEC
   11.2's seven required sections. They are owned by other authors.
5. **Coverage claims rest on test names, not test bodies.** See *Method* above.
6. **SPEC 18.2 TODO items are not implemented, and are not required.** Retry-queue
   persistence across restarts, configurable observability settings in front
   matter, and extracted semantic helper tools are all SPEC 18.2 TODOs explicitly
   excluded from conformance. SPEC 14.3 states the in-memory design is
   intentional.

## 11. SPEC 18.2 — RECOMMENDED Extensions

| Item | Status |
|---|---|
| HTTP server honors CLI `--port` over `server.port`, safe default bind, SPEC 13.7 endpoints/error semantics | ➖ Shipped. `test_http.py::test_cli_port_overrides_config_port`, `::test_default_bind_host_is_loopback`, `::test_state_returns_the_spec_shape`, `::test_unknown_issue_returns_404_with_the_error_envelope`, `::test_unsupported_method_on_state_returns_405`, `::test_refresh_returns_202_with_the_spec_fields` |
| Provider-native agent tools execute host-side without passing tracker secrets to the child | ➖ Shipped. See §6 Extension rows and [`SECURITY.md`](SECURITY.md) §4. |
| TODO: persist retry queue and session metadata across restarts | ➖ Not implemented (SPEC 14.3 makes in-memory intentional). |
| TODO: configurable observability settings in front matter | ➖ Not implemented. |
| TODO: extract common semantic helper tools only after real duplication | ➖ Not implemented, by design — SPEC 18.2 says do not preemptively replace provider-native tools with generic CRUD. |

## 12. SPEC 18.3 — Operational Validation Before Production

| Item | Status |
|---|---|
| Run the Real Integration Profile with valid credentials and network access | ⛔ Not done. See §9. |
| Verify hook execution and workflow path resolution on the target host OS/shell | ⚠️ Verified on this host (Windows 10 + Git Bash) by `test_hooks.py::test_windows_uses_a_posix_shell_when_one_is_installed`, `::test_documented_windows_cmd_fallback_actually_executes`. **Not** verified on any other target host — this is a per-deployment check by design. |
| If the HTTP server ships, verify port behavior and loopback/default bind on the target environment | ⚠️ Verified on this host by `test_http.py::test_server_binds_an_ephemeral_loopback_port_and_serves`. Per-deployment verification still required. |

---

## 13. Audit correction, 2026-07-28

An audit arm tasked with disproving the conformance claim found that **two
SPEC 18.1 items marked ✅ in this document were not met**. Both are now
implemented and covered; the corrections are recorded here rather than silently
edited above, because a traceability document that quietly revises its own
verdicts is worth less than one that shows them changing.

| Item | Was | Finding | Now |
|---|---|---|---|
| 4 — dynamic watch/reload/re-apply | ✅ | The reload reached the orchestrator's poll cadence, concurrency, and state lists, but **not the prompt** — nor codex settings, hooks, or workspace root. `AgentRunner`, `HookRunner`, and `WorkspaceManager` are built once at startup and were never handed a new config, so agents kept rendering the prompt frozen at process start. 3 of 6 SPEC 6.2 knobs re-applied. | ✅ All 8 knobs verified re-applying by a live before/after probe. `apply_config` added to the three long-lived collaborators and wired into `ServiceHost.reload_workflow`. |
| 17 — logs carry `session_id` | ✅ | `session_id=` appeared **zero** times as a log field anywhere in `src/`. The cited tests constructed the field by hand inside the test, proving the logging library renders what it is passed — not that any production path passed one. | ✅ The runner harvests `session_id` from the `session_started` payload (the only place it can learn `turn_id`) and binds it on session-lifecycle and per-turn logs. Omitted rather than guessed before it is known. |

The audit also confirmed that the caveat already recorded in §10 is stronger
than it read: the app-server suite's protocol assertions were shown to pass
identically against **deliberately nonsensical method names**, because the fake
server's method table is generated from the same `ProtocolNames` object the
client uses. The 40+ real login shells genuinely exercise framing, lifecycle,
and timeouts — but they are protocol-agnostic by construction, so the passing
suite is not evidence about wire compatibility in either direction. Item 10
remains ⚠️.

Findings against the SPEC 15 security posture are recorded separately in
`SECURITY.md` §12.
