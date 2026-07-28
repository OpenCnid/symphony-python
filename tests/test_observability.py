"""Conformance tests for ``symphony.observability`` (SPEC 13.1-13.6, 17.6).

Every fixture builds :class:`~symphony.models.OrchestratorState` directly from
the pre-written ``symphony.models``; no sibling module is imported, so nothing
here depends on work in flight elsewhere. All clocks are injected — the suite
contains no wall-clock sleeps.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from symphony.models import (
    CodexTotals,
    Issue,
    LiveSession,
    OrchestratorState,
    RetryEntry,
    RunningEntry,
    RunPhase,
)
from symphony.observability import humanize, status
from symphony.observability import logging as slog
from symphony.observability import snapshot as snap

T0 = datetime(2026, 2, 24, 20, 10, 12, tzinfo=UTC)
NOW = datetime(2026, 2, 24, 20, 15, 30, tzinfo=UTC)
MONO_MS = 1_000_000.0


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_logging():
    """Never let a test inherit or leak the process-wide router."""
    slog.reset_logging()
    yield
    slog.reset_logging()


@pytest.fixture
def sink() -> slog.ListSink:
    collector = slog.ListSink(name="probe")
    slog.configure(sinks=[collector], level="debug", clock=lambda: NOW)
    return collector


class RaisingSink:
    """A sink that always fails, as SPEC 13.2 requires us to tolerate."""

    def __init__(self, name: str = "broken", error: str = "disk full") -> None:
        self.name = name
        self.error = error
        self.calls = 0

    def emit(self, record: slog.LogRecord) -> None:
        self.calls += 1
        raise OSError(self.error)


def make_issue(**overrides: Any) -> Issue:
    base: dict[str, Any] = {
        "id": "abc123",
        "identifier": "MT-649",
        "title": "Fix the flaky poller",
        "state": "In Progress",
        "dispatchable": True,
        "url": "https://tracker.example/issues/MT-649",
    }
    base.update(overrides)
    return Issue(**base)


def make_running(
    *,
    started_at: datetime = T0,
    issue: Issue | None = None,
    session: LiveSession | None = None,
    **overrides: Any,
) -> RunningEntry:
    issue = issue or make_issue()
    session = session or LiveSession(
        session_id="thread-1-turn-1",
        thread_id="thread-1",
        turn_id="turn-1",
        last_codex_event="turn_completed",
        last_codex_timestamp=datetime(2026, 2, 24, 20, 14, 59, tzinfo=UTC),
        last_codex_message="Working on tests",
        codex_input_tokens=1200,
        codex_output_tokens=800,
        codex_total_tokens=2000,
        turn_count=7,
    )
    entry = RunningEntry(
        issue=issue,
        identifier=issue.identifier,
        started_at=started_at,
        session=session,
        workspace_path="/tmp/symphony_workspaces/MT-649",
        phase=RunPhase.STREAMING_TURN,
    )
    for key, value in overrides.items():
        setattr(entry, key, value)
    return entry


def make_state(
    *,
    running: dict[str, RunningEntry] | None = None,
    retry: dict[str, RetryEntry] | None = None,
    totals: CodexTotals | None = None,
    **overrides: Any,
) -> OrchestratorState:
    state = OrchestratorState(
        running=running or {},
        retry_attempts=retry or {},
        codex_totals=totals or CodexTotals(),
    )
    state.claimed.update(state.running)
    state.claimed.update(state.retry_attempts)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


# ==========================================================================
# SPEC 13.1 — logging conventions
# ==========================================================================


def test_bound_issue_context_appears_as_key_value(sink: slog.ListSink) -> None:
    """SPEC 13.1: issue-related logs carry issue_id and issue_identifier."""
    log = slog.get_logger("symphony.orchestrator").bind(
        issue_id="abc123", issue_identifier="MT-649"
    )
    log.info("agent attempt finished", outcome="completed")

    line = sink.text
    assert "issue_id=abc123" in line
    assert "issue_identifier=MT-649" in line
    assert 'msg="agent attempt finished"' in line
    assert "outcome=completed" in line
    assert "level=info" in line
    assert "logger=symphony.orchestrator" in line
    assert "ts=2026-02-24T20:15:30.000Z" in line


def test_session_lifecycle_context_is_rendered(sink: slog.ListSink) -> None:
    """SPEC 13.1: session lifecycle logs carry session_id."""
    slog.get_logger("symphony.agent").bind(session_id="thread-1-turn-1").info(
        "session started", outcome="started"
    )
    assert "session_id=thread-1-turn-1" in sink.text


def test_required_context_fields_lead_the_line_in_stable_order(sink: slog.ListSink) -> None:
    """SPEC 13.1 requires *stable* phrasing, so ordering cannot follow bind order."""
    log = slog.get_logger("s").bind(session_id="s1").bind(issue_identifier="MT-1")
    log.warning("retrying", issue_id="abc", outcome="retrying")

    rendered = sink.lines[0]
    positions = [
        rendered.index("issue_id=abc"),
        rendered.index("issue_identifier=MT-1"),
        rendered.index("session_id=s1"),
        rendered.index("outcome=retrying"),
    ]
    assert positions == sorted(positions)


def test_bind_is_immutable_and_layered(sink: slog.ListSink) -> None:
    """A caller holds one bound logger per issue; binding must not mutate it."""
    base = slog.get_logger("symphony.core")
    per_issue = base.bind(issue_id="abc123", issue_identifier="MT-649")
    per_turn = per_issue.bind(session_id="thread-1-turn-1")

    assert base.fields == {}
    assert dict(per_issue.fields) == {"issue_id": "abc123", "issue_identifier": "MT-649"}
    assert per_turn is not per_issue
    assert "session_id" not in per_issue.fields

    base.info("plain")
    assert "issue_id" not in sink.lines[0]


def test_bind_override_keeps_key_position(sink: slog.ListSink) -> None:
    log = slog.get_logger("s").bind(attempt=1, phase="StreamingTurn").bind(attempt=2)
    log.info("progress")
    rendered = sink.lines[0]
    assert "attempt=2" in rendered
    assert rendered.index("attempt=2") < rendered.index("phase=StreamingTurn")


def test_bind_with_no_fields_returns_same_logger(sink: slog.ListSink) -> None:
    log = slog.get_logger("s")
    assert log.bind() is log


def test_get_logger_is_cached_per_name(sink: slog.ListSink) -> None:
    assert slog.get_logger("symphony.a") is slog.get_logger("symphony.a")
    assert slog.get_logger("symphony.a") is not slog.get_logger("symphony.b")


def test_values_are_quoted_only_when_needed(sink: slog.ListSink) -> None:
    slog.get_logger("s").info(
        "outcome",
        reason="no available orchestrator slots",
        path="/tmp/symphony_workspaces/MT-649",
        attempt=3,
        ok=True,
        missing=None,
        elapsed=1.5,
    )
    rendered = sink.lines[0]
    assert 'reason="no available orchestrator slots"' in rendered
    assert "path=/tmp/symphony_workspaces/MT-649" in rendered
    assert "attempt=3" in rendered
    assert "ok=true" in rendered
    assert "missing=null" in rendered
    assert "elapsed=1.5" in rendered


def test_newlines_in_message_cannot_forge_a_log_line(sink: slog.ListSink) -> None:
    slog.get_logger("s").error("boom\nlevel=info msg=\"fake\"")
    assert "\n" not in sink.lines[0]


@pytest.mark.parametrize(
    "key",
    ["token", "api_key", "github_token", "authorization", "password", "tracker_secret", "pat"],
)
def test_secret_shaped_fields_are_redacted(sink: slog.ListSink, key: str) -> None:
    """SPEC 15.3 / 13.1: never log API tokens or secret env values."""
    slog.get_logger("s").info("configured", **{key: "ghp_SUPERSECRETVALUE"})
    assert "ghp_SUPERSECRETVALUE" not in sink.text
    assert f"{key}={slog.REDACTED}" in sink.text


@pytest.mark.parametrize(
    "key", ["input_tokens", "output_tokens", "total_tokens", "turn_count", "workspace_key"]
)
def test_counter_fields_are_not_mistaken_for_secrets(sink: slog.ListSink, key: str) -> None:
    slog.get_logger("s").info("totals", **{key: 42})
    assert f"{key}=42" in sink.text


def test_large_payloads_are_summarized_not_dumped(sink: slog.ListSink) -> None:
    """SPEC 13.1: avoid logging large raw payloads."""
    slog.get_logger("s").info(
        "event",
        payload={"prompt": "x" * 5000, "native_ref": "y" * 5000},
        blob="z" * 5000,
    )
    rendered = sink.lines[0]
    assert "xxxxx" not in rendered
    assert "<map n=2" in rendered
    assert "[truncated]" in rendered
    assert len(rendered) < 2000


def test_level_filtering_suppresses_debug_by_default() -> None:
    collector = slog.ListSink()
    slog.configure(sinks=[collector], level="info", clock=lambda: NOW)
    log = slog.get_logger("s")
    log.debug("hidden")
    log.info("shown")
    assert [r.message for r in collector.records] == ["shown"]

    slog.set_level("debug")
    log.debug("now visible")
    assert [r.message for r in collector.records] == ["shown", "now visible"]


def test_record_to_dict_is_json_safe_and_redacted(sink: slog.ListSink) -> None:
    slog.get_logger("s").info("x", issue_id="abc", api_key="secret-value")
    payload = sink.records[0].to_dict()
    assert json.loads(json.dumps(payload))["fields"] == {
        "issue_id": "abc",
        "api_key": slog.REDACTED,
    }


# ==========================================================================
# SPEC 13.2 / 14.2 / 17.6 — sink failures
# ==========================================================================


def test_sink_failure_does_not_crash_the_caller() -> None:
    """SPEC 17.6: logging sink failures do not crash orchestration."""
    broken = RaisingSink()
    slog.configure(sinks=[broken], fallback=io.StringIO(), clock=lambda: NOW)
    slog.get_logger("s").info("dispatching")  # must not raise
    assert broken.calls == 1


def test_sink_failure_is_reported_through_a_remaining_sink() -> None:
    """SPEC 13.2: emit an operator-visible warning through any remaining sink."""
    broken = RaisingSink(name="file-sink", error="disk full")
    good = slog.ListSink(name="stderr")
    slog.configure(sinks=[broken, good], clock=lambda: NOW)

    slog.get_logger("s").error("dispatch failed", outcome="failed")

    messages = [r.message for r in good.records]
    assert messages == ["dispatch failed", "log sink failed"]
    notice = good.records[1]
    assert notice.level == "warning"
    assert notice.fields["sink"] == "file-sink"
    assert notice.fields["outcome"] == "failed"
    assert "disk full" in notice.fields["reason"]


def test_total_sink_failure_still_reaches_the_fallback_stream() -> None:
    """SPEC 13.2: startup/dispatch failures stay visible without a debugger."""
    fallback = io.StringIO()
    slog.configure(sinks=[RaisingSink()], fallback=fallback, clock=lambda: NOW)

    slog.get_logger("symphony.cli").error("startup failed", reason="missing WORKFLOW.md")

    text = fallback.getvalue()
    assert 'msg="startup failed"' in text
    assert 'reason="missing WORKFLOW.md"' in text
    assert "log sink failed" in text


def test_failure_report_survives_a_second_sink_failing_mid_report() -> None:
    """The failure notice must never recurse into an exception."""
    fallback = io.StringIO()
    first, second = RaisingSink(name="a"), RaisingSink(name="b")
    slog.configure(sinks=[first, second], fallback=fallback, clock=lambda: NOW)

    slog.get_logger("s").info("tick")  # must not raise

    text = fallback.getvalue()
    assert text.count("log sink failed") == 2
    assert 'msg="tick"' in text


def test_persistently_failing_sink_is_disabled_after_a_threshold() -> None:
    broken = RaisingSink(name="flaky")
    good = slog.ListSink()
    slog.configure(sinks=[broken, good], clock=lambda: NOW, max_consecutive_failures=2)
    log = slog.get_logger("s")

    log.info("one")
    log.info("two")
    log.info("three")

    assert broken.calls == 2, "a disabled sink must not be called again"
    assert [r.fields.get("outcome") for r in good.records if r.message.startswith("log sink")] == [
        "failed",
        "disabled",
    ]
    assert broken in slog.get_router().disabled_sinks
    assert broken not in slog.get_router().sinks


def test_one_success_resets_the_failure_counter() -> None:
    class Flaky:
        name = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        def emit(self, record: slog.LogRecord) -> None:
            self.calls += 1
            if self.calls % 2:
                raise RuntimeError("transient")

    flaky = Flaky()
    slog.configure(sinks=[flaky, slog.ListSink()], clock=lambda: NOW, max_consecutive_failures=2)
    log = slog.get_logger("s")
    for _ in range(6):
        log.info("tick")

    assert flaky.calls == 6
    assert flaky in slog.get_router().sinks


def test_plain_callable_sinks_are_supported() -> None:
    captured: list[str] = []
    slog.configure(sinks=[lambda record: captured.append(record.message)], clock=lambda: NOW)
    slog.get_logger("s").info("hello")
    assert captured == ["hello"]


# ==========================================================================
# SPEC 13.3 / 13.7.2 — snapshot shape
# ==========================================================================


def test_snapshot_top_level_shape_matches_spec_13_7_2() -> None:
    state = make_state(running={"abc123": make_running()})
    doc = snap.build_snapshot(state, now=NOW, monotonic_ms=MONO_MS)
    assert list(doc) == list(snap.SNAPSHOT_KEYS)
    assert doc["generated_at"] == "2026-02-24T20:15:30Z"
    assert doc["counts"] == {"running": 1, "retrying": 0}
    assert doc["rate_limits"] is None


def test_snapshot_running_row_matches_spec_13_7_2() -> None:
    state = make_state(running={"abc123": make_running()})
    row = snap.build_snapshot(state, now=NOW, monotonic_ms=MONO_MS)["running"][0]
    assert list(row) == list(snap.RUNNING_ROW_KEYS)
    assert row == {
        "issue_id": "abc123",
        "issue_identifier": "MT-649",
        "issue_url": "https://tracker.example/issues/MT-649",
        "state": "In Progress",
        "session_id": "thread-1-turn-1",
        "turn_count": 7,
        "last_event": "turn_completed",
        "last_message": "Working on tests",
        "started_at": "2026-02-24T20:10:12Z",
        "last_event_at": "2026-02-24T20:14:59Z",
        "tokens": {"input_tokens": 1200, "output_tokens": 800, "total_tokens": 2000},
    }


def test_snapshot_retry_row_derives_wall_clock_due_at() -> None:
    """SPEC 4.1.7: due_at_ms is monotonic; the API field is wall time."""
    retry = RetryEntry(
        issue_id="def456",
        identifier="MT-650",
        attempt=3,
        due_at_ms=MONO_MS + 30_000,
        error="no available orchestrator slots",
    )
    doc = snap.build_snapshot(
        make_state(retry={"def456": retry}), now=NOW, monotonic_ms=MONO_MS
    )
    row = doc["retrying"][0]
    assert list(row) == list(snap.RETRY_ROW_KEYS)
    assert row["due_at"] == "2026-02-24T20:16:00Z"
    assert row["attempt"] == 3
    assert row["error"] == "no available orchestrator slots"
    assert doc["counts"] == {"running": 0, "retrying": 1}


def test_snapshot_is_json_serializable() -> None:
    state = make_state(
        running={"abc123": make_running()},
        retry={"d": RetryEntry("d", "MT-650", 1, MONO_MS + 1000, error="boom")},
        totals=CodexTotals(5000, 2400, 7400, 100.0),
    )
    state.codex_rate_limits = {"primary": {"used_percent": 12.5, "resets_at": NOW}}
    doc = snap.build_snapshot(state, now=NOW, monotonic_ms=MONO_MS)
    assert json.loads(json.dumps(doc))["rate_limits"]["primary"]["resets_at"] == (
        "2026-02-24T20:15:30Z"
    )


def test_snapshot_never_echoes_opaque_native_ref() -> None:
    """SPEC 15.3: opaque provider data stays out of observability surfaces."""
    issue = make_issue(native_ref={"installation_token": "ghs_LEAKED"})
    state = make_state(running={"abc123": make_running(issue=issue)})
    assert "ghs_LEAKED" not in json.dumps(snap.build_snapshot(state, now=NOW, monotonic_ms=MONO_MS))


def test_snapshot_coerces_non_string_event_values() -> None:
    """The snapshot is an RLM/HTTP read surface, so it must stay serializable."""
    session = LiveSession(session_id="s", last_codex_event=RunPhase.STREAMING_TURN)  # type: ignore[arg-type]
    state = make_state(running={"abc123": make_running(session=session)})
    row = snap.build_snapshot(state, now=NOW, monotonic_ms=MONO_MS)["running"][0]
    assert row["last_event"] == "StreamingTurn"
    json.dumps(row)


def test_snapshot_clips_long_agent_messages() -> None:
    session = LiveSession(session_id="s", last_codex_message="m" * 5000)
    state = make_state(running={"abc123": make_running(session=session)})
    row = snap.build_snapshot(state, now=NOW, monotonic_ms=MONO_MS)["running"][0]
    assert len(row["last_message"]) == snap.MAX_MESSAGE_CHARS + 3


def test_snapshot_does_not_mutate_state_and_copies_payloads() -> None:
    state = make_state(
        running={"abc123": make_running()}, totals=CodexTotals(1, 2, 3, 10.0)
    )
    state.codex_rate_limits = {"primary": {"used_percent": 12.5}}

    doc = snap.build_snapshot(state, now=NOW, monotonic_ms=MONO_MS)
    doc["rate_limits"]["primary"]["used_percent"] = 99
    doc["running"][0]["tokens"]["input_tokens"] = 99

    assert state.codex_rate_limits == {"primary": {"used_percent": 12.5}}
    assert state.codex_totals.seconds_running == 10.0
    assert state.running["abc123"].session.codex_input_tokens == 1200


# ==========================================================================
# SPEC 13.5 — live runtime aggregate
# ==========================================================================


def test_seconds_running_adds_active_elapsed_to_ended_total() -> None:
    """SPEC 13.5: cumulative ended-session runtime + active elapsed from started_at."""
    state = make_state(
        running={
            "abc123": make_running(started_at=NOW - timedelta(seconds=60)),
            "def456": make_running(
                issue=make_issue(id="def456", identifier="MT-650"),
                started_at=NOW - timedelta(seconds=30),
            ),
        },
        totals=CodexTotals(0, 0, 0, 1000.0),
    )
    doc = snap.build_snapshot(state, now=NOW, monotonic_ms=MONO_MS)
    assert doc["codex_totals"]["seconds_running"] == 1090.0
    assert snap.live_seconds_running(state, NOW) == 1090.0


def test_seconds_running_is_recomputed_on_every_read() -> None:
    """SPEC 13.5: a live aggregate at render time; no background ticking."""
    state = make_state(
        running={"abc123": make_running(started_at=NOW)}, totals=CodexTotals(seconds_running=5.0)
    )
    first = snap.build_snapshot(state, now=NOW, monotonic_ms=MONO_MS)
    later = snap.build_snapshot(state, now=NOW + timedelta(seconds=45), monotonic_ms=MONO_MS)

    assert first["codex_totals"]["seconds_running"] == 5.0
    assert later["codex_totals"]["seconds_running"] == 50.0
    assert state.codex_totals.seconds_running == 5.0, "the stored counter must not tick"


def test_seconds_running_with_no_active_sessions_is_the_stored_total() -> None:
    state = make_state(totals=CodexTotals(seconds_running=1834.2))
    doc = snap.build_snapshot(state, now=NOW, monotonic_ms=MONO_MS)
    assert doc["codex_totals"]["seconds_running"] == 1834.2


def test_backwards_clock_never_subtracts_runtime() -> None:
    state = make_state(
        running={"abc123": make_running(started_at=NOW)}, totals=CodexTotals(seconds_running=7.0)
    )
    doc = snap.build_snapshot(state, now=NOW - timedelta(seconds=90), monotonic_ms=MONO_MS)
    assert doc["codex_totals"]["seconds_running"] == 7.0


def test_naive_started_at_is_treated_as_utc() -> None:
    naive = NOW.replace(tzinfo=None) - timedelta(seconds=20)
    state = make_state(running={"abc123": make_running(started_at=naive)})
    assert snap.live_seconds_running(state, NOW) == 20.0


# ==========================================================================
# SPEC 13.7.2 — per-issue detail
# ==========================================================================


def test_issue_detail_for_running_issue() -> None:
    entry = make_running(
        retry_attempt=2,
        last_error=None,
        recent_events=[
            {
                "at": datetime(2026, 2, 24, 20, 14, 59, tzinfo=UTC),
                "event": "notification",
                "message": "Working on tests",
            }
        ],
    )
    detail = snap.build_issue_detail(
        make_state(running={"abc123": entry}), "MT-649", now=NOW, monotonic_ms=MONO_MS
    )
    assert detail is not None
    assert detail["issue_identifier"] == "MT-649"
    assert detail["issue_id"] == "abc123"
    assert detail["status"] == "running"
    assert detail["workspace"] == {"path": "/tmp/symphony_workspaces/MT-649"}
    assert detail["attempts"] == {"restart_count": 2, "current_retry_attempt": 2}
    assert detail["running"]["turn_count"] == 7
    assert detail["running"]["phase"] == "StreamingTurn"
    assert detail["retry"] is None
    assert detail["logs"] == {"codex_session_logs": []}
    assert detail["recent_events"] == [
        {"at": "2026-02-24T20:14:59Z", "event": "notification", "message": "Working on tests"}
    ]
    assert detail["tracked"] == {}
    json.dumps(detail)


def test_issue_detail_for_retrying_issue() -> None:
    retry = RetryEntry("def456", "MT-650", 3, MONO_MS + 30_000, error="worker exited: crash")
    detail = snap.build_issue_detail(
        make_state(retry={"def456": retry}), "MT-650", now=NOW, monotonic_ms=MONO_MS
    )
    assert detail is not None
    assert detail["status"] == "retrying"
    assert detail["running"] is None
    assert detail["retry"]["due_at"] == "2026-02-24T20:16:00Z"
    assert detail["last_error"] == "worker exited: crash"
    assert detail["attempts"]["current_retry_attempt"] == 3


def test_issue_detail_accepts_issue_id_and_folds_case() -> None:
    state = make_state(running={"abc123": make_running()})
    by_id = snap.build_issue_detail(state, "abc123", now=NOW, monotonic_ms=MONO_MS)
    by_case = snap.build_issue_detail(state, "mt-649", now=NOW, monotonic_ms=MONO_MS)
    assert by_id is not None and by_id["issue_identifier"] == "MT-649"
    assert by_case is not None and by_case["issue_id"] == "abc123"


def test_issue_detail_is_none_for_unknown_issue() -> None:
    """SPEC 13.7.2: unknown issues become 404 at the HTTP layer."""
    state = make_state(running={"abc123": make_running()})
    assert snap.build_issue_detail(state, "MT-999", now=NOW, monotonic_ms=MONO_MS) is None
    assert snap.build_issue_detail(state, "", now=NOW, monotonic_ms=MONO_MS) is None


def test_issue_detail_reports_completed_bookkeeping() -> None:
    state = make_state()
    state.completed.add("zzz999")
    detail = snap.build_issue_detail(state, "zzz999", now=NOW, monotonic_ms=MONO_MS)
    assert detail is not None
    assert detail["status"] == "completed"
    assert detail["running"] is None and detail["retry"] is None


def test_issue_detail_caps_recent_event_history() -> None:
    events = [{"event": "notification", "message": f"n{i}"} for i in range(60)]
    entry = make_running(recent_events=events)
    detail = snap.build_issue_detail(
        make_state(running={"abc123": entry}), "MT-649", now=NOW, monotonic_ms=MONO_MS
    )
    assert detail is not None
    assert len(detail["recent_events"]) == snap.MAX_RECENT_EVENTS
    assert detail["recent_events"][-1]["message"] == "n59"


# ==========================================================================
# SPEC 13.6 — humanized event summaries
# ==========================================================================


@pytest.mark.parametrize("name", humanize.KNOWN_EVENTS)
def test_every_spec_10_4_event_has_a_humanized_summary(name: str) -> None:
    """SPEC 17.6: summaries cover the key wrapper/agent event classes."""
    summary = humanize.humanize_event(name)
    assert summary and summary != name
    assert "_" not in summary
    assert summary[0].isupper()


def test_summaries_include_the_concise_reason() -> None:
    assert (
        humanize.humanize_event("turn_failed", {"reason": "response timeout"})
        == "Turn failed: response timeout"
    )
    assert (
        humanize.humanize_event("startup_failed", {"error": "codex not found"})
        == "Agent session failed to start: codex not found"
    )
    assert (
        humanize.humanize_event("unsupported_tool_call", {"name": "web.search"})
        == "Unsupported tool call: web.search"
    )
    assert (
        humanize.humanize_event("notification", {"message": "Working on tests"})
        == "Notification: Working on tests"
    )


def test_turn_completed_reports_token_usage() -> None:
    summary = humanize.humanize_event(
        "turn_completed",
        {"turn_number": 7, "usage": {"input_tokens": 1200, "output_tokens": 800}},
    )
    assert summary == "Turn 7 completed (tokens in=1200 out=800)"


def test_wrapper_protocol_events_are_readable() -> None:
    """SPEC 13.5 names wrapper events such as thread/tokenUsage/updated."""
    assert humanize.humanize_event("thread/tokenUsage/updated") == "Thread token usage updated"
    assert (
        humanize.humanize_event(
            "thread/tokenUsage/updated", {"total_token_usage": {"total_tokens": 2000}}
        )
        == "Thread token usage updated (tokens total=2000)"
    )


def test_unknown_and_empty_events_degrade_readably() -> None:
    assert humanize.humanize_event("some_new_event") == "Some new event"
    assert humanize.humanize_event(None) == "Unknown agent event"
    assert humanize.humanize_event("") == "Unknown agent event"
    assert humanize.humanize_event({}) == "Unknown agent event"


def test_humanize_accepts_event_objects_and_mappings() -> None:
    """Duck-typed against the SPEC 10.4 event shape; the agent module is not imported."""

    @dataclass(frozen=True, slots=True)
    class FakeAgentEvent:
        event: str
        timestamp: datetime
        usage: dict[str, Any] | None = None
        payload: dict[str, Any] = field(default_factory=dict)

    fake = FakeAgentEvent(
        event="turn_completed",
        timestamp=NOW,
        usage={"total_tokens": 99},
        payload={"turn_number": 2},
    )
    assert humanize.humanize_event(fake) == "Turn 2 completed (tokens total=99)"
    assert (
        humanize.humanize_event({"event": "notification", "payload": {"message": "hi"}})
        == "Notification: hi"
    )
    assert humanize.event_name({"event": "malformed"}) == "malformed"


def test_summaries_never_dump_payloads_or_secrets() -> None:
    """SPEC 13.1 / 15.3: summaries are not a payload escape hatch."""
    summary = humanize.humanize_event(
        "other_message",
        {
            "api_key": "ghp_SUPERSECRET",
            "raw": {"authorization": "Bearer ghp_SUPERSECRET"},
            "message": "m" * 5000,
        },
    )
    assert "ghp_SUPERSECRET" not in summary
    assert len(summary) <= humanize.MAX_SUMMARY_CHARS + 3


def test_summaries_collapse_newlines() -> None:
    summary = humanize.humanize_event("notification", {"message": "line one\nline two"})
    assert summary == "Notification: line one line two"


def test_humanize_events_preserves_order() -> None:
    assert humanize.humanize_events(["session_started", "turn_completed"]) == [
        "Agent session started",
        "Turn completed",
    ]


# ==========================================================================
# SPEC 13.4 — status surface
# ==========================================================================


def test_status_renders_running_and_retry_rows() -> None:
    state = make_state(
        running={"abc123": make_running(started_at=NOW - timedelta(seconds=318))},
        retry={"def456": RetryEntry("def456", "MT-650", 3, MONO_MS + 30_000, error="no slots")},
        totals=CodexTotals(5000, 2400, 7400, 1000.0),
    )
    text = status.render_status(state, now=NOW, monotonic_ms=MONO_MS)

    assert "generated_at=2026-02-24T20:15:30Z" in text
    assert "running=1/10" in text and "retrying=1" in text
    assert "MT-649" in text and "In Progress" in text and "turn_completed" in text
    assert "5m18s" in text
    assert "MT-650" in text and "30s" in text and "no slots" in text
    assert "total=7,400" in text
    assert "runtime=21m58s" in text  # 1000s banked + 318s live
    assert "RATE LIMITS  (none)" in text


def test_status_handles_empty_state() -> None:
    text = status.render_status(make_state(), now=NOW, monotonic_ms=MONO_MS)
    assert "RUNNING (0)" in text
    assert "RETRYING (0)" in text
    assert text.count("(none)") >= 2


def test_status_tolerates_a_half_populated_session() -> None:
    """SPEC 13.4: the status surface MUST NOT be required for correctness."""
    entry = make_running(session=LiveSession())
    entry.workspace_path = None
    text = status.render_status(
        make_state(running={"abc123": entry}), now=NOW, monotonic_ms=MONO_MS
    )
    assert "MT-649" in text


def test_status_is_read_only() -> None:
    state = make_state(
        running={"abc123": make_running()}, totals=CodexTotals(1, 2, 3, 4.0)
    )
    before = (state.codex_totals.seconds_running, len(state.running), len(state.claimed))
    status.render_status(state, now=NOW, monotonic_ms=MONO_MS)
    assert (state.codex_totals.seconds_running, len(state.running), len(state.claimed)) == before


def test_print_status_writes_to_the_given_stream() -> None:
    buffer = io.StringIO()
    status.print_status(make_state(), stream=buffer, now=NOW, monotonic_ms=MONO_MS)
    assert buffer.getvalue().startswith("Symphony status")
    assert buffer.getvalue().endswith("\n")


def test_status_renders_rate_limits_when_present() -> None:
    state = make_state()
    state.codex_rate_limits = {"primary_used_percent": 12.5}
    assert "primary_used_percent=12.5" in status.render_status(
        state, now=NOW, monotonic_ms=MONO_MS
    )


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "-"),
        (0, "0s"),
        (45, "45s"),
        (60, "1m00s"),
        (1834.2, "30m34s"),
        (8000, "2h13m"),
        (-5, "0s"),
    ],
)
def test_format_duration(seconds: float | None, expected: str) -> None:
    assert status.format_duration(seconds) == expected


def test_format_tokens() -> None:
    assert status.format_tokens(7400) == "7,400"
    assert status.format_tokens(None) == "-"
