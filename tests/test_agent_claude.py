"""Conformance tests for the Claude Code backend (SPEC 10, 13.5, 15.3, 16.5).

The real ``claude`` binary IS available on this host, but it costs tokens and
takes 30-120s per invocation, so exactly one test — the last in this file —
uses it, behind ``@pytest.mark.integration``. Everything else drives a
**scripted fake subprocess**: a real ``python`` child replaying recorded
``stream-json`` lines from ``docs/claude-protocol.md``, launched through the
``spawn`` constructor seam. That keeps real asyncio stream framing, real pipe
EOF, real exit codes, real cwd and real child environment in the loop while
costing nothing.

Every wire shape used below is copied from ``docs/claude-protocol.md``, which
records what ``claude 2.1.214`` actually printed.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from symphony.agent.base import (
    DEFAULT_BACKEND,
    AgentBackendSpec,
    CodingAgentClient,
    CodingAgentSession,
    backend_kinds,
    backend_spec,
    build_agent_client,
    register_backend,
)
from symphony.agent.claude import (
    DEFAULT_PERMISSION_MODE,
    PERMISSION_MODES,
    ClaudeCodeClient,
    ClaudeConfig,
    ClaudeSession,
    deterministic_session_uuid,
    resolve_claude,
)
from symphony.agent.events import extract_rate_limits, extract_token_totals
from symphony.errors import (
    CodexNotFound,
    ConfigValidationError,
    InvalidWorkspaceCwd,
    PortExit,
    ResponseTimeout,
    TurnCancelled,
    TurnFailed,
    TurnTimeout,
)
from symphony.models import session_id as compose_session_id

# Kept as a belt-and-braces import. It used to be load-bearing: `_ensure_loaded`
# short-circuited on a non-empty registry, so importing one backend directly
# hid the other permanently. That is fixed (a `_LOADED` flag now gates the
# import instead of dict emptiness) and pinned by
# `test_registry_survives_a_direct_backend_import`, which proves it in a fresh
# interpreter where this line cannot mask the failure.
importlib.import_module("symphony.agent.app_server")


# ---------------------------------------------------------------------------
# Scripted fake `claude`. Config-driven, so scenarios stay data, not code.
# ---------------------------------------------------------------------------

FAKE_CLAUDE = r'''
import json, os, sys, time

cfg = json.load(open(sys.argv[1], encoding="utf-8"))

report = cfg.get("report")
if report:
    with open(report, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "cwd": os.getcwd(),
                "env_present": sorted(n for n in cfg.get("watch_env", []) if n in os.environ),
            },
            fh,
        )


def emit(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


for act in cfg.get("actions", []):
    if "delay_ms" in act:
        time.sleep(act["delay_ms"] / 1000.0)
    if "stderr" in act:
        sys.stderr.write(act["stderr"] + "\n")
        sys.stderr.flush()
    if "raw" in act:
        emit(act["raw"])
    if "send" in act:
        emit(json.dumps(act["send"]))
    if "exit" in act:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(act["exit"])
'''


# ---------------------------------------------------------------------------
# Recorded event shapes (docs/claude-protocol.md §2)
# ---------------------------------------------------------------------------

CLI_SESSION = "11111111-2222-3333-4444-555555555555"


def init_event(**over: Any) -> dict[str, Any]:
    event = {
        "type": "system",
        "subtype": "init",
        "cwd": "/workspace",
        "session_id": CLI_SESSION,
        "tools": ["Read", "Write", "Bash"],
        "mcp_servers": [],
        "model": "claude-opus-4-8",
        "permissionMode": "bypassPermissions",
        "claude_code_version": "2.1.214",
        "capabilities": ["interrupt_receipt_v1", "msg_lifecycle_v1"],
        "apiKeySource": "none",
        "output_style": "default",
        "uuid": "uuid-init-1",
    }
    event.update(over)
    return event


def assistant_text(text: str = "working on it", **over: Any) -> dict[str, Any]:
    event = {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "model": "claude-opus-4-8",
            "role": "assistant",
            "stop_reason": None,
            # Per-message usage: docs/claude-protocol.md §3.2 says this MUST NOT
            # reach the SPEC 13.5 aggregate.
            "usage": {
                "input_tokens": 999_001,
                "output_tokens": 999_002,
                "cache_read_input_tokens": 999_003,
            },
            "content": [{"type": "text", "text": text}],
        },
        "parent_tool_use_id": None,
        "session_id": CLI_SESSION,
        "uuid": "uuid-assistant-1",
    }
    event.update(over)
    return event


def assistant_tool_use(name: str = "Write") -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "hmm",
                },
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": name,
                    "input": {"file_path": "a.txt", "content": "x"},
                    "caller": {"type": "direct"},
                },
            ],
        },
        "session_id": CLI_SESSION,
        "uuid": "uuid-assistant-2",
    }


def tool_result_event(tool_use_id: str = "toolu_01") -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "File created successfully",
                }
            ]
        },
        "session_id": CLI_SESSION,
    }


RATE_LIMIT_INFO = {
    "status": "allowed",
    "resetsAt": 1785274800,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
    "overageDisabledReason": "org_level_disabled_until",
    "isUsingOverage": False,
}


def rate_limit_event(**over: Any) -> dict[str, Any]:
    info = dict(RATE_LIMIT_INFO)
    info.update(over)
    return {
        "type": "rate_limit_event",
        "rate_limit_info": info,
        "session_id": CLI_SESSION,
        "uuid": "uuid-rate-1",
    }


USAGE_TURN_1 = {
    "input_tokens": 18,
    "output_tokens": 302,
    "cache_creation_input_tokens": 29_344,
    "cache_read_input_tokens": 28_974,
}
USAGE_TURN_2 = {
    "input_tokens": 10,
    "output_tokens": 100,
    "cache_creation_input_tokens": 7,
    "cache_read_input_tokens": 5,
}


def result_event(**over: Any) -> dict[str, Any]:
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "duration_ms": 7388,
        "duration_api_ms": 8909,
        "ttft_ms": 5132,
        "num_turns": 2,
        "result": "DONE",
        "stop_reason": "end_turn",
        "session_id": CLI_SESSION,
        "total_cost_usd": 0.0637264,
        "usage": dict(USAGE_TURN_1),
        "modelUsage": {},
        "permission_denials": [],
        "terminal_reason": None,
        "uuid": "uuid-result-1",
    }
    event.update(over)
    return event


def happy_turn(*extra: dict[str, Any], **result_over: Any) -> list[dict[str, Any]]:
    """Actions for one complete, successful turn."""
    return [
        {"send": init_event()},
        *[{"send": event} for event in extra],
        {"send": result_event(**result_over)},
    ]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def has_seq(argv: list[str], *wanted: str) -> bool:
    """Whether ``wanted`` appears as a contiguous run inside ``argv``."""
    n = len(wanted)
    return any(argv[i : i + n] == list(wanted) for i in range(len(argv) - n + 1))


def value_of(argv: list[str], flag: str) -> str:
    assert flag in argv, f"{flag!r} not in {argv}"
    return argv[argv.index(flag) + 1]


class Harness:
    """One client wired to a queue of scripted fake subprocesses."""

    def __init__(
        self,
        client: ClaudeCodeClient,
        events: list[Any],
        spawns: list[dict[str, Any]],
        reports: list[Path],
        workspace: Path,
    ) -> None:
        self.client = client
        self.events = events
        self.spawns = spawns
        self.reports = reports
        self.workspace = workspace

    @property
    def names(self) -> list[str]:
        return [e.event for e in self.events]

    def of(self, name: str) -> list[Any]:
        return [e for e in self.events if e.event == name]

    def one(self, name: str) -> Any:
        found = self.of(name)
        assert found, f"no {name!r} event in {self.names}"
        assert len(found) == 1, f"expected one {name!r}, saw {len(found)}"
        return found[0]

    def argv(self, turn: int = 0) -> list[str]:
        assert len(self.spawns) > turn, f"only {len(self.spawns)} spawn(s)"
        return list(self.spawns[turn]["argv"])

    def spawn_kwargs(self, turn: int = 0) -> dict[str, Any]:
        return dict(self.spawns[turn]["kwargs"])

    def report(self, turn: int = 0) -> dict[str, Any]:
        raw = self.reports[turn].read_text(encoding="utf-8")
        return json.loads(raw)


@pytest.fixture
async def build(tmp_path: Path):
    made: list[ClaudeCodeClient] = []
    counter = [0]
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")

    def _build(*turns: list[dict[str, Any]], **kwargs: Any) -> Harness:
        counter[0] += 1
        n = counter[0]

        workspace = kwargs.pop("workspace", None)
        if workspace is None:
            workspace = tmp_path / f"ws_{n}"
            workspace.mkdir(exist_ok=True)
        watch_env = list(kwargs.pop("watch_env", ()))

        confs: list[Path] = []
        reports: list[Path] = []
        for i, actions in enumerate(turns):
            report = tmp_path / f"report_{n}_{i}.json"
            conf = tmp_path / f"turn_{n}_{i}.json"
            conf.write_text(
                json.dumps(
                    {
                        "actions": list(actions),
                        "report": report.as_posix(),
                        "watch_env": watch_env,
                    }
                ),
                encoding="utf-8",
            )
            confs.append(conf)
            reports.append(report)

        spawns: list[dict[str, Any]] = []

        async def spawn(*argv: str, **spawn_kwargs: Any) -> Any:
            index = len(spawns)
            spawns.append({"argv": list(argv), "kwargs": dict(spawn_kwargs)})
            assert index < len(confs), f"turn {index + 1} spawned; only {len(confs)} scripted"
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                str(script),
                str(confs[index]),
                stdin=spawn_kwargs["stdin"],
                stdout=spawn_kwargs["stdout"],
                stderr=spawn_kwargs["stderr"],
                cwd=spawn_kwargs["cwd"],
                env=spawn_kwargs["env"],
                limit=spawn_kwargs["limit"],
            )

        cfg = kwargs.pop("cfg", None)
        if cfg is None:
            cfg = ClaudeConfig(command=sys.executable, **kwargs.pop("cfg_kwargs", {}))

        events: list[Any] = []
        client = ClaudeCodeClient(
            cfg,
            workspace=workspace,
            on_event=kwargs.pop("on_event", events.append),
            spawn=kwargs.pop("spawn", spawn),
            **kwargs,
        )
        made.append(client)
        return Harness(client, events, spawns, reports, workspace)

    yield _build

    for client in made:
        await client.stop()


# ---------------------------------------------------------------------------
# docs/claude-protocol.md §3.1 — the `is_error` trap. THE assertion.
# ---------------------------------------------------------------------------


async def test_result_with_success_subtype_but_is_error_true_fails_the_turn(build):
    """`subtype: "success"` with `is_error: true` is a FAILURE (protocol §3.1).

    This is the auth-failure shape observed from the real binary. Code that
    keys on `subtype` marks it successful and the orchestrator schedules a
    1-second continuation retry against a permanently broken credential.
    """
    h = build(
        happy_turn(
            subtype="success",
            is_error=True,
            result="Not logged in · Please run /login",
            terminal_reason="api_error",
            api_error_status=401,
        )
    )
    session = await h.client.start_session()

    with pytest.raises(TurnFailed) as excinfo:
        await h.client.run_turn(session, "do the thing")

    assert "Not logged in" in str(excinfo.value)
    assert excinfo.value.details["terminal_reason"] == "api_error"
    assert excinfo.value.details["api_error_status"] == 401
    assert "turn_failed" in h.names
    assert "turn_completed" not in h.names


async def test_result_with_is_error_false_completes_the_turn(build):
    """The other half of the trap: a genuine success must NOT fail.

    Without this, a driver that ignores `is_error` entirely is indistinguishable
    from one that honours it.
    """
    h = build(happy_turn(subtype="success", is_error=False))
    session = await h.client.start_session()

    await h.client.run_turn(session, "do the thing")

    assert "turn_failed" not in h.names
    completed = h.one("turn_completed")
    assert completed.payload["result"] == "DONE"
    assert completed.payload["stop_reason"] == "end_turn"
    assert completed.payload["num_turns"] == 2
    assert completed.payload["duration_ms"] == 7388


async def test_is_error_true_wins_over_error_subtype_too(build):
    """`is_error` is the authority regardless of what `subtype` says."""
    h = build(happy_turn(subtype="error_during_execution", is_error=True, result="boom"))
    session = await h.client.start_session()

    with pytest.raises(TurnFailed):
        await h.client.run_turn(session, "go")

    # The failing payload still carries accounting, so a failed turn is billed.
    failed = h.one("turn_failed")
    assert failed.payload["total_token_usage"]["total_tokens"] > 0


async def test_missing_is_error_key_is_treated_as_success(build):
    """A `result` with no `is_error` at all must not fail the turn."""
    event = result_event()
    event.pop("is_error")
    h = build([{"send": init_event()}, {"send": event}])
    session = await h.client.start_session()

    await h.client.run_turn(session, "go")

    assert "turn_completed" in h.names


# ---------------------------------------------------------------------------
# SPEC 10.1 / protocol §1 — argv construction
# ---------------------------------------------------------------------------


async def test_first_turn_preassigns_session_id_and_requires_verbose(build):
    h = build(happy_turn())
    session = await h.client.start_session()
    preassigned = session.thread_id
    await h.client.run_turn(session, "implement the feature")

    argv = h.argv(0)
    assert argv[0] == resolve_claude(sys.executable)
    assert has_seq(argv, "--print", "implement the feature")
    assert has_seq(argv, "--output-format", "stream-json")
    # Without --verbose the stream is suppressed entirely (protocol §1).
    assert "--verbose" in argv
    assert has_seq(argv, "--session-id", preassigned)
    assert uuid.UUID(preassigned)  # --session-id must be a valid UUID
    assert "--resume" not in argv
    assert has_seq(argv, "--permission-mode", DEFAULT_PERMISSION_MODE)


async def test_continuation_turn_resumes_and_drops_session_id(build):
    """SPEC 7.1: turn two reaches the same conversation without resending.

    The fake echoes back the pre-assigned id on `system/init`, which is what the
    real CLI does when `--session-id` is honoured, so this pins the whole
    round trip: pre-assign, learn, resume.
    """
    preassigned = deterministic_session_uuid("ENG-777")
    h = build(
        [{"send": init_event(session_id=preassigned)}, {"send": result_event()}],
        [
            {"send": init_event(session_id=preassigned)},
            {"send": result_event(usage=dict(USAGE_TURN_2))},
        ],
        issue_identifier="ENG-777",
    )
    session = await h.client.start_session()
    assert session.thread_id == preassigned

    await h.client.run_turn(session, "first")
    await h.client.run_turn(session, "second")

    first, second = h.argv(0), h.argv(1)
    assert has_seq(first, "--session-id", preassigned)
    assert "--resume" not in first

    assert "--session-id" not in second
    assert has_seq(second, "--resume", preassigned)
    assert has_seq(second, "--print", "second")
    # The task prompt is not resent; only the continuation prompt is.
    assert "first" not in second
    # Same conversation identity across both invocations.
    assert value_of(first, "--session-id") == value_of(second, "--resume")


async def test_init_reported_session_id_overrides_the_preassigned_one(build):
    """The id the CLI reports is what a continuation must resume."""
    reported = "99999999-8888-7777-6666-555555555555"
    h = build(
        [{"send": init_event(session_id=reported)}, {"send": result_event()}],
        happy_turn(),
    )
    session = await h.client.start_session()
    preassigned = session.thread_id
    assert preassigned != reported

    await h.client.run_turn(session, "first")
    assert session.thread_id == reported

    await h.client.run_turn(session, "second")
    assert has_seq(h.argv(1), "--resume", reported)


async def test_fork_session_only_applies_on_a_resume(build):
    h = build(
        happy_turn(),
        happy_turn(),
        cfg=ClaudeConfig(command=sys.executable, fork_session=True),
    )
    session = await h.client.start_session()

    await h.client.run_turn(session, "first")
    assert "--fork-session" not in h.argv(0)

    await h.client.run_turn(session, "second")
    assert "--fork-session" in h.argv(1)


async def test_every_configured_option_lands_as_the_flag_the_cli_accepts(build):
    """Each flag name is exactly what `claude 2.1.214` parses (protocol §1)."""
    cfg = ClaudeConfig(
        command=sys.executable,
        model="haiku",
        permission_mode="acceptEdits",
        allowed_tools=("Bash(git *)", "Read(src/**)"),
        disallowed_tools=("WebFetch", "mcp__evil__*"),
        max_turns=12,
        max_budget_usd=1.5,
        append_system_prompt="be terse",
        system_prompt="you are a bot",
        add_dirs=("/extra/one", "/extra/two"),
        mcp_config=("a.json", "b.json"),
        settings="settings.json",
        agents="agents.json",
        effort="high",
        bare=True,
        session_persistence=False,
        extra_args=("--debug", "api"),
    )
    h = build(happy_turn(), cfg=cfg)
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")
    argv = h.argv(0)

    assert has_seq(argv, "--model", "haiku")
    assert has_seq(argv, "--permission-mode", "acceptEdits")
    # Comma-joined, not repeated flags.
    assert has_seq(argv, "--allowedTools", "Bash(git *),Read(src/**)")
    assert has_seq(argv, "--disallowedTools", "WebFetch,mcp__evil__*")
    assert has_seq(argv, "--max-turns", "12")
    assert has_seq(argv, "--max-budget-usd", "1.5")
    assert has_seq(argv, "--append-system-prompt", "be terse")
    assert has_seq(argv, "--system-prompt", "you are a bot")
    assert has_seq(argv, "--effort", "high")
    # Repeated flags, one per value, order preserved.
    assert has_seq(argv, "--add-dir", "/extra/one")
    assert has_seq(argv, "--add-dir", "/extra/two")
    assert has_seq(argv, "--mcp-config", "a.json")
    assert has_seq(argv, "--mcp-config", "b.json")
    assert has_seq(argv, "--settings", "settings.json")
    assert has_seq(argv, "--agents", "agents.json")
    assert "--bare" in argv
    assert "--no-session-persistence" in argv
    assert argv[-2:] == ["--debug", "api"]


async def test_unset_options_emit_no_flag_at_all(build):
    """A default config must not smuggle empty values onto the command line."""
    h = build(happy_turn())
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")
    argv = h.argv(0)

    for absent in (
        "--model",
        "--allowedTools",
        "--disallowedTools",
        "--max-turns",
        "--max-budget-usd",
        "--append-system-prompt",
        "--system-prompt",
        "--effort",
        "--add-dir",
        "--mcp-config",
        "--settings",
        "--agents",
        "--bare",
        "--no-session-persistence",
    ):
        assert absent not in argv, f"{absent} leaked into a default invocation"
    assert "" not in argv


# ---------------------------------------------------------------------------
# SPEC 9.5 Invariant 1 — the workspace is the cwd, not a flag
# ---------------------------------------------------------------------------


async def test_workspace_is_the_child_cwd_and_never_a_flag(build):
    """Protocol §1: there is no `--cwd`; the launcher sets it, which is what
    makes SPEC 9.5 Invariant 1 enforceable. Verified from inside the child."""
    h = build(happy_turn())
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    argv = h.argv(0)
    assert "--cwd" not in argv
    assert str(h.workspace) not in argv
    assert h.workspace.as_posix() not in argv

    assert h.spawn_kwargs(0)["cwd"] == str(h.workspace)
    # The child actually ran there.
    assert Path(h.report(0)["cwd"]).resolve() == h.workspace.resolve()


async def test_stdin_is_devnull_and_line_limit_is_raised(build):
    """`--print` never reads stdin; a 10 MB line limit keeps a huge tool_use
    payload from raising LimitOverrunError mid-turn."""
    h = build(happy_turn())
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    kwargs = h.spawn_kwargs(0)
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE
    assert kwargs["limit"] == 10 * 1024 * 1024


@pytest.mark.parametrize("bad", ["relative/path", "__does_not_exist__"])
async def test_non_absolute_or_missing_workspace_is_rejected(build, tmp_path, bad):
    workspace = Path(bad) if bad == "relative/path" else tmp_path / bad
    h = build(happy_turn(), workspace=workspace)

    with pytest.raises(InvalidWorkspaceCwd):
        await h.client.start_session()
    assert h.spawns == []


async def test_workspace_removed_between_turns_is_caught_before_launch(build, tmp_path):
    workspace = tmp_path / "vanishing"
    workspace.mkdir()
    h = build(happy_turn(), workspace=workspace)
    session = await h.client.start_session()

    workspace.rmdir()
    with pytest.raises(InvalidWorkspaceCwd):
        await h.client.run_turn(session, "go")
    assert h.spawns == []


# ---------------------------------------------------------------------------
# SPEC 15.3 — declared secrets are not inherited by the child
# ---------------------------------------------------------------------------


async def test_declared_secret_env_names_are_absent_from_the_child(build, monkeypatch):
    """Claude Code is launched directly, not through `bash -lc`, so nothing
    re-sources a stripped credential (docs/SECURITY.md §12.2)."""
    monkeypatch.setenv("SYMPHONY_TEST_TRACKER_TOKEN", "super-secret")
    monkeypatch.setenv("SYMPHONY_TEST_API_KEY", "also-secret")
    monkeypatch.setenv("SYMPHONY_TEST_KEEPME", "harmless")

    h = build(
        happy_turn(),
        secret_env_names=["SYMPHONY_TEST_TRACKER_TOKEN", "SYMPHONY_TEST_API_KEY"],
        watch_env=[
            "SYMPHONY_TEST_TRACKER_TOKEN",
            "SYMPHONY_TEST_API_KEY",
            "SYMPHONY_TEST_KEEPME",
        ],
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    env = h.spawn_kwargs(0)["env"]
    assert "SYMPHONY_TEST_TRACKER_TOKEN" not in env
    assert "SYMPHONY_TEST_API_KEY" not in env
    assert env["SYMPHONY_TEST_KEEPME"] == "harmless"
    assert "super-secret" not in json.dumps(env)

    # And the process that actually ran could not see them either.
    assert h.report(0)["env_present"] == ["SYMPHONY_TEST_KEEPME"]

    # The parent's own environment is untouched.
    assert os.environ["SYMPHONY_TEST_TRACKER_TOKEN"] == "super-secret"


async def test_undeclared_secret_names_are_a_no_op(build, monkeypatch):
    monkeypatch.setenv("SYMPHONY_TEST_KEEPME", "harmless")
    h = build(
        happy_turn(),
        secret_env_names=["NOT_SET_ANYWHERE"],
        watch_env=["SYMPHONY_TEST_KEEPME"],
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    assert h.report(0)["env_present"] == ["SYMPHONY_TEST_KEEPME"]


# ---------------------------------------------------------------------------
# SPEC 13.5 — token accounting is cumulative across turns
# ---------------------------------------------------------------------------


async def test_two_turns_sum_token_usage_instead_of_replacing_it(build):
    """Claude reports per-turn usage; the session must sum into thread totals.

    Verified by running the real `events.extract_token_totals` selector over the
    emitted event rather than by eyeballing the dict.
    """
    h = build(
        happy_turn(usage=dict(USAGE_TURN_1)),
        happy_turn(usage=dict(USAGE_TURN_2)),
    )
    session = await h.client.start_session()

    await h.client.run_turn(session, "first")
    first = h.of("turn_completed")[0]
    assert extract_token_totals(first.payload) == (18 + 28_974, 302, 18 + 302 + 28_974)

    await h.client.run_turn(session, "second")
    second = h.of("turn_completed")[1]

    # Sums, not replacements. A driver that overwrote would report turn 2 only.
    exp_input = (18 + 10) + (28_974 + 5)
    exp_output = 302 + 100
    exp_total = (18 + 10) + (302 + 100) + (28_974 + 5)
    assert extract_token_totals(second.payload) == (exp_input, exp_output, exp_total)
    assert second.payload["total_token_usage"] == {
        "input_tokens": exp_input,
        "output_tokens": exp_output,
        "total_tokens": exp_total,
    }
    # The AgentEvent surface the orchestrator actually consumes agrees.
    assert second.token_totals() == (exp_input, exp_output, exp_total)

    assert session.input_tokens == 28
    assert session.output_tokens == 402
    assert session.cache_read_tokens == 28_979
    assert session.cache_creation_tokens == 29_351
    assert session.total_tokens == exp_total
    assert second.payload["cache"] == {
        "read_input_tokens": 28_979,
        "creation_input_tokens": 29_351,
    }


async def test_per_message_assistant_usage_never_reaches_the_aggregate(build):
    """Protocol §3.2: only `result.usage` is reported upward."""
    h = build(happy_turn(assistant_text(), tool_result_event()))
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    for event in h.events:
        if event.event == "turn_completed":
            continue
        assert event.token_totals() is None, f"{event.event} leaked token totals"

    # The 999_00x sentinel numbers from assistant.message.usage are nowhere.
    totals = h.one("turn_completed").token_totals()
    assert totals == (18 + 28_974, 302, 18 + 302 + 28_974)


async def test_malformed_usage_fields_do_not_corrupt_the_totals(build):
    h = build(
        happy_turn(
            usage={
                "input_tokens": -5,
                "output_tokens": True,
                "cache_read_input_tokens": "lots",
                "cache_creation_input_tokens": 3.9,
            }
        )
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    assert session.input_tokens == 0
    assert session.output_tokens == 0
    assert session.cache_read_tokens == 0
    assert session.cache_creation_tokens == 3
    assert h.one("turn_completed").payload["total_token_usage"]["total_tokens"] == 0


async def test_result_without_a_usage_map_is_survivable(build):
    event = result_event()
    event.pop("usage")
    h = build([{"send": init_event()}, {"send": event}])
    session = await h.client.start_session()

    await h.client.run_turn(session, "go")

    assert h.one("turn_completed").payload["total_token_usage"]["total_tokens"] == 0


# ---------------------------------------------------------------------------
# SPEC 13.5 — cost accumulates across turns
# ---------------------------------------------------------------------------


async def test_cost_accumulates_across_turns(build):
    h = build(
        happy_turn(total_cost_usd=0.0637264),
        happy_turn(total_cost_usd=0.02, usage=dict(USAGE_TURN_2)),
    )
    session = await h.client.start_session()

    await h.client.run_turn(session, "first")
    assert h.of("turn_completed")[0].payload["cost"] == {
        "turn_usd": 0.0637264,
        "session_usd": 0.063726,
    }

    await h.client.run_turn(session, "second")
    second = h.of("turn_completed")[1].payload["cost"]
    assert second["turn_usd"] == 0.02
    assert second["session_usd"] == pytest.approx(0.083726)
    assert session.total_cost_usd == pytest.approx(0.0837264)


async def test_missing_or_bogus_cost_leaves_the_session_total_alone(build):
    h = build(happy_turn(total_cost_usd=None), happy_turn(total_cost_usd=True))
    session = await h.client.start_session()

    await h.client.run_turn(session, "first")
    await h.client.run_turn(session, "second")

    assert session.total_cost_usd == 0.0
    assert h.of("turn_completed")[0].payload["cost"]["turn_usd"] is None


# ---------------------------------------------------------------------------
# SPEC 13.3 / 13.5 — rate limits
# ---------------------------------------------------------------------------


async def test_rate_limit_event_reaches_the_session_and_the_payloads(build):
    """`rate_limit_event` is a top-level type, not a `system` subtype."""
    h = build(happy_turn(rate_limit_event()))
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    assert session.last_rate_limits == RATE_LIMIT_INFO

    notes = [e for e in h.of("notification") if "rate_limits" in e.payload]
    assert len(notes) == 1
    assert notes[0].payload["rate_limits"]["rateLimitType"] == "five_hour"
    # resetsAt is a Unix epoch in *seconds* and must survive verbatim.
    assert notes[0].payload["rate_limits"]["resetsAt"] == 1785274800

    completed = h.one("turn_completed")
    assert completed.payload["rate_limits"] == RATE_LIMIT_INFO
    # The real SPEC 13.5 extractor finds it.
    assert extract_rate_limits(completed.payload) == RATE_LIMIT_INFO
    assert completed.rate_limits() == RATE_LIMIT_INFO


async def test_latest_rate_limit_snapshot_wins_and_survives_into_the_next_turn(build):
    h = build(
        happy_turn(rate_limit_event(status="allowed"), rate_limit_event(status="rejected")),
        happy_turn(usage=dict(USAGE_TURN_2)),
    )
    session = await h.client.start_session()

    await h.client.run_turn(session, "first")
    assert session.last_rate_limits is not None
    assert session.last_rate_limits["status"] == "rejected"

    await h.client.run_turn(session, "second")
    assert h.of("turn_completed")[1].payload["rate_limits"]["status"] == "rejected"


async def test_turn_without_rate_limits_omits_the_key_entirely(build):
    h = build(happy_turn())
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    assert "rate_limits" not in h.one("turn_completed").payload
    assert session.last_rate_limits is None


async def test_rate_limit_event_with_a_non_mapping_info_is_ignored(build):
    h = build(happy_turn({"type": "rate_limit_event", "rate_limit_info": "nope"}))
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    assert session.last_rate_limits is None
    assert "turn_completed" in h.names


# ---------------------------------------------------------------------------
# SPEC 10.4 — event translation
# ---------------------------------------------------------------------------


async def test_session_started_carries_the_spec_4_2_session_id(build):
    h = build(happy_turn())
    session = await h.client.start_session()
    await h.client.run_turn(session, "go", title="Fix the parser")

    started = h.one("session_started")
    assert started.payload["thread_id"] == CLI_SESSION
    assert started.payload["turn_id"] == "uuid-init-1"
    assert started.payload["session_id"] == compose_session_id(CLI_SESSION, "uuid-init-1")
    assert started.payload["turn_number"] == 1
    assert started.payload["title"] == "Fix the parser"
    assert started.payload["model"] == "claude-opus-4-8"
    assert started.payload["permission_mode"] == "bypassPermissions"
    assert started.payload["agent_version"] == "2.1.214"
    assert started.payload["tools"] == ["Read", "Write", "Bash"]


async def test_init_without_a_uuid_falls_back_to_a_turn_scoped_id(build):
    event = init_event()
    event.pop("uuid")
    h = build([{"send": event}, {"send": result_event()}])
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    assert h.one("session_started").payload["turn_id"] == "turn-1"


async def test_assistant_text_and_tool_use_and_tool_result_become_notifications(build):
    h = build(
        happy_turn(assistant_text("hello there"), assistant_tool_use("Write"), tool_result_event())
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    summaries = [e.payload.get("summary") for e in h.of("notification")]
    assert "hello there" in summaries
    assert "tool: Write" in summaries
    assert "tool result" in summaries

    tool_note = next(e for e in h.of("notification") if e.payload.get("tool"))
    assert tool_note.payload["tool"] == "Write"
    result_note = next(e for e in h.of("notification") if e.payload.get("summary") == "tool result")
    assert result_note.payload["tool_use_id"] == "toolu_01"


async def test_long_assistant_text_is_clipped(build):
    h = build(happy_turn(assistant_text("x" * 1000)))
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    summary = next(e.payload["summary"] for e in h.of("notification") if e.payload.get("summary"))
    assert len(summary) == 400
    assert summary.endswith("…")


async def test_assistant_error_key_emits_turn_ended_with_error(build):
    h = build(happy_turn(assistant_text(error="authentication_failed")))
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    assert h.one("turn_ended_with_error").payload["error"] == "authentication_failed"


async def test_system_subtype_routing(build):
    h = build(
        happy_turn(
            {"type": "system", "subtype": "thinking_tokens", "tokens": 512},
            {
                "type": "system",
                "subtype": "post_turn_summary",
                "summarizes_uuid": "u1",
                "status_category": "in_progress",
                "status_detail": "Editing src/main.py",
                "needs_action": "",
            },
            {"type": "system", "subtype": "brand_new_subtype"},
            {"type": "control_response", "response": {}},
        )
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    # thinking_tokens is deliberately silent — it fires repeatedly.
    assert all(e.payload.get("subtype") != "thinking_tokens" for e in h.events)

    summary = next(e for e in h.of("notification") if e.payload.get("status"))
    assert summary.payload["summary"] == "Editing src/main.py"
    assert summary.payload["status"] == "in_progress"
    assert summary.payload["needs_action"] == ""

    subtypes = [e.payload.get("subtype") for e in h.of("notification")]
    assert "brand_new_subtype" in subtypes

    assert h.one("other_message").payload["type"] == "control_response"


async def test_permission_denials_are_counted_across_turns(build):
    h = build(
        happy_turn(permission_denials=[{"tool": "Bash"}]),
        happy_turn(
            permission_denials=[{"tool": "Write"}, {"tool": "Edit"}],
            usage=dict(USAGE_TURN_2),
        ),
    )
    session = await h.client.start_session()

    await h.client.run_turn(session, "first")
    assert h.of("turn_completed")[0].payload["permission_denials"] == 1

    await h.client.run_turn(session, "second")
    assert h.of("turn_completed")[1].payload["permission_denials"] == 3
    assert len(session.permission_denials) == 3


async def test_emitted_events_carry_the_child_pid(build):
    h = build(happy_turn())
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    pids = {e.codex_app_server_pid for e in h.events}
    assert len(pids) == 1
    assert pids.pop().isdigit()


async def test_an_exploding_observer_cannot_break_the_turn(build):
    """SPEC 14.2 observer isolation."""
    seen: list[str] = []

    def boom(event: Any) -> None:
        seen.append(event.event)
        raise RuntimeError("observer is broken")

    h = build(happy_turn(assistant_text()), on_event=boom)
    session = await h.client.start_session()

    await h.client.run_turn(session, "go")
    assert "turn_completed" in seen


async def test_a_client_without_an_observer_still_runs(build):
    h = build(happy_turn(), on_event=None)
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")
    assert session.output_tokens == 302


# ---------------------------------------------------------------------------
# Stream robustness
# ---------------------------------------------------------------------------


async def test_a_non_json_line_emits_malformed_without_killing_the_turn(build):
    h = build(
        [
            {"raw": "Warning: an update is available"},
            {"send": init_event()},
            {"raw": ""},
            {"raw": "[1, 2, 3]"},
            {"raw": '"a bare string"'},
            {"send": result_event()},
        ]
    )
    session = await h.client.start_session()

    await h.client.run_turn(session, "go")

    malformed = h.one("malformed")
    assert malformed.payload["line"] == "Warning: an update is available"
    # Valid JSON that is not an object is dropped silently, not reported twice.
    assert "turn_completed" in h.names
    assert session.output_tokens == 302


async def test_a_malformed_line_is_clipped_to_400_characters(build):
    noise = "!" * 1000
    h = build([{"raw": noise}, {"send": init_event()}, {"send": result_event()}])
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    line = h.one("malformed").payload["line"]
    assert len(line) == 400
    assert line == noise[:400]


async def test_stderr_is_captured_out_of_band_and_bounded(build):
    h = build(
        [
            {"stderr": "diagnostic line"},
            {"send": init_event()},
            {"send": result_event()},
            {"delay_ms": 120},
        ]
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "go")

    # Nothing from stderr may be parsed as a protocol event.
    assert "malformed" not in h.names
    assert "turn_completed" in h.names


# ---------------------------------------------------------------------------
# SPEC 10.6 — subprocess exit mapping
# ---------------------------------------------------------------------------


async def test_mid_turn_exit_maps_to_port_exit_with_stderr_tail(build):
    h = build(
        [
            {"stderr": "fatal: could not reach api"},
            {"send": init_event()},
            {"send": assistant_text()},
            {"delay_ms": 200},
            {"exit": 1},
        ]
    )
    session = await h.client.start_session()

    with pytest.raises(PortExit) as excinfo:
        await h.client.run_turn(session, "go")

    assert excinfo.value.details["returncode"] == 1
    assert "fatal: could not reach api" in excinfo.value.details["stderr_tail"]
    assert "turn_completed" not in h.names


async def test_exit_143_maps_to_turn_cancelled_not_port_exit(build):
    """Protocol §3.4: 143 is SIGTERM — a cancellation, not a crash."""
    h = build([{"send": init_event()}, {"delay_ms": 50}, {"exit": 143}])
    session = await h.client.start_session()

    with pytest.raises(TurnCancelled) as excinfo:
        await h.client.run_turn(session, "go")

    assert excinfo.value.details["returncode"] == 143
    assert not isinstance(excinfo.value, PortExit)


async def test_immediate_exit_before_any_output_is_a_port_exit(build):
    h = build([{"stderr": "Not logged in"}, {"delay_ms": 200}, {"exit": 2}])
    session = await h.client.start_session()

    with pytest.raises(PortExit) as excinfo:
        await h.client.run_turn(session, "go")

    assert excinfo.value.details["returncode"] == 2
    assert session.started is False


async def test_a_missing_executable_raises_codex_not_found(build):
    async def exploding_spawn(*argv: str, **kwargs: Any) -> Any:
        raise FileNotFoundError(argv[0])

    h = build(happy_turn(), spawn=exploding_spawn)
    session = await h.client.start_session()

    with pytest.raises(CodexNotFound) as excinfo:
        await h.client.run_turn(session, "go")
    assert excinfo.value.details["command"] == sys.executable


# ---------------------------------------------------------------------------
# SPEC 10.5 / 10.6 — timeouts. A turn MUST NOT block indefinitely.
# ---------------------------------------------------------------------------


async def test_read_timeout_bounds_startup_silence(build):
    h = build(
        [{"delay_ms": 30_000}, {"send": init_event()}],
        cfg=ClaudeConfig(command=sys.executable, read_timeout_ms=150, turn_timeout_ms=30_000),
    )
    session = await h.client.start_session()

    with pytest.raises(ResponseTimeout) as excinfo:
        await h.client.run_turn(session, "go")

    assert excinfo.value.details["timeout_ms"] == 150
    assert session.started is False


async def test_turn_timeout_bounds_silence_after_output_has_begun(build):
    """The deadline switches once the stream is alive: a startup budget of 150ms
    must not fire on a turn that has already produced events."""
    h = build(
        [
            {"send": init_event()},
            {"send": assistant_text()},
            {"delay_ms": 30_000},
            {"send": result_event()},
        ],
        cfg=ClaudeConfig(command=sys.executable, read_timeout_ms=150, turn_timeout_ms=400),
    )
    session = await h.client.start_session()

    with pytest.raises(TurnTimeout) as excinfo:
        await h.client.run_turn(session, "go")

    assert excinfo.value.details["timeout_ms"] == 400
    assert session.started is True
    # It got far enough to report the session before timing out.
    assert "session_started" in h.names


async def test_the_startup_budget_stops_applying_once_output_has_begun(build):
    """`read_timeout_ms` bounds startup and *only* startup.

    A short startup budget with a generous silence budget must let a turn that
    has already spoken think for longer than the startup budget. If the read
    deadline kept applying after the first line, real work would be killed at
    200ms.
    """
    h = build(
        [{"send": init_event()}, {"delay_ms": 700}, {"send": result_event()}],
        cfg=ClaudeConfig(command=sys.executable, read_timeout_ms=200, turn_timeout_ms=8_000),
    )
    session = await h.client.start_session()

    await h.client.run_turn(session, "go")

    assert "turn_completed" in h.names
    assert session.output_tokens == 302


async def test_output_resets_the_silence_deadline(build):
    """A turn that keeps talking must not be killed by turn_timeout_ms."""
    chatter: list[dict[str, Any]] = [{"send": init_event()}]
    for _ in range(6):
        chatter.append({"delay_ms": 60})
        chatter.append({"send": assistant_text()})
    chatter.append({"send": result_event()})

    h = build(
        chatter,
        cfg=ClaudeConfig(command=sys.executable, read_timeout_ms=5_000, turn_timeout_ms=400),
    )
    session = await h.client.start_session()

    await h.client.run_turn(session, "go")
    assert "turn_completed" in h.names


async def test_a_timed_out_turn_kills_the_child(build):
    h = build(
        [{"delay_ms": 30_000}],
        cfg=ClaudeConfig(command=sys.executable, read_timeout_ms=150),
    )
    session = await h.client.start_session()

    with pytest.raises(ResponseTimeout):
        await h.client.run_turn(session, "go")

    assert h.client.pid is None


# ---------------------------------------------------------------------------
# SPEC 16.5 — lifecycle
# ---------------------------------------------------------------------------


async def test_stop_is_idempotent_before_during_and_after(build):
    h = build(happy_turn())
    session = await h.client.start_session()

    await h.client.stop()
    await h.client.run_turn(session, "go")
    await h.client.stop()
    await h.client.stop()
    await session.stop()
    await session.stop()

    assert h.client.pid is None


async def test_no_process_starts_until_the_first_turn(build):
    h = build(happy_turn())
    await h.client.start_session()

    assert h.spawns == []
    assert h.client.pid is None
    assert h.events == []


async def test_turn_count_increments_even_on_a_failing_turn(build):
    h = build(happy_turn(is_error=True), happy_turn(usage=dict(USAGE_TURN_2)))
    session = await h.client.start_session()

    with pytest.raises(TurnFailed):
        await h.client.run_turn(session, "first")
    assert session.turn_count == 1

    await h.client.run_turn(session, "second")
    assert session.turn_count == 2


# ---------------------------------------------------------------------------
# Session identity
# ---------------------------------------------------------------------------


def test_deterministic_session_uuid_is_stable_and_identifier_scoped():
    a1 = deterministic_session_uuid("ENG-123")
    a2 = deterministic_session_uuid("ENG-123")
    b = deterministic_session_uuid("ENG-124")

    assert a1 == a2, "the same issue must resume the same conversation"
    assert a1 != b, "different issues must not collide onto one conversation"
    # --session-id requires a valid UUID.
    assert uuid.UUID(a1).version == 5
    assert str(uuid.UUID(a1)) == a1
    # Namespaced, so it cannot collide with another uuid5 consumer.
    assert a1 != str(uuid.uuid5(uuid.NAMESPACE_URL, "ENG-123"))


async def test_start_session_derives_the_thread_id_from_the_issue_identifier(build):
    h = build(happy_turn(), issue_identifier="ENG-123")
    session = await h.client.start_session()

    assert session.thread_id == deterministic_session_uuid("ENG-123")
    assert session.started is False
    assert session.turn_count == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"issue_identifier": None},
        {"issue_identifier": "ENG-123", "cfg": ClaudeConfig(command=sys.executable,
                                                            deterministic_session_id=False)},
    ],
)
async def test_start_session_falls_back_to_a_random_uuid(build, kwargs):
    h1 = build(happy_turn(), **kwargs)
    h2 = build(happy_turn(), **kwargs)

    s1 = await h1.client.start_session()
    s2 = await h2.client.start_session()

    assert s1.thread_id != s2.thread_id
    assert uuid.UUID(s1.thread_id).version == 4


def test_session_credit_and_totals_are_defensive():
    session = ClaudeSession(thread_id="t")
    session.credit({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2}, 0.5)
    session.credit({"input_tokens": "x", "output_tokens": None}, -1.0)
    session.credit({}, True)

    assert session.input_tokens == 10
    assert session.output_tokens == 5
    assert session.cache_read_tokens == 2
    assert session.total_tokens == 17
    assert session.total_cost_usd == 0.5
    assert session.absolute_usage() == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
    }


def test_claude_session_satisfies_the_backend_session_protocol():
    assert isinstance(ClaudeSession(thread_id="t"), CodingAgentSession)


# ---------------------------------------------------------------------------
# Executable resolution
# ---------------------------------------------------------------------------


def test_resolve_claude_returns_an_absolute_path_unchanged():
    assert resolve_claude(sys.executable) == sys.executable


def test_resolve_claude_uses_only_the_head_token():
    assert resolve_claude(f"{sys.executable} --flag") == sys.executable


def test_resolve_claude_raises_codex_not_found_for_an_unknown_command():
    with pytest.raises(CodexNotFound) as excinfo:
        resolve_claude("definitely-not-a-real-binary-9f2c")
    assert excinfo.value.details["command"] == "definitely-not-a-real-binary-9f2c"


def test_resolve_claude_defaults_to_claude_for_a_blank_command(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        "symphony.agent.claude.shutil.which", lambda name: seen.append(name) or None
    )
    with pytest.raises(CodexNotFound):
        resolve_claude("   ")
    assert seen == ["claude"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_defaults_match_the_documented_posture():
    cfg = ClaudeConfig.from_mapping(None)

    assert cfg.command == "claude"
    assert cfg.permission_mode == DEFAULT_PERMISSION_MODE == "bypassPermissions"
    assert cfg.turn_timeout_ms == 3_600_000
    assert cfg.read_timeout_ms == 60_000
    assert cfg.stall_timeout_ms == 300_000
    assert cfg.stall_detection_enabled is True
    assert cfg.deterministic_session_id is True
    assert cfg.session_persistence is True
    assert cfg.bare is False


def test_config_coercion_rejects_junk_without_failing_the_workflow():
    cfg = ClaudeConfig.from_mapping(
        {
            "command": "  ",
            "permission_mode": "yolo",
            "allowed_tools": "Bash",
            "disallowed_tools": ["WebFetch", 7, "  "],
            "max_turns": 0,
            "max_budget_usd": -3,
            "turn_timeout_ms": True,
            "read_timeout_ms": "60000",
            "stall_timeout_ms": 0,
            "unknown_future_key": "ignored",
        }
    )

    assert cfg.command == "claude"
    assert cfg.permission_mode == DEFAULT_PERMISSION_MODE
    assert cfg.allowed_tools == ("Bash",)
    assert cfg.disallowed_tools == ("WebFetch",)
    assert cfg.max_turns is None
    assert cfg.max_budget_usd is None
    # `True` is not a timeout.
    assert cfg.turn_timeout_ms == 3_600_000
    assert cfg.read_timeout_ms == 60_000
    assert cfg.stall_timeout_ms == 0
    assert cfg.stall_detection_enabled is False


@pytest.mark.parametrize("mode", PERMISSION_MODES)
def test_every_documented_permission_mode_round_trips(mode):
    assert ClaudeConfig.from_mapping({"permission_mode": mode}).permission_mode == mode


# ---------------------------------------------------------------------------
# symphony.agent.base — the backend registry
# ---------------------------------------------------------------------------


def test_registry_resolves_both_shipped_backends():
    """Both bundled backends resolve, each owning its own front-matter block.

    NOTE: `symphony.agent.app_server` is imported explicitly at the top of this
    module. `base._ensure_loaded()` short-circuits as soon as `_BACKENDS` is
    non-empty, so importing `symphony.agent.claude` first (which this file does)
    leaves `codex` unregistered. See the defect report.
    """
    kinds = backend_kinds()
    assert "claude" in kinds
    assert "codex" in kinds
    assert kinds == sorted(kinds)

    claude = backend_spec("claude")
    assert claude.kind == "claude"
    assert claude.config_key == "claude"

    codex = backend_spec("codex")
    assert codex.kind == "codex"
    assert codex.config_key == "codex"

    assert DEFAULT_BACKEND == "codex"


def test_unknown_kind_raises_a_typed_config_error():
    with pytest.raises(ConfigValidationError) as excinfo:
        backend_spec("gemini")

    assert excinfo.value.details["kind"] == "gemini"
    assert "claude" in excinfo.value.details["supported"]
    assert "gemini" in str(excinfo.value)


def test_build_agent_client_constructs_the_claude_backend(tmp_path):
    client = build_agent_client(
        "claude",
        {"model": "haiku", "permission_mode": "acceptEdits"},
        workspace=tmp_path,
        secret_env_names=("TOKEN",),
        approval_decider=object(),  # Claude has no approval hook; must be dropped.
        issue_identifier="ENG-9",
    )

    assert isinstance(client, ClaudeCodeClient)
    assert isinstance(client, CodingAgentClient)
    assert isinstance(client.cfg, ClaudeConfig)
    assert client.cfg.model == "haiku"
    assert client.cfg.permission_mode == "acceptEdits"
    assert client.workspace == tmp_path


def test_build_agent_client_accepts_an_already_typed_config(tmp_path):
    cfg = ClaudeConfig(command="claude", effort="high")
    client = build_agent_client("claude", cfg, workspace=tmp_path)

    assert client.cfg is cfg


def test_build_agent_client_tolerates_a_missing_config_block(tmp_path):
    client = build_agent_client("claude", None, workspace=tmp_path)

    assert client.cfg == ClaudeConfig.from_mapping(None)


def test_build_agent_client_rejects_an_unknown_kind(tmp_path):
    with pytest.raises(ConfigValidationError):
        build_agent_client("nope", {}, workspace=tmp_path)


def test_register_backend_requires_a_kind():
    with pytest.raises(ValueError):
        register_backend(AgentBackendSpec(kind="", config_key="x", factory=object))


def test_registering_a_third_party_backend_round_trips():
    spec = AgentBackendSpec(
        kind="_test_backend", config_key="_test", factory=lambda cfg, **kw: ("built", cfg, kw)
    )
    try:
        register_backend(spec)
        assert backend_spec("_test_backend") is spec
        assert "_test_backend" in backend_kinds()
        built = build_agent_client("_test_backend", {"a": 1}, workspace=Path("."))
        assert built[0] == "built"
        assert built[2]["workspace"] == Path(".")
    finally:
        from symphony.agent import base as base_mod

        base_mod._BACKENDS.pop("_test_backend", None)

    assert "_test_backend" not in backend_kinds()


# ---------------------------------------------------------------------------
# SPEC 17.8 — Real Integration Profile. Costs real tokens, so it is opt-in:
#   SYMPHONY_CLAUDE_INTEGRATION=1 pytest tests/test_agent_claude.py -m integration
# The marker alone is not enough — this project has no `-m "not integration"`
# in addopts, so an unguarded test would bill every default suite run. The
# env-var gate matches test_tracker_github.py and test_ssh_worker.py.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_real_claude_binary_end_to_end(tmp_path):  # pragma: no cover - costs tokens
    """Drive the actual `claude` CLI once, cheaply, to re-verify the contract.

    Mirrors the re-verification command in docs/claude-protocol.md. A fresh
    issue identifier per run keeps `--session-id` from colliding with a
    persisted session from an earlier run.
    """
    import shutil

    if not os.environ.get("SYMPHONY_CLAUDE_INTEGRATION"):
        pytest.skip("SYMPHONY_CLAUDE_INTEGRATION not set; this test spends real tokens")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not on PATH")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    events: list[Any] = []
    cfg = ClaudeConfig(
        command="claude",
        model="haiku",
        max_turns=1,
        read_timeout_ms=180_000,
        turn_timeout_ms=180_000,
        session_persistence=False,
    )
    client = ClaudeCodeClient(
        cfg,
        workspace=workspace,
        on_event=events.append,
        issue_identifier=f"symphony-integration-{uuid.uuid4()}",
    )

    session = await client.start_session()
    try:
        await client.run_turn(session, "Reply with exactly: OK", title="integration probe")
    finally:
        await client.stop()

    names = [e.event for e in events]
    assert "turn_failed" not in names, [e.payload for e in events if e.event == "turn_failed"]

    started = next(e for e in events if e.event == "session_started")
    assert started.payload["agent_version"]
    assert started.payload["thread_id"] == session.thread_id

    completed = next(e for e in events if e.event == "turn_completed")
    assert "OK" in str(completed.payload["result"])
    totals = extract_token_totals(completed.payload)
    assert totals is not None
    assert totals[0] > 0 and totals[1] > 0
    assert completed.payload["cost"]["session_usd"] >= 0


# --------------------------------------------------------------------------
# Regressions for two defects found while testing this backend
# --------------------------------------------------------------------------


def test_registry_survives_a_direct_backend_import() -> None:
    """Importing one backend must not hide the other (base.py `_LOADED`).

    Registration is an *import side effect*, so a guard that tested
    ``if _BACKENDS:`` short-circuited on a non-empty but incomplete registry.
    That was production-reachable: ``workflow/config.py`` imports
    ``symphony.agent.claude`` directly, so a ``codex`` workflow resolved after
    that point failed with a misleading "unsupported agent.kind 'codex'".

    Run in a fresh interpreter on purpose — this module already imports both
    backends, so an in-process assertion could not fail.
    """
    probe = (
        "import symphony.agent.claude;"
        "from symphony.agent.base import backend_kinds, backend_spec;"
        "print(','.join(backend_kinds()));"
        "backend_spec('codex')"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert out.returncode == 0, f"backend_spec('codex') failed: {out.stderr[-400:]}"
    assert out.stdout.strip() == "claude,codex"


async def test_a_pre_init_banner_does_not_buy_the_turn_budget(tmp_path: Path) -> None:
    """The startup budget applies until `system/init`, not until any line.

    `claude` can print an update banner or a login warning before the init
    event. Switching the deadline on "a line arrived" rather than on
    `session.started` desynchronized the wait from the error: a hang after such
    a line waited the full turn budget — an hour by default — and then raised
    `ResponseTimeout` blaming `read_timeout_ms`.
    """
    script = "import time; print('WARNING: update available', flush=True); time.sleep(30)"
    cfg = ClaudeConfig.from_mapping({"read_timeout_ms": 300, "turn_timeout_ms": 20_000})

    async def spawn(*_args: object, **kw: object) -> object:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdin=kw.get("stdin"),
            stdout=kw.get("stdout"),
            stderr=kw.get("stderr"),
            cwd=kw.get("cwd"),
            env=kw.get("env"),
            limit=kw.get("limit", 65536),
        )

    client = ClaudeCodeClient(cfg, workspace=tmp_path, on_event=lambda _e: None, spawn=spawn)
    session = await client.start_session()

    started = time.monotonic()
    with pytest.raises(ResponseTimeout) as caught:
        await client.run_turn(session, "x")
    waited_ms = (time.monotonic() - started) * 1000
    await client.stop()

    # The reported budget and the observed wait must agree. Before the fix this
    # reported 300 and waited ~20000.
    assert caught.value.details["timeout_ms"] == 300
    assert waited_ms < 5_000, f"waited {waited_ms:.0f}ms against a 300ms startup budget"
