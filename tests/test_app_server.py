"""Conformance tests for the Codex app-server client (SPEC 10.1-10.3, 10.6, 17.5).

The real ``codex`` binary is not available here and MUST NOT be required. Every
transport test drives a scripted fake subprocess that speaks the same
newline-delimited JSON-RPC framing, launched through the same real
``bash -lc <codex.command>`` path the production code uses.

Sibling modules (``symphony.agent.events``, ``symphony.workflow.config``,
``symphony.agent.approvals``) are written by other authors and may not exist
yet, so this file injects fakes matching the signatures in ``CONTRACTS.md``.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Sibling injection: CONTRACTS.md section 3 shape for symphony.agent.events.
# Installed only when the real module is absent, so assertions below stay valid
# once the owning author lands it.
# ---------------------------------------------------------------------------
if importlib.util.find_spec("symphony.agent.events") is None:  # pragma: no cover

    @dataclass(frozen=True, slots=True)
    class StubAgentEvent:
        event: str
        timestamp: datetime
        codex_app_server_pid: str | None = None
        usage: dict[str, Any] | None = None
        payload: dict[str, Any] = field(default_factory=dict)

    _stub = types.ModuleType("symphony.agent.events")
    _stub.AgentEvent = StubAgentEvent  # type: ignore[attr-defined]
    sys.modules["symphony.agent.events"] = _stub

from symphony.agent import app_server as mod
from symphony.agent.app_server import (
    PROTOCOL,
    ApprovalDecision,
    AppServerClient,
    AppServerSession,
    ProtocolNames,
    default_approval_decision,
)
from symphony.errors import (
    AgentError,
    CodexNotFound,
    InvalidWorkspaceCwd,
    PortExit,
    ResponseError,
    ResponseTimeout,
    TurnCancelled,
    TurnFailed,
    TurnInputRequired,
    TurnTimeout,
)
from symphony.models import session_id
from symphony.trackers.base import ToolResult, ToolSpec

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="SPEC 10.1 requires bash -lc")


# ---------------------------------------------------------------------------
# CodexConfig fake (CONTRACTS.md symphony.workflow.config.CodexConfig)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeCodexConfig:
    command: str
    approval_policy: str = "on-request"
    thread_sandbox: str = "workspace-write"
    turn_sandbox_policy: str = "workspace-write-net-off"
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000


# ---------------------------------------------------------------------------
# Scripted fake app-server. Config-driven so scenarios stay data, not code.
# ---------------------------------------------------------------------------

FAKE_SERVER = r'''
import json, os, sys, threading, time

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
_wlock = threading.Lock()
_cv = threading.Condition()
_responses = set()


def out_obj(obj):
    out_raw(json.dumps(obj))


def out_raw(text):
    with _wlock:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def record(msg):
    path = cfg.get("record")
    if not path:
        return
    with _wlock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg) + "\n")


def run_actions(actions):
    for act in actions:
        if "delay_ms" in act:
            time.sleep(act["delay_ms"] / 1000.0)
        if "stderr" in act:
            sys.stderr.write(act["stderr"] + "\n")
            sys.stderr.flush()
        if "raw" in act:
            out_raw(act["raw"])
        if "raw_size" in act:
            out_raw("x" * act["raw_size"])
        if "await_response" in act:
            target = act["await_response"]
            with _cv:
                _cv.wait_for(lambda: target in _responses, timeout=15)
        if "send" in act:
            repeat = act.get("repeat", 1)
            for i in range(repeat):
                if i:
                    time.sleep(act.get("every_ms", 0) / 1000.0)
                out_obj(act["send"])
        if "exit" in act:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(act["exit"])


for line in cfg.get("stderr", []):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
for raw in cfg.get("raw_prelude", []):
    out_raw(raw)
if cfg.get("report_cwd"):
    out_obj({"jsonrpc": "2.0", "method": "test/cwd", "params": {"cwd": os.getcwd()}})
if cfg.get("report_env"):
    out_obj({"jsonrpc": "2.0", "method": "test/env",
             "params": {"names": [n for n in cfg["report_env"] if n in os.environ]}})
if cfg.get("exit_immediately") is not None:
    sys.exit(cfg["exit_immediately"])

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    record(msg)
    method = msg.get("method")
    if method is None:
        with _cv:
            _responses.add(msg.get("id"))
            _cv.notify_all()
        continue
    plan = cfg.get("methods", {}).get(method)
    if plan is None:
        if "id" in msg:
            out_obj({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        continue
    if "exit" in plan:
        sys.stdout.flush()
        os._exit(plan["exit"])
    if "error" in plan:
        if "id" in msg:
            out_obj({"jsonrpc": "2.0", "id": msg["id"], "error": plan["error"]})
    elif not plan.get("silent") and "id" in msg:
        out_obj({"jsonrpc": "2.0", "id": msg["id"], "result": plan.get("result", {})})
    if plan.get("then"):
        threading.Thread(target=run_actions, args=(plan["then"],), daemon=True).start()
'''


def notif(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params or {}}


def request(req_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


def scenario(
    *,
    p: ProtocolNames = PROTOCOL,
    thread_id: str = "th_abc",
    turn_id: str = "tu_001",
    turn_then: list[dict[str, Any]] | None = None,
    initialize: dict[str, Any] | None = None,
    new_thread: dict[str, Any] | None = None,
    start_turn: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A working three-step startup plus one completing turn."""
    complete = [{"delay_ms": 5, "send": notif(p.turn_completed, {p.f_turn_id: turn_id})}]
    cfg: dict[str, Any] = {
        "methods": {
            p.initialize: initialize or {"result": {"userAgent": "fake-codex/0"}},
            p.new_thread: new_thread or {"result": {p.f_thread_id: thread_id}},
            p.start_turn: start_turn
            or {
                "result": {p.f_turn_id: turn_id},
                "then": turn_then if turn_then is not None else complete,
            },
        }
    }
    cfg.update(extra)
    return cfg


class Harness:
    """One client wired to one scripted fake subprocess."""

    def __init__(self, client: AppServerClient, events: list[Any], record: Path) -> None:
        self.client = client
        self.events = events
        self.record = record

    @property
    def names(self) -> list[str]:
        return [e.event for e in self.events]

    def of(self, name: str) -> list[Any]:
        return [e for e in self.events if e.event == name]

    def one(self, name: str) -> Any:
        found = self.of(name)
        assert found, f"no {name!r} event in {self.names}"
        return found[0]

    def sent(self) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        text = self.record.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in text if line.strip()]

    def sent_methods(self) -> list[str]:
        return [m["method"] for m in self.sent() if m.get("method")]

    def sent_params(self, method: str) -> dict[str, Any]:
        for msg in self.sent():
            if msg.get("method") == method:
                return msg.get("params") or {}
        raise AssertionError(f"{method!r} was never sent; saw {self.sent_methods()}")

    def responses(self) -> list[dict[str, Any]]:
        return [m for m in self.sent() if m.get("method") is None]


@pytest.fixture
async def build(tmp_path: Path):
    made: list[AppServerClient] = []
    counter = [0]

    def _build(cfg_dict: dict[str, Any], **kwargs: Any) -> Harness:
        counter[0] += 1
        n = counter[0]
        script = tmp_path / f"fake_server_{n}.py"
        script.write_text(FAKE_SERVER, encoding="utf-8")
        record = tmp_path / f"received_{n}.jsonl"
        cfg_dict = dict(cfg_dict)
        cfg_dict.setdefault("record", record.as_posix())
        conf = tmp_path / f"scenario_{n}.json"
        conf.write_text(json.dumps(cfg_dict), encoding="utf-8")

        command = kwargs.pop(
            "command",
            f'"{Path(sys.executable).as_posix()}" -u "{script.as_posix()}" "{conf.as_posix()}"',
        )
        codex = kwargs.pop("codex", None) or FakeCodexConfig(command=command)
        workspace = kwargs.pop("workspace", None)
        if workspace is None:
            workspace = tmp_path / f"ws_{n}"
            workspace.mkdir(exist_ok=True)

        events: list[Any] = []
        kwargs.setdefault("tool_specs", [])
        kwargs.setdefault("tool_executor", lambda name, args: ToolResult.success({}))
        client = AppServerClient(
            codex,
            workspace=workspace,
            on_event=events.append,
            **kwargs,
        )
        made.append(client)
        return Harness(client, events, record)

    yield _build

    for client in made:
        await client.stop()


# ---------------------------------------------------------------------------
# SPEC 10.1 — launch contract
# ---------------------------------------------------------------------------


@needs_bash
async def test_launch_argv_is_bash_lc_with_workspace_cwd(build, tmp_path):
    """SPEC 10.1 / 17.5: bash -lc <codex.command>, cwd = workspace."""
    seen: dict[str, Any] = {}

    async def recording_spawn(argv, cwd, env):
        seen["argv"] = list(argv)
        seen["cwd"] = cwd
        raise FileNotFoundError("stop before launching")

    h = build(scenario(), spawn=recording_spawn)
    with pytest.raises(CodexNotFound):
        await h.client.start_session()

    argv = seen["argv"]
    assert len(argv) == 3
    assert Path(argv[0]).stem.lower() == "bash"
    assert argv[1:] == ["-lc", h.client.cfg.command]
    assert Path(seen["cwd"]) == h.client.workspace
    assert Path(seen["cwd"]).is_absolute()


@needs_bash
def test_bash_is_resolved_through_path_not_createprocess_search_order():
    """CONTRACTS rule 7: on Windows a bare ``bash`` silently means WSL.

    ``CreateProcess`` searches System32 before PATH, so the bare name resolves
    to WSL's bash on any host with WSL installed. WSL cannot see a ``D:\\...``
    workspace, so that launch would break SPEC 9.5 Invariant 1 invisibly.
    """
    resolved = mod.resolve_bash()
    assert Path(resolved).is_absolute()
    assert resolved == shutil.which("bash")


@needs_bash
async def test_child_process_really_runs_in_the_workspace(build):
    """SPEC 9.5 Invariant 1 / 15.2: the agent runs only in its workspace."""
    h = build(scenario(report_cwd=True))
    await h.client.start_session()

    cwd_events = [
        e for e in h.of("notification") if e.payload.get("method") == "test/cwd"
    ]
    assert cwd_events, f"fake never reported cwd; saw {h.names}"
    reported = Path(cwd_events[0].payload["params"]["cwd"]).resolve()
    assert reported == h.client.workspace.resolve()


async def test_relative_workspace_is_rejected_before_launch(build, tmp_path):
    spawned = []

    async def spawn(argv, cwd, env):  # pragma: no cover - must never run
        spawned.append(argv)
        raise AssertionError("launch must not happen")

    h = build(scenario(), workspace=Path("relative/ws"), spawn=spawn)
    with pytest.raises(InvalidWorkspaceCwd):
        await h.client.start_session()
    assert spawned == []
    assert h.names == ["startup_failed"]


async def test_missing_workspace_directory_is_rejected_before_launch(build, tmp_path):
    h = build(scenario(), workspace=tmp_path / "does_not_exist")
    with pytest.raises(InvalidWorkspaceCwd):
        await h.client.start_session()


def test_max_line_size_is_ten_megabytes():
    """SPEC 10.1 RECOMMENDED process setting."""
    assert mod.MAX_LINE_BYTES == 10 * 1024 * 1024


@needs_bash
async def test_declared_secret_env_names_are_stripped_from_child(build, monkeypatch):
    """SPEC 15.3 / 17.5: tracker secrets are not inherited by the child."""
    monkeypatch.setenv("SYMPHONY_TEST_TOKEN", "super-secret")
    monkeypatch.setenv("SYMPHONY_TEST_KEEP", "visible")
    h = build(
        scenario(report_env=["SYMPHONY_TEST_TOKEN", "SYMPHONY_TEST_KEEP"]),
        secret_env_names=["SYMPHONY_TEST_TOKEN"],
    )
    await h.client.start_session()

    env_events = [e for e in h.of("notification") if e.payload.get("method") == "test/env"]
    assert env_events, f"fake never reported env; saw {h.names}"
    present = env_events[0].payload["params"]["names"]
    assert "SYMPHONY_TEST_TOKEN" not in present
    assert "SYMPHONY_TEST_KEEP" in present


@needs_bash
async def test_missing_command_maps_to_codex_not_found(build):
    """SPEC 10.6: shell exit 127 means codex.command does not exist."""
    h = build(scenario(), command="symphony-no-such-codex-binary app-server")
    with pytest.raises(CodexNotFound):
        await h.client.start_session()
    assert h.names == ["startup_failed"]


# ---------------------------------------------------------------------------
# SPEC 10.2 — session startup responsibilities
# ---------------------------------------------------------------------------


@needs_bash
async def test_startup_initializes_then_creates_thread_with_cwd_and_policies(build):
    """SPEC 10.2 / 17.5: startup order, absolute cwd, documented policies."""
    h = build(scenario())
    session = await h.client.start_session()

    assert isinstance(session, AppServerSession)
    assert session.thread_id == "th_abc"
    assert h.sent_methods()[:3] == [PROTOCOL.initialize, PROTOCOL.initialized, PROTOCOL.new_thread]

    params = h.sent_params(PROTOCOL.new_thread)
    assert params[PROTOCOL.f_cwd] == str(h.client.workspace)
    assert Path(params[PROTOCOL.f_cwd]).is_absolute()
    assert params[PROTOCOL.f_approval_policy] == h.client.cfg.approval_policy
    assert params[PROTOCOL.f_thread_sandbox] == h.client.cfg.thread_sandbox

    init = h.sent_params(PROTOCOL.initialize)
    assert init[PROTOCOL.f_client_info]["name"]


@needs_bash
async def test_thread_response_without_identity_maps_to_response_error(build):
    h = build(scenario(new_thread={"result": {"nothing": "here"}}))
    with pytest.raises(ResponseError):
        await h.client.start_session()
    assert h.names == ["startup_failed"]


@needs_bash
async def test_jsonrpc_error_response_maps_to_response_error(build):
    h = build(scenario(new_thread={"error": {"code": -32001, "message": "no thread for you"}}))
    with pytest.raises(ResponseError) as excinfo:
        await h.client.start_session()
    assert "no thread for you" in str(excinfo.value)
    assert excinfo.value.category == "response_error"


@needs_bash
async def test_second_start_session_on_one_client_is_refused(build):
    h = build(scenario())
    await h.client.start_session()
    with pytest.raises(AgentError):
        await h.client.start_session()


@needs_bash
async def test_session_started_carries_composed_session_id(build):
    """SPEC 10.2 / 4.2: session_id == "<thread_id>-<turn_id>"."""
    h = build(scenario(thread_id="th_9", turn_id="tu_7"))
    session = await h.client.start_session()
    await h.client.run_turn(session, "do the work", title="ENG-1: Fix the thing")

    started = h.one("session_started")
    assert started.payload["thread_id"] == "th_9"
    assert started.payload["turn_id"] == "tu_7"
    assert started.payload["session_id"] == session_id("th_9", "tu_7") == "th_9-tu_7"
    assert started.payload["turn_number"] == 1


@needs_bash
async def test_turn_carries_prompt_title_cwd_and_sandbox_policy(build):
    """SPEC 10.2: issue-identifying title, absolute cwd, turn sandbox policy."""
    h = build(scenario())
    session = await h.client.start_session()
    await h.client.run_turn(session, "RENDERED ISSUE PROMPT", title="ENG-42: Ship it")

    params = h.sent_params(PROTOCOL.start_turn)
    assert params[PROTOCOL.f_prompt] == "RENDERED ISSUE PROMPT"
    assert params[PROTOCOL.f_title] == "ENG-42: Ship it"
    assert params[PROTOCOL.f_thread_id] == session.thread_id
    assert params[PROTOCOL.f_cwd] == str(h.client.workspace)
    assert params[PROTOCOL.f_turn_sandbox_policy] == h.client.cfg.turn_sandbox_policy


@needs_bash
async def test_tool_specs_are_advertised_at_thread_start(build):
    """SPEC 10.5 / 17.5: only the selected adapter's tools are advertised."""
    specs = [
        ToolSpec("linear_comment", "Comment on an issue", {"type": "object"}, True),
        ToolSpec("linear_read", "Read an issue", {"type": "object"}, False),
    ]
    h = build(scenario(), tool_specs=specs)
    await h.client.start_session()

    advertised = h.sent_params(PROTOCOL.new_thread)[PROTOCOL.f_tools]
    assert [t[PROTOCOL.f_tool_name] for t in advertised] == ["linear_comment", "linear_read"]
    assert advertised[0][PROTOCOL.f_tool_schema] == {"type": "object"}


@needs_bash
async def test_no_tools_key_when_no_specs_are_configured(build):
    h = build(scenario(), tool_specs=[])
    await h.client.start_session()
    assert PROTOCOL.f_tools not in h.sent_params(PROTOCOL.new_thread)


# ---------------------------------------------------------------------------
# SPEC 10.3 — streaming turn processing and continuation
# ---------------------------------------------------------------------------


@needs_bash
async def test_continuation_turn_reuses_thread_and_live_subprocess(build):
    """SPEC 10.2 / 10.3: same thread_id, same process, prompt not resent."""
    p = PROTOCOL
    turn_plan = {
        "result": {p.f_turn_id: "tu_x"},
        "then": [{"delay_ms": 5, "send": notif(p.turn_completed, {p.f_turn_id: "tu_x"})}],
    }
    h = build(scenario(start_turn=turn_plan))
    session = await h.client.start_session()
    pid_before = h.client.pid

    await session.start_turn("FIRST: rendered issue prompt", title="ENG-1: a")
    assert h.client.is_running
    await session.start_turn("SECOND: continuation guidance", title="ENG-1: a")

    assert h.client.pid == pid_before
    assert h.client.is_running
    assert h.client.turn_count == 2

    turns = [m for m in h.sent() if m.get("method") == p.start_turn]
    assert len(turns) == 2
    assert [t["params"][p.f_thread_id] for t in turns] == [session.thread_id] * 2
    assert turns[0]["params"][p.f_prompt] == "FIRST: rendered issue prompt"
    assert turns[1]["params"][p.f_prompt] == "SECOND: continuation guidance"
    assert h.sent_methods().count(PROTOCOL.new_thread) == 1
    assert [e.payload["turn_number"] for e in h.of("session_started")] == [1, 2]


@needs_bash
async def test_turn_failure_signal_raises_turn_failed(build):
    p = PROTOCOL
    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [{"delay_ms": 5, "send": notif(p.turn_failed, {"error": "model refused"})}],
    }
    h = build(scenario(start_turn=plan))
    session = await h.client.start_session()
    with pytest.raises(TurnFailed) as excinfo:
        await h.client.run_turn(session, "prompt")
    assert excinfo.value.category == "turn_failed"
    assert h.of("turn_failed")
    assert not h.of("turn_completed")


@needs_bash
async def test_turn_cancellation_signal_raises_turn_cancelled(build):
    p = PROTOCOL
    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [{"delay_ms": 5, "send": notif(p.turn_cancelled, {})}],
    }
    h = build(scenario(start_turn=plan))
    session = await h.client.start_session()
    with pytest.raises(TurnCancelled) as excinfo:
        await h.client.run_turn(session, "prompt")
    assert excinfo.value.category == "turn_cancelled"
    assert h.of("turn_cancelled")


@needs_bash
async def test_user_input_request_fails_fast_and_answers_the_request(build):
    """SPEC 10.5: documented policy fails the turn; the session never stalls."""
    p = PROTOCOL
    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [{"delay_ms": 5, "send": request(9001, p.user_input, {"prompt": "which env?"})}],
    }
    h = build(scenario(start_turn=plan))
    session = await h.client.start_session()
    with pytest.raises(TurnInputRequired) as excinfo:
        await h.client.run_turn(session, "prompt")
    assert excinfo.value.category == "turn_input_required"
    assert h.of("turn_input_required")

    replies = [r for r in h.responses() if r.get("id") == 9001]
    assert replies and "error" in replies[0], "the request must be answered, not left hanging"


@needs_bash
async def test_subprocess_exit_during_turn_maps_to_port_exit(build):
    p = PROTOCOL
    plan = {"result": {p.f_turn_id: "tu_1"}, "then": [{"delay_ms": 10}, {"exit": 3}]}
    h = build(scenario(start_turn=plan))
    session = await h.client.start_session()
    with pytest.raises(PortExit) as excinfo:
        await h.client.run_turn(session, "prompt")
    assert excinfo.value.category == "port_exit"
    assert h.of("turn_ended_with_error")


@needs_bash
async def test_stop_terminates_the_subprocess_and_is_idempotent(build):
    h = build(scenario())
    session = await h.client.start_session()
    assert h.client.is_running
    await session.stop()
    assert not h.client.is_running
    await session.stop()
    await h.client.stop_session(session)


@needs_bash
async def test_run_turn_rejects_a_foreign_session(build):
    h1 = build(scenario())
    h2 = build(scenario())
    s1 = await h1.client.start_session()
    await h2.client.start_session()
    with pytest.raises(AgentError):
        await h2.client.run_turn(s1, "prompt")


# ---------------------------------------------------------------------------
# SPEC 10.6 — the three timeouts are three distinct mechanisms
# ---------------------------------------------------------------------------


@needs_bash
async def test_read_timeout_bounds_a_request_response_exchange(build):
    """SPEC 10.6: read_timeout_ms covers startup and sync requests."""
    h = build(
        scenario(initialize={"silent": True}),
        codex=None,
    )
    h.client.cfg = FakeCodexConfig(command=h.client.cfg.command, read_timeout_ms=120)
    with pytest.raises(ResponseTimeout) as excinfo:
        await h.client.start_session()
    assert excinfo.value.category == "response_timeout"
    assert excinfo.value.details["read_timeout_ms"] == 120


@needs_bash
async def test_turn_timeout_fires_on_stream_silence(build):
    """SPEC 10.6 / 10.3: silence beyond turn_timeout_ms fails the turn."""
    p = PROTOCOL
    plan = {"result": {p.f_turn_id: "tu_1"}}  # responds, then says nothing
    codex = None
    h = build(scenario(start_turn=plan), codex=codex)
    h.client.cfg = FakeCodexConfig(command=h.client.cfg.command, turn_timeout_ms=150)
    session = await h.client.start_session()
    with pytest.raises(TurnTimeout) as excinfo:
        await h.client.run_turn(session, "prompt")
    assert excinfo.value.category == "turn_timeout"
    assert excinfo.value.details["turn_timeout_ms"] == 150
    assert h.client.is_running, "turn timeout is a turn failure, not a transport teardown"


@needs_bash
async def test_turn_timeout_is_a_silence_bound_not_a_total_runtime_cap(build):
    """SPEC 10.6: each app-server output resets the window.

    The turn streams for ~500ms with a 200ms window. A total-runtime cap would
    fail this; a silence bound must not.
    """
    p = PROTOCOL
    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [
            {"send": notif("thread/item/updated", {"n": 1}), "repeat": 10, "every_ms": 55},
            {"delay_ms": 20, "send": notif(p.turn_completed, {p.f_turn_id: "tu_1"})},
        ],
    }
    h = build(scenario(start_turn=plan))
    h.client.cfg = FakeCodexConfig(command=h.client.cfg.command, turn_timeout_ms=200)
    session = await h.client.start_session()

    await h.client.run_turn(session, "prompt")  # must not raise

    assert h.of("turn_completed")
    assert len([e for e in h.of("notification") if e.payload.get("method") ==
                "thread/item/updated"]) == 10


def test_stall_timeout_is_not_read_by_this_module():
    """SPEC 10.6: stall_timeout_ms is enforced by the orchestrator, not here.

    Guards against the conformance failure of collapsing the three distinct
    timeouts onto one mechanism: this module must never consume the third.
    """
    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "cfg.stall_timeout_ms" not in source
    assert "read_timeout_ms" in source
    assert "turn_timeout_ms" in source


# ---------------------------------------------------------------------------
# SPEC 10.3 — transport framing, stderr separation, malformed input
# ---------------------------------------------------------------------------


@needs_bash
async def test_stderr_is_kept_separate_from_the_protocol_stream(build):
    """SPEC 10.3 / 17.5: diagnostics never enter protocol parsing."""
    h = build(
        scenario(
            stderr=["codex: warming up", "codex: still here"],
            start_turn={
                "result": {PROTOCOL.f_turn_id: "tu_1"},
                "then": [
                    {"stderr": "codex: mid-turn diagnostic"},
                    {"delay_ms": 5,
                     "send": notif(PROTOCOL.turn_completed, {PROTOCOL.f_turn_id: "tu_1"})},
                ],
            },
        )
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    assert h.of("turn_completed")
    assert not h.of("malformed")
    tail = "\n".join(h.client.stderr_tail())
    assert "codex: warming up" in tail


@needs_bash
async def test_non_json_line_emits_malformed_and_the_session_survives(build):
    """A `bash -lc` login profile can print to stdout; resync, do not crash."""
    h = build(scenario(raw_prelude=["motd: welcome to the build box", "<<<not json>>>"]))
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    malformed = h.of("malformed")
    assert len(malformed) == 2
    assert malformed[0].payload["reason"] == "not_json"
    assert "motd" in malformed[0].payload["preview"]
    assert h.of("turn_completed")


@needs_bash
async def test_oversize_line_is_dropped_and_the_session_survives(build, monkeypatch):
    """SPEC 10.1 max line size; behavior verified with a small injected limit."""
    monkeypatch.setattr(mod, "MAX_LINE_BYTES", 4096)
    h = build(
        scenario(
            start_turn={
                "result": {PROTOCOL.f_turn_id: "tu_1"},
                "then": [
                    {"raw_size": 40_000},
                    {"delay_ms": 20,
                     "send": notif(PROTOCOL.turn_completed, {PROTOCOL.f_turn_id: "tu_1"})},
                ],
            }
        )
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    reasons = [e.payload.get("reason") for e in h.of("malformed")]
    assert "line_exceeds_max_line_size" in reasons
    assert h.of("turn_completed")


@needs_bash
async def test_unmatched_response_becomes_other_message(build):
    h = build(scenario(raw_prelude=[json.dumps({"jsonrpc": "2.0", "id": 4242, "result": {}})]))
    await h.client.start_session()
    assert any(e.payload.get("reason") == "unmatched_response" for e in h.of("other_message"))


# ---------------------------------------------------------------------------
# SPEC 10.5 — approvals, tool calls, unsupported calls
# ---------------------------------------------------------------------------


@needs_bash
async def test_command_approval_is_auto_approved_without_stalling(build):
    p = PROTOCOL
    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [
            {"send": request(5001, p.exec_approval, {"command": ["rm", "-rf", "build"]})},
            {"await_response": 5001},
            {"send": notif(p.turn_completed, {p.f_turn_id: "tu_1"})},
        ],
    }
    h = build(scenario(start_turn=plan))
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    reply = next(r for r in h.responses() if r.get("id") == 5001)
    assert reply["result"][p.f_decision] == p.approve_value
    assert h.one("approval_auto_approved").payload["method"] == p.exec_approval


@needs_bash
async def test_file_change_approval_is_auto_approved(build):
    p = PROTOCOL
    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [
            {"send": request(5002, p.patch_approval, {"patch": "diff"})},
            {"await_response": 5002},
            {"send": notif(p.turn_completed, {p.f_turn_id: "tu_1"})},
        ],
    }
    h = build(scenario(start_turn=plan))
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    reply = next(r for r in h.responses() if r.get("id") == 5002)
    assert reply["result"][p.f_decision] == p.approve_value


@needs_bash
async def test_injected_approval_decider_can_deny(build):
    """SPEC 10.5: the policy is pluggable; symphony.agent.approvals owns it."""
    p = PROTOCOL
    seen: list[str] = []

    def deny(method: str, params: dict[str, Any]) -> ApprovalDecision:
        seen.append(method)
        return ApprovalDecision(approved=False, reason="policy: read-only host")

    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [
            {"send": request(5003, p.exec_approval, {"command": ["curl"]})},
            {"await_response": 5003},
            {"send": notif(p.turn_completed, {p.f_turn_id: "tu_1"})},
        ],
    }
    h = build(scenario(start_turn=plan), approval_decider=deny)
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    assert seen == [p.exec_approval]
    reply = next(r for r in h.responses() if r.get("id") == 5003)
    assert reply["result"][p.f_decision] == p.deny_value
    assert not h.of("approval_auto_approved")


def test_default_policy_is_the_documented_high_trust_posture():
    """CONTRACTS section 5 / SPEC 10.5."""
    assert default_approval_decision(PROTOCOL.exec_approval, {}).approved
    assert default_approval_decision(PROTOCOL.patch_approval, {}).approved


@needs_bash
async def test_advertised_tool_executes_host_side_and_returns_its_result(build):
    p = PROTOCOL
    calls: list[tuple[str, dict[str, Any]]] = []

    async def executor(name: str, arguments: dict[str, Any]) -> ToolResult:
        calls.append((name, arguments))
        return ToolResult.success({"comment_id": "c_1"})

    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [
            {
                "send": request(
                    6001,
                    p.tool_call,
                    {p.f_tool_name: "linear_comment", p.f_tool_arguments: {"body": "hi"}},
                )
            },
            {"await_response": 6001},
            {"send": notif(p.turn_completed, {p.f_turn_id: "tu_1"})},
        ],
    }
    h = build(
        scenario(start_turn=plan),
        tool_specs=[ToolSpec("linear_comment", "Comment", {"type": "object"}, True)],
        tool_executor=executor,
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    assert calls == [("linear_comment", {"body": "hi"})]
    reply = next(r for r in h.responses() if r.get("id") == 6001)
    assert reply["result"][p.f_tool_ok] is True
    assert reply["result"][p.f_tool_content] == {"comment_id": "c_1"}


@needs_bash
async def test_unsupported_tool_call_fails_structurally_without_stalling(build):
    """SPEC 10.5 / 17.5: reject unknown tools, keep the session running."""
    p = PROTOCOL
    called: list[str] = []

    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [
            {"send": request(6002, p.tool_call, {p.f_tool_name: "delete_everything"})},
            {"await_response": 6002},
            {"send": notif(p.turn_completed, {p.f_turn_id: "tu_1"})},
        ],
    }
    h = build(
        scenario(start_turn=plan),
        tool_specs=[ToolSpec("linear_comment", "Comment", {"type": "object"}, True)],
        tool_executor=lambda n, a: called.append(n),
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    assert called == [], "an unadvertised tool must never reach the executor"
    reply = next(r for r in h.responses() if r.get("id") == 6002)
    assert reply["result"][p.f_tool_ok] is False
    assert "delete_everything" in reply["result"][p.f_tool_error]
    assert h.one("unsupported_tool_call").payload["tool"] == "delete_everything"
    assert h.of("turn_completed")


@needs_bash
async def test_raising_tool_executor_returns_failure_and_keeps_the_session(build):
    p = PROTOCOL

    async def boom(name: str, arguments: dict[str, Any]) -> ToolResult:
        raise RuntimeError("tracker unreachable")

    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [
            {"send": request(6003, p.tool_call, {p.f_tool_name: "linear_comment"})},
            {"await_response": 6003},
            {"send": notif(p.turn_completed, {p.f_turn_id: "tu_1"})},
        ],
    }
    h = build(
        scenario(start_turn=plan),
        tool_specs=[ToolSpec("linear_comment", "Comment", {"type": "object"}, True)],
        tool_executor=boom,
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    reply = next(r for r in h.responses() if r.get("id") == 6003)
    assert reply["result"][p.f_tool_ok] is False
    assert "tracker unreachable" in reply["result"][p.f_tool_error]
    assert h.of("turn_completed")


@needs_bash
async def test_unknown_server_request_is_answered_not_ignored(build):
    p = PROTOCOL
    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [
            {"send": request(7001, "thread/somethingBrandNew", {})},
            {"await_response": 7001},
            {"send": notif(p.turn_completed, {p.f_turn_id: "tu_1"})},
        ],
    }
    h = build(scenario(start_turn=plan))
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    reply = next(r for r in h.responses() if r.get("id") == 7001)
    assert "error" in reply
    assert h.of("turn_completed")


# ---------------------------------------------------------------------------
# SPEC 10.4 / 13.5 — emitted events and telemetry pass-through
# ---------------------------------------------------------------------------


@needs_bash
async def test_events_carry_pid_and_timestamp(build):
    stamp = datetime.fromisoformat("2026-07-28T00:00:00+00:00")
    h = build(scenario(), now=lambda: stamp)
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    assert h.events
    for event in h.events:
        assert event.timestamp == stamp
        assert event.codex_app_server_pid == h.client.pid
        assert event.codex_app_server_pid is not None


@needs_bash
async def test_token_usage_notification_surfaces_the_usage_map(build):
    """SPEC 10.4 / 13.5: usage is surfaced; accounting stays in the orchestrator."""
    p = PROTOCOL
    usage = {"input_tokens": 120, "output_tokens": 34, "total_tokens": 154}
    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [
            {"send": notif(p.token_usage, {p.f_usage: usage})},
            {"delay_ms": 10, "send": notif(p.turn_completed, {p.f_turn_id: "tu_1"})},
        ],
    }
    h = build(scenario(start_turn=plan))
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    usage_events = [e for e in h.of("notification") if e.payload.get("method") == p.token_usage]
    assert usage_events
    assert usage_events[0].usage == usage


@needs_bash
async def test_rate_limit_payload_is_passed_through(build):
    p = PROTOCOL
    limits = {"primary": {"used_percent": 12.5, "resets_at": "2026-07-28T01:00:00Z"}}
    plan = {
        "result": {p.f_turn_id: "tu_1"},
        "then": [
            {"send": notif("thread/rateLimits/updated", {p.f_rate_limits: limits})},
            {"delay_ms": 10, "send": notif(p.turn_completed, {p.f_turn_id: "tu_1"})},
        ],
    }
    h = build(scenario(start_turn=plan))
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")

    events = [
        e for e in h.of("notification")
        if e.payload.get("method") == "thread/rateLimits/updated"
    ]
    assert events
    assert events[0].payload["params"][p.f_rate_limits] == limits


@needs_bash
async def test_observer_exceptions_do_not_break_the_run(build):
    """SPEC 17.6: a failing sink must not crash orchestration."""
    h = build(scenario())

    def explode(event: Any) -> None:
        raise RuntimeError("sink is down")

    h.client._on_event = explode  # deliberate sink-failure injection
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt")
    assert h.client.is_running


# ---------------------------------------------------------------------------
# SPEC 10 preamble — protocol strings are isolated from the state machine
# ---------------------------------------------------------------------------


async def test_protocol_names_block_is_a_single_replaceable_unit():
    custom = ProtocolNames(initialize="boot", new_thread="conv/new", start_turn="conv/send")
    assert custom.initialize == "boot"
    assert PROTOCOL.initialize == "initialize"
    assert custom.turn_terminal_outcome(custom.turn_completed) == "completed"
    assert custom.turn_terminal_outcome(custom.turn_failed) == "failed"
    assert custom.turn_terminal_outcome(custom.turn_cancelled) == "cancelled"
    assert custom.turn_terminal_outcome("thread/anything/else") is None


@needs_bash
async def test_whole_lifecycle_works_under_renamed_protocol_strings(build):
    """If a real binary uses other names, only ProtocolNames must change."""
    other = ProtocolNames(
        initialize="session/initialize",
        initialized="session/ready",
        new_thread="conversation/create",
        start_turn="conversation/sendUserTurn",
        turn_completed="conversation/turnDone",
        exec_approval="conversation/execApproval",
        f_thread_id="conversationId",
        f_turn_id="userTurnId",
        f_prompt="items",
        f_decision="verdict",
        approve_value="always_allow",
    )
    plan = {
        "result": {other.f_turn_id: "T7"},
        "then": [
            {"send": request(8001, other.exec_approval, {"command": ["ls"]})},
            {"await_response": 8001},
            {"send": notif(other.turn_completed, {other.f_turn_id: "T7"})},
        ],
    }
    h = build(
        scenario(p=other, thread_id="C7", turn_id="T7", start_turn=plan),
        protocol=other,
    )
    session = await h.client.start_session()
    await h.client.run_turn(session, "prompt", title="ENG-3: renamed")

    assert session.thread_id == "C7"
    assert h.sent_methods()[:3] == [other.initialize, other.initialized, other.new_thread]
    assert h.sent_params(other.start_turn)[other.f_prompt] == "prompt"
    assert h.one("session_started").payload["session_id"] == "C7-T7"
    reply = next(r for r in h.responses() if r.get("id") == 8001)
    assert reply["result"][other.f_decision] == other.approve_value
    assert h.of("turn_completed")


@needs_bash
async def test_identity_is_read_from_a_nested_wrapper_shape(build):
    """Codex versions differ on nesting; both shapes resolve to one thread_id."""
    h = build(
        scenario(new_thread={"result": {"thread": {PROTOCOL.f_thread_id: "th_nested"}}})
    )
    session = await h.client.start_session()
    assert session.thread_id == "th_nested"
