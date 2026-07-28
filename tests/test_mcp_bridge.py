"""Tests for the provider-native tracker tool bridge (SPEC 10.5, 11.5, 15.3).

The bridge is the Claude backend's answer to the Codex app-server's tool
advertisement. Its transport choice is a security decision — an in-process HTTP
server keeps the tracker credential out of the agent's process tree, which a
stdio server launched by Claude Code could not do — so the authorization tests
below are load-bearing rather than hygiene.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from symphony.agent.mcp_bridge import MCP_SERVER_NAME, TrackerToolBridge, mcp_tool_name
from symphony.trackers.base import ToolResult, ToolSpec

SPECS = [
    ToolSpec(
        name="linear_set_issue_state",
        description="Move the issue",
        input_schema={"type": "object", "properties": {"state_name": {"type": "string"}}},
        mutates_tracker=True,
    ),
    ToolSpec(
        name="linear_list_workflow_states",
        description="List states",
        input_schema={"type": "object", "properties": {}},
        mutates_tracker=False,
    ),
]


class Recorder:
    """Stands in for the runner's host-side tool executor."""

    def __init__(self, result: ToolResult | None = None, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = result or ToolResult.success({"moved": True})
        self.raises = raises

    async def __call__(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((name, arguments))
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
async def bridge():
    recorder = Recorder()
    b = TrackerToolBridge(SPECS, recorder)
    await b.start()
    b.recorder = recorder
    try:
        yield b
    finally:
        await b.stop()


async def rpc(
    b: TrackerToolBridge,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    rid: Any = 1,
    token: str | None = None,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token if token is not None else b.token}"}
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        payload["id"] = rid
    if params is not None:
        payload["params"] = params
    async with httpx.AsyncClient() as client:
        return await client.post(b.url, json=payload, headers=headers, timeout=10)


# -- lifecycle ---------------------------------------------------------------


async def test_binds_loopback_on_an_ephemeral_port(bridge: TrackerToolBridge) -> None:
    assert bridge.running
    assert bridge.port is not None and bridge.port > 0
    assert bridge.url.startswith("http://127.0.0.1:")


async def test_stop_is_idempotent(bridge: TrackerToolBridge) -> None:
    await bridge.stop()
    await bridge.stop()
    assert not bridge.running


async def test_a_bridge_with_no_tools_never_starts() -> None:
    b = TrackerToolBridge([], Recorder())
    await b.start()
    assert not b.running


# -- authorization: the reason this is HTTP and not stdio --------------------


async def test_a_missing_token_is_rejected(bridge: TrackerToolBridge) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            bridge.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=10
        )
    assert response.status_code == 401


async def test_a_wrong_token_cannot_reach_a_mutating_tool(bridge: TrackerToolBridge) -> None:
    response = await rpc(
        bridge,
        "tools/call",
        {"name": "linear_set_issue_state", "arguments": {"state_name": "Done"}},
        token="not-the-token",
    )
    assert response.status_code == 401
    # The point of the token: loopback alone is not an authorization boundary,
    # and these tools write to a real tracker.
    assert bridge.recorder.calls == []


async def test_tokens_differ_between_bridges() -> None:
    first, second = TrackerToolBridge(SPECS, Recorder()), TrackerToolBridge(SPECS, Recorder())
    assert first.token != second.token
    assert len(first.token) >= 32


# -- MCP protocol ------------------------------------------------------------


async def test_initialize_echoes_the_requested_protocol_version(bridge: TrackerToolBridge) -> None:
    result = (await rpc(bridge, "initialize", {"protocolVersion": "2024-11-05"})).json()["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == MCP_SERVER_NAME
    assert "tools" in result["capabilities"]


async def test_tools_list_exposes_every_adapter_spec(bridge: TrackerToolBridge) -> None:
    tools = (await rpc(bridge, "tools/list")).json()["result"]["tools"]
    assert [t["name"] for t in tools] == [s.name for s in SPECS]
    assert tools[0]["inputSchema"] == SPECS[0].input_schema


async def test_a_mutating_tool_says_so_in_its_description(bridge: TrackerToolBridge) -> None:
    """MCP has no first-class mutation flag, so it is surfaced in prose."""
    tools = {t["name"]: t for t in (await rpc(bridge, "tools/list")).json()["result"]["tools"]}
    assert "mutates tracker state" in tools["linear_set_issue_state"]["description"]
    assert "mutates tracker state" not in tools["linear_list_workflow_states"]["description"]


async def test_notifications_are_accepted_without_a_result(bridge: TrackerToolBridge) -> None:
    response = await rpc(bridge, "notifications/initialized", {}, rid=None)
    assert response.status_code == 202


async def test_an_unknown_method_is_a_jsonrpc_error(bridge: TrackerToolBridge) -> None:
    assert (await rpc(bridge, "resources/list")).json()["error"]["code"] == -32601


# -- tool execution ----------------------------------------------------------


async def test_a_tool_call_reaches_the_host_side_executor(bridge: TrackerToolBridge) -> None:
    response = await rpc(
        bridge,
        "tools/call",
        {"name": "linear_set_issue_state", "arguments": {"state_name": "Human Review"}},
    )
    result = response.json()["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"moved": True}
    assert bridge.recorder.calls == [("linear_set_issue_state", {"state_name": "Human Review"})]


async def test_an_unsupported_tool_fails_structurally_without_stalling(
    bridge: TrackerToolBridge,
) -> None:
    """SPEC 10.5: an unsupported name returns a failure result, never raises."""
    result = (await rpc(bridge, "tools/call", {"name": "rm_rf", "arguments": {}})).json()["result"]
    assert result["isError"] is True
    assert "unsupported tool" in result["content"][0]["text"]
    assert bridge.recorder.calls == []


async def test_an_adapter_failure_becomes_a_tool_error_not_a_transport_error() -> None:
    b = TrackerToolBridge(SPECS, Recorder(result=ToolResult.failure("rate limited")))
    await b.start()
    try:
        response = await rpc(b, "tools/call", {"name": "linear_set_issue_state", "arguments": {}})
        result = response.json()["result"]
        assert result["isError"] is True
        assert "rate limited" in result["content"][0]["text"]
    finally:
        await b.stop()


async def test_an_executor_exception_does_not_break_the_session() -> None:
    b = TrackerToolBridge(SPECS, Recorder(raises=RuntimeError("boom")))
    await b.start()
    try:
        response = await rpc(b, "tools/call", {"name": "linear_set_issue_state", "arguments": {}})
        result = response.json()["result"]
        assert result["isError"] is True
        assert "RuntimeError" in result["content"][0]["text"]
    finally:
        await b.stop()


# -- Claude Code wiring ------------------------------------------------------


def test_mcp_config_carries_the_url_and_bearer_token() -> None:
    b = TrackerToolBridge(SPECS, Recorder())
    b._port = 9999  # shape assertion only; no bind needed
    entry = b.mcp_config()["mcpServers"][MCP_SERVER_NAME]
    assert entry["type"] == "http"
    assert entry["url"].endswith("/mcp")
    assert entry["headers"]["Authorization"] == f"Bearer {b.token}"


def test_allowed_tool_patterns_name_each_tool_explicitly() -> None:
    b = TrackerToolBridge(SPECS, Recorder())
    assert b.allowed_tool_patterns() == [
        "mcp__symphony__linear_set_issue_state",
        "mcp__symphony__linear_list_workflow_states",
    ]
    # A wildcard would widen silently the moment the adapter gains a tool.
    assert not any("*" in pattern for pattern in b.allowed_tool_patterns())


def test_mcp_tool_name_matches_claude_codes_namespacing() -> None:
    assert mcp_tool_name("linear_add_comment") == "mcp__symphony__linear_add_comment"
