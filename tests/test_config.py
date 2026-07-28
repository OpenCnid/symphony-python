"""Conformance tests for the typed configuration layer — SPEC 5.3, 6.1, 6.3, 6.4.

These tests exercise the SPEC 17.1 bullets owned by ``symphony.workflow.config``:
config defaults, ``tracker.kind`` support validation, ``tracker.provider``
preservation, ``$VAR`` and ``~`` resolution, ``codex.command`` preservation, and
per-state concurrency map normalization.

No sibling module is imported for behavior. The tracker adapter registry is
replaced with a locally defined fake so the suite runs whether or not
``symphony.trackers.memory`` (or any other adapter) exists yet.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from symphony.errors import ConfigValidationError, InvalidTrackerConfig
from symphony.models import Issue, WorkflowDefinition
from symphony.trackers import base as tracker_base
from symphony.trackers.base import TrackerAdapter
from symphony.workflow import config as cfgmod
from symphony.workflow.config import (
    CodexConfig,
    HookConfig,
    ServiceConfig,
    build_config,
    default_workspace_root,
    expand_value,
    validate_dispatch_config,
)

# --------------------------------------------------------------------------
# Fakes — this module's siblings are being written in parallel and must not be
# imported for behavior (see module docstring).
# --------------------------------------------------------------------------


class FakeAdapter(TrackerAdapter):
    """Stand-in adapter that documents default states (SPEC 5.3.1)."""

    kind = "fake"
    default_active_states = ("Todo", "In Progress")
    default_terminal_states = ("Done", "Canceled")

    def __init__(self, provider: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(provider, **kwargs)
        # Real adapters normalize their own provider map in place (defaults,
        # `$VAR` resolution). Simulated here so the preflight cannot get away
        # with handing over the live mapping.
        provider["applied_adapter_default"] = True
        if provider.get("reject"):
            raise InvalidTrackerConfig("provider missing required key", field="token")
        if provider.get("explode"):
            raise RuntimeError("adapter blew up in a way the spec never described")

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        return []

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        return []


class BareAdapter(TrackerAdapter):
    """Adapter profile that documents *no* default states."""

    kind = "bare"

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        return []

    async def fetch_issues_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        return []


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[TrackerAdapter]]:
    """Replace the global adapter registry for the duration of one test."""
    fake: dict[str, type[TrackerAdapter]] = {
        FakeAdapter.kind: FakeAdapter,
        BareAdapter.kind: BareAdapter,
    }
    monkeypatch.setattr(tracker_base, "_REGISTRY", fake)
    return fake


def defn(config: dict[str, Any] | None = None, *, source_path: str | None = None):
    """A ``WorkflowDefinition`` with only the front matter under test."""
    return WorkflowDefinition(
        config=config if config is not None else {},
        prompt_template="body",
        source_path=source_path,
    )


# --------------------------------------------------------------------------
# SPEC 6.4 — Core config fields cheat sheet
# --------------------------------------------------------------------------


def test_every_spec_6_4_default_applies_when_front_matter_is_empty() -> None:
    """SPEC 6.4: the full defaults table, asserted field by field."""
    cfg = build_config(defn({}))

    assert cfg.tracker_kind == ""
    assert cfg.tracker_provider == {}
    assert cfg.required_labels == ()
    assert cfg.active_states == ()
    assert cfg.terminal_states == ()
    assert cfg.poll_interval_ms == 30000
    assert cfg.workspace_root == (Path(tempfile.gettempdir()) / "symphony_workspaces").resolve()
    assert cfg.hooks.after_create is None
    assert cfg.hooks.before_run is None
    assert cfg.hooks.after_run is None
    assert cfg.hooks.before_remove is None
    assert cfg.hooks.timeout_ms == 60000
    assert cfg.max_concurrent_agents == 10
    assert cfg.max_turns == 20
    assert cfg.max_retry_backoff_ms == 300000
    assert cfg.max_concurrent_agents_by_state == {}
    assert cfg.codex.command == "codex app-server"
    assert cfg.codex.turn_timeout_ms == 3600000
    assert cfg.codex.read_timeout_ms == 5000
    assert cfg.codex.stall_timeout_ms == 300000
    # Implementation-defined by SPEC 5.3.6; pinned here so a silent change fails.
    assert cfg.codex.approval_policy == "never"
    assert cfg.codex.thread_sandbox == "danger-full-access"
    assert cfg.codex.turn_sandbox_policy == "danger-full-access"
    # Extension fields (SPEC 13.7, Appendix A) stay disabled unless configured.
    assert cfg.server_port is None
    assert cfg.ssh_hosts == ()
    assert cfg.max_concurrent_agents_per_host is None


def test_defaults_are_also_the_dataclass_defaults() -> None:
    """The SPEC 6.4 table and the record defaults cannot drift apart."""
    assert HookConfig().timeout_ms == 60000
    assert CodexConfig() == CodexConfig(
        command="codex app-server",
        approval_policy="never",
        thread_sandbox="danger-full-access",
        turn_sandbox_policy="danger-full-access",
        turn_timeout_ms=3600000,
        read_timeout_ms=5000,
        stall_timeout_ms=300000,
    )


def test_explicit_front_matter_values_win_over_defaults() -> None:
    cfg = build_config(
        defn(
            {
                "polling": {"interval_ms": 1500},
                "hooks": {"timeout_ms": 9000},
                "agent": {
                    "max_concurrent_agents": 3,
                    "max_turns": 7,
                    "max_retry_backoff_ms": 42000,
                },
                "codex": {
                    "command": "my-codex --stdio",
                    "approval_policy": "on-request",
                    "thread_sandbox": "workspace-write",
                    "turn_sandbox_policy": "read-only",
                    "turn_timeout_ms": 11,
                    "read_timeout_ms": 22,
                    "stall_timeout_ms": 33,
                },
            }
        )
    )

    assert cfg.poll_interval_ms == 1500
    assert cfg.hooks.timeout_ms == 9000
    assert cfg.max_concurrent_agents == 3
    assert cfg.max_turns == 7
    assert cfg.max_retry_backoff_ms == 42000
    assert cfg.codex == CodexConfig(
        command="my-codex --stdio",
        approval_policy="on-request",
        thread_sandbox="workspace-write",
        turn_sandbox_policy="read-only",
        turn_timeout_ms=11,
        read_timeout_ms=22,
        stall_timeout_ms=33,
    )


def test_default_workspace_root_derives_from_the_system_temp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SPEC 5.3.3: default is ``<system-temp>/symphony_workspaces``."""
    monkeypatch.setattr(cfgmod.tempfile, "gettempdir", lambda: str(tmp_path))

    assert default_workspace_root() == tmp_path / "symphony_workspaces"
    assert build_config(defn({})).workspace_root == (tmp_path / "symphony_workspaces").resolve()


def test_stall_timeout_of_zero_or_less_is_valid_and_disables_detection() -> None:
    """SPEC 5.3.6: ``stall_timeout_ms <= 0`` disables stall detection."""
    zero = build_config(defn({"codex": {"stall_timeout_ms": 0}}))
    negative = build_config(defn({"codex": {"stall_timeout_ms": -1}}))

    assert zero.codex.stall_timeout_ms == 0
    assert zero.codex.stall_detection_enabled is False
    assert negative.codex.stall_timeout_ms == -1
    assert build_config(defn({})).codex.stall_detection_enabled is True


# --------------------------------------------------------------------------
# SPEC 6.1 — ``~`` and ``$VAR`` expansion, and where it must NOT apply
# --------------------------------------------------------------------------


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A fake home directory that ``os.path.expanduser`` honors on any OS."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    return fake_home


def test_expand_value_expands_leading_tilde(home: Path) -> None:
    """SPEC 5.3.3 / 6.1: ``~`` is expanded in path values.

    Compared as paths: ``expand_value`` is a string-level expansion and keeps
    the authored separator, which ``Path`` normalizes later.
    """
    assert Path(expand_value("~/agents")) == home / "agents"
    assert Path(expand_value("~")) == home


def test_expand_value_expands_bare_and_braced_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYM_ROOT", "/srv/sym")
    monkeypatch.setenv("SYM_LEAF", "leaf")

    assert expand_value("$SYM_ROOT") == "/srv/sym"
    assert expand_value("${SYM_ROOT}/x") == "/srv/sym/x"
    assert expand_value("$SYM_ROOT/$SYM_LEAF") == "/srv/sym/leaf"


def test_expand_value_treats_an_empty_variable_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC 5.3.1: a ``$VAR`` resolving to '' is *missing*, not empty."""
    monkeypatch.setenv("SYM_EMPTY", "")

    with pytest.raises(ConfigValidationError) as exc:
        expand_value("$SYM_EMPTY/workspaces")

    assert exc.value.details["variable"] == "SYM_EMPTY"


def test_expand_value_treats_an_unset_variable_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYM_NEVER_SET", raising=False)

    with pytest.raises(ConfigValidationError):
        expand_value("${SYM_NEVER_SET}")


def test_expand_value_error_does_not_leak_the_variable_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC 15.3: validate presence of secrets without printing them."""
    monkeypatch.setenv("SYM_SECRET", "")

    with pytest.raises(ConfigValidationError) as exc:
        expand_value("$SYM_SECRET")

    rendered = repr(exc.value.to_dict())
    assert "SYM_SECRET" in rendered
    assert "variable" in exc.value.details


def test_expand_value_does_not_recursively_rescan_substituted_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value that happens to contain ``$OTHER`` is not expanded again."""
    monkeypatch.setenv("SYM_OUTER", "$SYM_INNER")
    monkeypatch.setenv("SYM_INNER", "boom")

    assert expand_value("$SYM_OUTER") == "$SYM_INNER"


def test_expand_value_joins_relative_results_onto_base_dir(tmp_path: Path) -> None:
    assert expand_value("sub/dir", base_dir=tmp_path) == str(tmp_path / "sub" / "dir")


def test_expand_value_leaves_absolute_results_alone(tmp_path: Path) -> None:
    absolute = str((tmp_path / "abs").resolve())
    assert expand_value(absolute, base_dir=tmp_path / "elsewhere") == absolute


def test_expand_value_leaves_text_without_markers_untouched() -> None:
    assert expand_value("plain/relative/path") == "plain/relative/path"
    assert expand_value("has$ but no name") == "has$ but no name"


def test_expand_value_rejects_non_string_input() -> None:
    with pytest.raises(ConfigValidationError):
        expand_value(17)  # type: ignore[arg-type]


def test_workspace_root_expands_tilde(home: Path) -> None:
    """SPEC 17.1: ``~`` path expansion works."""
    cfg = build_config(defn({"workspace": {"root": "~/agent-work"}}))
    assert cfg.workspace_root == (home / "agent-work").resolve()


def test_workspace_root_expands_a_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SPEC 17.1: ``$VAR`` resolution works for path values."""
    monkeypatch.setenv("SYM_WS", str(tmp_path / "from-env"))
    cfg = build_config(defn({"workspace": {"root": "$SYM_WS"}}))
    assert cfg.workspace_root == (tmp_path / "from-env").resolve()


def test_workspace_root_with_an_empty_variable_fails_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYM_WS", "")
    with pytest.raises(ConfigValidationError):
        build_config(defn({"workspace": {"root": "$SYM_WS"}}))


def test_relative_workspace_root_resolves_against_the_workflow_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SPEC 5.3.3 / 6.1: relative to the ``WORKFLOW.md`` dir, not the cwd."""
    workflow_dir = tmp_path / "repo"
    workflow_dir.mkdir()
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    cfg = build_config(
        defn(
            {"workspace": {"root": "./symphony_workspaces"}},
            source_path=str(workflow_dir / "WORKFLOW.md"),
        )
    )

    assert cfg.workspace_root == (workflow_dir / "symphony_workspaces").resolve()
    assert cfg.workspace_root != (elsewhere / "symphony_workspaces").resolve()


def test_relative_workspace_root_falls_back_to_cwd_without_a_source_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = build_config(defn({"workspace": {"root": "ws"}}, source_path=None))
    assert cfg.workspace_root == (tmp_path / "ws").resolve()


def test_absolute_workspace_root_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "explicit"
    cfg = build_config(
        defn({"workspace": {"root": str(root)}}, source_path=str(tmp_path / "WORKFLOW.md"))
    )
    assert cfg.workspace_root == root.resolve()
    assert cfg.workspace_root.is_absolute()


def test_codex_command_is_preserved_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC 6.1 / 17.1: shell command strings are never rewritten."""
    monkeypatch.setenv("HOME", "/somewhere")
    monkeypatch.setenv("USERPROFILE", "/somewhere")
    monkeypatch.setenv("CODEX_BIN", "/opt/codex")

    command = "$CODEX_BIN/codex app-server --config ~/.codex/config.toml"
    cfg = build_config(defn({"codex": {"command": command}}))

    assert cfg.codex.command == command


def test_hook_scripts_are_preserved_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC 6.1 / 15.4: hooks are shell source, not paths."""
    monkeypatch.setenv("HOME", "/somewhere")
    monkeypatch.setenv("USERPROFILE", "/somewhere")

    script = 'echo "workspace created: $PWD"\ncp ~/.netrc .\n'
    cfg = build_config(
        defn(
            {
                "hooks": {
                    "after_create": script,
                    "before_run": "  ",
                    "after_run": 42,
                }
            }
        )
    )

    assert cfg.hooks.after_create == script
    assert cfg.hooks.before_run is None
    assert cfg.hooks.after_run is None
    assert cfg.hooks.before_remove is None


def test_environment_variables_never_globally_override_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC 6.1: env applies only where a value explicitly references ``$VAR``."""
    for name in (
        "MAX_TURNS",
        "SYMPHONY_MAX_TURNS",
        "INTERVAL_MS",
        "POLLING_INTERVAL_MS",
        "SYMPHONY_POLL_INTERVAL_MS",
        "WORKSPACE_ROOT",
        "SYMPHONY_WORKSPACE_ROOT",
        "CODEX_COMMAND",
        "SYMPHONY_CODEX_COMMAND",
        "MAX_CONCURRENT_AGENTS",
    ):
        monkeypatch.setenv(name, "999")

    cfg = build_config(
        defn(
            {
                "polling": {"interval_ms": 1234},
                "agent": {"max_turns": 5, "max_concurrent_agents": 2},
                "codex": {"command": "yaml-codex"},
            }
        )
    )

    assert cfg.poll_interval_ms == 1234
    assert cfg.max_turns == 5
    assert cfg.max_concurrent_agents == 2
    assert cfg.codex.command == "yaml-codex"


# --------------------------------------------------------------------------
# SPEC 5.3.4 / 5.3.5 — the fatal-vs-ignored asymmetry
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -3, "abc", 1.5, True, False, [], {}, "", "  "])
def test_invalid_max_turns_fails_configuration_validation(bad: Any) -> None:
    """SPEC 5.3.5: ``max_turns`` is a positive integer; invalid values are fatal."""
    with pytest.raises(ConfigValidationError) as exc:
        build_config(defn({"agent": {"max_turns": bad}}))
    assert exc.value.details["field"] == "agent.max_turns"


@pytest.mark.parametrize("bad", [0, -1, "abc", 2.5, True, [], {}, "", "  "])
def test_invalid_hook_timeout_fails_configuration_validation(bad: Any) -> None:
    """SPEC 5.3.4: ``hooks.timeout_ms`` invalid values fail validation."""
    with pytest.raises(ConfigValidationError) as exc:
        build_config(defn({"hooks": {"timeout_ms": bad}}))
    assert exc.value.details["field"] == "hooks.timeout_ms"


def test_absent_or_null_fatal_fields_fall_back_to_defaults() -> None:
    """Absent is not invalid: only a present, unusable value is fatal."""
    cfg = build_config(defn({"agent": {"max_turns": None}, "hooks": {"timeout_ms": None}}))
    assert cfg.max_turns == 20
    assert cfg.hooks.timeout_ms == 60000


@pytest.mark.parametrize(
    ("section", "key", "bad"),
    [
        ("polling", "interval_ms", "not-a-number"),
        ("polling", "interval_ms", 0),
        ("polling", "interval_ms", -5),
        ("agent", "max_retry_backoff_ms", "nope"),
        ("agent", "max_concurrent_agents", -1),
        ("codex", "turn_timeout_ms", "x"),
        ("codex", "read_timeout_ms", 0),
    ],
)
def test_invalid_non_fatal_numbers_fall_back_to_their_default(
    section: str, key: str, bad: Any
) -> None:
    """Only ``max_turns`` and ``hooks.timeout_ms`` are fatal (SPEC 5.3.4/5.3.5)."""
    cfg = build_config(defn({section: {key: bad}}))
    defaults = build_config(defn({}))
    assert cfg.poll_interval_ms == defaults.poll_interval_ms
    assert cfg.max_retry_backoff_ms == defaults.max_retry_backoff_ms
    assert cfg.max_concurrent_agents == defaults.max_concurrent_agents
    assert cfg.codex.turn_timeout_ms == defaults.codex.turn_timeout_ms
    assert cfg.codex.read_timeout_ms == defaults.codex.read_timeout_ms


def test_zero_max_concurrent_agents_is_honored_as_a_drain_switch() -> None:
    """SPEC 5.3.5 calls this an "integer", not a positive one."""
    assert build_config(defn({"agent": {"max_concurrent_agents": 0}})).max_concurrent_agents == 0


def test_numeric_strings_coerce_for_integer_fields() -> None:
    cfg = build_config(defn({"agent": {"max_turns": "7"}, "polling": {"interval_ms": " 900 "}}))
    assert cfg.max_turns == 7
    assert cfg.poll_interval_ms == 900


# --------------------------------------------------------------------------
# SPEC 5.3.5 / 8.3 — per-state concurrency overrides
# --------------------------------------------------------------------------


def test_per_state_concurrency_keys_normalize_by_trim_and_lowercase() -> None:
    """SPEC 17.1: the override map normalizes state names."""
    cfg = build_config(
        defn({"agent": {"max_concurrent_agents_by_state": {"  In PROGRESS  ": 2, "Todo": 5}}})
    )
    assert cfg.max_concurrent_agents_by_state == {"in progress": 2, "todo": 5}


@pytest.mark.parametrize("bad", [0, -1, "abc", None, True, False, 1.5, [], {}, ""])
def test_per_state_concurrency_ignores_invalid_entries_without_failing(bad: Any) -> None:
    """SPEC 5.3.5: invalid entries are *ignored*, unlike ``max_turns``."""
    cfg = build_config(
        defn({"agent": {"max_concurrent_agents_by_state": {"todo": bad, "review": 3}}})
    )
    assert cfg.max_concurrent_agents_by_state == {"review": 3}


def test_per_state_concurrency_drops_blank_and_non_string_keys() -> None:
    cfg = build_config(
        defn({"agent": {"max_concurrent_agents_by_state": {"   ": 4, 7: 4, "done": 1}}})
    )
    assert cfg.max_concurrent_agents_by_state == {"done": 1}


def test_per_state_concurrency_ignores_a_non_map_value() -> None:
    cfg = build_config(defn({"agent": {"max_concurrent_agents_by_state": ["todo"]}}))
    assert cfg.max_concurrent_agents_by_state == {}


def test_slot_limit_for_state_prefers_the_override_then_the_global_limit() -> None:
    """SPEC 8.3: per-state override if present, otherwise the global limit."""
    cfg = build_config(
        defn(
            {
                "agent": {
                    "max_concurrent_agents": 9,
                    "max_concurrent_agents_by_state": {"in progress": 2},
                }
            }
        )
    )
    assert cfg.slot_limit_for_state("In Progress") == 2
    assert cfg.slot_limit_for_state("  IN PROGRESS ") == 2
    assert cfg.slot_limit_for_state("Todo") == 9


# --------------------------------------------------------------------------
# SPEC 5.3.1 — tracker section
# --------------------------------------------------------------------------


def test_tracker_provider_preserves_adapter_owned_keys_verbatim() -> None:
    """SPEC 5.3.1 / 17.1: core preserves unknown provider keys untouched."""
    provider = {
        "endpoint": "https://api.example.com/graphql?team=$TEAM",
        "token": "$SYM_TOKEN",
        "nested": {"scope": ["a", "b"]},
        "unknown_future_key": 1,
    }
    cfg = build_config(defn({"tracker": {"kind": "fake", "provider": provider}}))

    assert cfg.tracker_provider == provider
    assert cfg.tracker_kind == "fake"


def test_tracker_provider_is_not_expanded_by_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC 5.3.1: only the adapter knows which keys are secrets vs. URIs.

    Core must not rewrite a URI, and an unresolvable secret must not make
    building the config explode before the adapter can report it.
    """
    monkeypatch.delenv("SYM_TOKEN", raising=False)
    cfg = build_config(
        defn({"tracker": {"kind": "fake", "provider": {"token": "$SYM_TOKEN"}}})
    )
    assert cfg.tracker_provider["token"] == "$SYM_TOKEN"


def test_config_is_isolated_from_later_mutation_of_the_source_definition() -> None:
    source = {"tracker": {"kind": "fake", "provider": {"a": 1}}, "agent": {"max_turns": 3}}
    d = defn(source)
    cfg = build_config(d)

    source["agent"]["max_turns"] = 999
    source["tracker"]["provider"]["a"] = 999
    cfg.tracker_provider["a"] = 7

    assert cfg.max_turns == 3
    assert cfg.raw["agent"]["max_turns"] == 3
    assert cfg.raw["tracker"]["provider"]["a"] == 1


def test_raw_keeps_unknown_top_level_keys_for_forward_compatibility() -> None:
    """SPEC 5.3: unknown keys are ignored, not rejected."""
    cfg = build_config(defn({"future_extension": {"enabled": True}, "agent": {"max_turns": 4}}))
    assert cfg.raw["future_extension"] == {"enabled": True}
    assert cfg.max_turns == 4


def test_non_map_sections_are_treated_as_absent() -> None:
    cfg = build_config(
        defn({"polling": "every 30s", "agent": ["nope"], "tracker": 5, "codex": None})
    )
    assert cfg.poll_interval_ms == 30000
    assert cfg.max_turns == 20
    assert cfg.tracker_kind == ""
    assert cfg.codex.command == "codex app-server"


def test_required_labels_are_normalized_and_blank_entries_are_preserved() -> None:
    """SPEC 5.3.1: a blank configured label matches no issue."""
    cfg = build_config(defn({"tracker": {"required_labels": ["  Ready ", "AGENT", "  ", 7]}}))

    assert cfg.required_labels == ("ready", "agent", "")

    issue = Issue(
        id="1",
        identifier="ENG-1",
        title="t",
        state="Todo",
        dispatchable=True,
        labels=("ready", "agent"),
    )
    assert issue.has_labels(cfg.required_labels) is False


def test_required_labels_match_case_insensitively_when_none_are_blank() -> None:
    cfg = build_config(defn({"tracker": {"required_labels": ["Ready", " AGENT"]}}))
    issue = Issue(
        id="1",
        identifier="ENG-1",
        title="t",
        state="Todo",
        dispatchable=True,
        labels=("agent", "ready", "extra"),
    )
    assert issue.has_labels(cfg.required_labels) is True


def test_states_keep_provider_native_spelling_but_compare_case_insensitively() -> None:
    """SPEC 5.3.1: adapters query with native names; comparison is folded."""
    cfg = build_config(
        defn(
            {
                "tracker": {
                    "active_states": ["Todo", " In Progress "],
                    "terminal_states": ["Done", "Canceled"],
                }
            }
        )
    )

    assert cfg.active_states == ("Todo", "In Progress")
    assert cfg.terminal_states == ("Done", "Canceled")
    assert cfg.is_active("in progress") is True
    assert cfg.is_active("  TODO ") is True
    assert cfg.is_active("Done") is False
    assert cfg.is_terminal("canceled") is True
    assert cfg.is_terminal("Todo") is False


def test_states_fall_back_to_the_adapter_documented_defaults(
    registry: dict[str, type[TrackerAdapter]],
) -> None:
    """SPEC 5.3.1: REQUIRED "unless the selected adapter profile documents a default"."""
    cfg = build_config(defn({"tracker": {"kind": "fake"}}))

    assert cfg.active_states == FakeAdapter.default_active_states
    assert cfg.terminal_states == FakeAdapter.default_terminal_states


def test_configured_states_win_over_adapter_defaults(
    registry: dict[str, type[TrackerAdapter]],
) -> None:
    cfg = build_config(defn({"tracker": {"kind": "fake", "active_states": ["Backlog"]}}))

    assert cfg.active_states == ("Backlog",)
    # The omitted half still picks up the adapter's documented default.
    assert cfg.terminal_states == FakeAdapter.default_terminal_states


def test_states_stay_empty_when_the_adapter_documents_no_defaults(
    registry: dict[str, type[TrackerAdapter]],
) -> None:
    cfg = build_config(defn({"tracker": {"kind": "bare"}}))
    assert cfg.active_states == ()
    assert cfg.terminal_states == ()


def test_state_defaults_are_skipped_for_an_unregistered_kind() -> None:
    """Building config never requires an adapter module to be imported."""
    cfg = build_config(defn({"tracker": {"kind": "not-registered-anywhere"}}))
    assert cfg.active_states == ()
    assert cfg.terminal_states == ()


# --------------------------------------------------------------------------
# Extension config — SPEC 13.7 (`server.port`) and Appendix A (`worker.*`)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [(8787, 8787), (0, 0), ("8080", 8080)])
def test_server_port_is_read_when_present(raw: Any, expected: int) -> None:
    """SPEC 13.7: ``0`` requests an ephemeral port; presence enables the server."""
    assert build_config(defn({"server": {"port": raw}})).server_port == expected


@pytest.mark.parametrize("bad", [-1, 70000, "nope", None, True, 1.5])
def test_invalid_server_port_leaves_the_extension_disabled(bad: Any) -> None:
    assert build_config(defn({"server": {"port": bad}})).server_port is None


def test_ssh_worker_extension_fields_are_read() -> None:
    """SPEC Appendix A: optional remote-execution pool settings."""
    cfg = build_config(
        defn(
            {
                "worker": {
                    "ssh_hosts": [" build-1 ", "build-2", "", 7],
                    "max_concurrent_agents_per_host": 3,
                }
            }
        )
    )
    assert cfg.ssh_hosts == ("build-1", "build-2")
    assert cfg.max_concurrent_agents_per_host == 3


@pytest.mark.parametrize("bad", [0, -2, "x", None])
def test_non_positive_per_host_cap_is_treated_as_unset(bad: Any) -> None:
    cfg = build_config(defn({"worker": {"max_concurrent_agents_per_host": bad}}))
    assert cfg.max_concurrent_agents_per_host is None


# --------------------------------------------------------------------------
# SPEC 6.3 — Dispatch preflight validation
# --------------------------------------------------------------------------


def make_cfg(**overrides: Any) -> ServiceConfig:
    """A valid-by-default ``ServiceConfig`` for preflight tests."""
    base = build_config(defn({"tracker": {"kind": "fake", "provider": {"token": "t"}}}))
    return replace(base, **overrides) if overrides else base


def test_preflight_accepts_a_valid_config(registry: dict[str, type[TrackerAdapter]]) -> None:
    assert validate_dispatch_config(make_cfg()) is None


def test_preflight_rejects_a_missing_tracker_kind(
    registry: dict[str, type[TrackerAdapter]],
) -> None:
    """SPEC 6.3: ``tracker.kind`` is present and supported."""
    with pytest.raises(ConfigValidationError) as exc:
        validate_dispatch_config(make_cfg(tracker_kind=""))
    assert exc.value.details["field"] == "tracker.kind"


def test_preflight_rejects_an_unsupported_tracker_kind(
    registry: dict[str, type[TrackerAdapter]],
) -> None:
    """SPEC 17.1: kind validation enforces an implementation-supported adapter."""
    with pytest.raises(ConfigValidationError) as exc:
        validate_dispatch_config(make_cfg(tracker_kind="nope"))

    assert exc.value.details["kind"] == "nope"
    assert exc.value.details["supported"] == ["bare", "fake"]


@pytest.mark.parametrize("command", ["", "   "])
def test_preflight_rejects_an_empty_codex_command(
    registry: dict[str, type[TrackerAdapter]], command: str
) -> None:
    """SPEC 6.3: ``codex.command`` is present and non-empty."""
    cfg = make_cfg(codex=CodexConfig(command=command))
    with pytest.raises(ConfigValidationError) as exc:
        validate_dispatch_config(cfg)
    assert exc.value.details["field"] == "codex.command"


def test_a_blank_codex_command_in_front_matter_survives_to_preflight(
    registry: dict[str, type[TrackerAdapter]],
) -> None:
    """A present-but-blank command must not be silently healed to the default."""
    cfg = build_config(defn({"tracker": {"kind": "fake"}, "codex": {"command": "   "}}))
    assert cfg.codex.command == ""
    with pytest.raises(ConfigValidationError):
        validate_dispatch_config(cfg)


def test_preflight_reports_provider_rejection_from_the_adapter(
    registry: dict[str, type[TrackerAdapter]],
) -> None:
    """SPEC 6.3: the selected adapter must accept ``tracker.provider``."""
    cfg = make_cfg(tracker_provider={"reject": True})

    with pytest.raises(ConfigValidationError) as exc:
        validate_dispatch_config(cfg)

    assert exc.value.details["field"] == "tracker.provider"
    assert exc.value.details["cause_category"] == "invalid_tracker_config"
    assert isinstance(exc.value.__cause__, InvalidTrackerConfig)


def test_preflight_converts_an_unexpected_adapter_error_into_a_typed_one(
    registry: dict[str, type[TrackerAdapter]],
) -> None:
    """SPEC 6.2: an invalid config must not crash the service."""
    cfg = make_cfg(tracker_provider={"explode": True})

    with pytest.raises(ConfigValidationError) as exc:
        validate_dispatch_config(cfg)

    assert exc.value.details["cause_type"] == "RuntimeError"
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_preflight_does_not_hand_the_adapter_the_live_provider_mapping(
    registry: dict[str, type[TrackerAdapter]],
) -> None:
    """A preflight construction must not be able to mutate live config.

    Preflight runs on *every* dispatch tick (SPEC 6.3), so an adapter that
    normalizes its provider map in place would otherwise accumulate edits into
    the effective configuration.
    """
    cfg = make_cfg(tracker_provider={"token": "t"})

    validate_dispatch_config(cfg)
    validate_dispatch_config(cfg)

    assert cfg.tracker_provider == {"token": "t"}
    assert "applied_adapter_default" not in cfg.tracker_provider


def test_preflight_error_is_json_safe(registry: dict[str, type[TrackerAdapter]]) -> None:
    """SPEC 13.7 API and the RLM surface both render errors via ``to_dict``."""
    with pytest.raises(ConfigValidationError) as exc:
        validate_dispatch_config(make_cfg(tracker_kind="nope"))

    payload = exc.value.to_dict()
    assert payload["category"] == "config_validation_error"
    assert json.loads(json.dumps(payload))["details"]["kind"] == "nope"


# --------------------------------------------------------------------------
# End-to-end shape against the repository's own WORKFLOW.md front matter
# --------------------------------------------------------------------------


def test_repository_workflow_front_matter_builds(tmp_path: Path) -> None:
    """The shipped WORKFLOW.md front matter resolves to the expected config."""
    front_matter = {
        "tracker": {
            "kind": "memory",
            "provider": {"seed": []},
            "required_labels": [],
            "active_states": ["Todo", "In Progress"],
            "terminal_states": ["Done", "Canceled"],
        },
        "polling": {"interval_ms": 30000},
        "workspace": {"root": "./symphony_workspaces"},
        "hooks": {
            "timeout_ms": 60000,
            "after_create": 'echo "workspace created: $PWD"\n',
            "before_run": 'echo "starting attempt in $PWD"\n',
        },
        "agent": {
            "max_concurrent_agents": 4,
            "max_turns": 20,
            "max_retry_backoff_ms": 300000,
            "max_concurrent_agents_by_state": {"in progress": 2},
        },
        "codex": {
            "command": "codex app-server",
            "turn_timeout_ms": 3600000,
            "read_timeout_ms": 5000,
            "stall_timeout_ms": 300000,
        },
        "server": {"port": 8787},
    }
    cfg = build_config(defn(front_matter, source_path=str(tmp_path / "WORKFLOW.md")))

    assert cfg.tracker_kind == "memory"
    assert cfg.workspace_root == (tmp_path / "symphony_workspaces").resolve()
    assert cfg.max_concurrent_agents == 4
    assert cfg.slot_limit_for_state("In Progress") == 2
    assert cfg.slot_limit_for_state("Todo") == 4
    assert cfg.server_port == 8787
    assert cfg.hooks.after_create is not None and "$PWD" in cfg.hooks.after_create
    assert cfg.codex.command == "codex app-server"
    assert cfg.is_active("todo") and cfg.is_terminal("DONE")


def test_service_config_is_frozen() -> None:
    """House rule: config records are immutable value objects."""
    cfg = build_config(defn({}))
    with pytest.raises(FrozenInstanceError):
        cfg.max_turns = 1  # type: ignore[misc]
