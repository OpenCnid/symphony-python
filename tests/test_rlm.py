"""Tests for the RLM addressability surface (``symphony.rlm``).

These exercise the three properties the surface actually promises — bounded
size, bounded recursion, and graceful absence — rather than restating shapes.
Each test is written so that removing the behavior it covers makes it fail.
"""

from __future__ import annotations

import importlib
import inspect
import json
import math
from datetime import UTC, datetime
from pathlib import Path

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
from symphony.rlm import (
    BudgetExceeded,
    RecursionBudget,
    ReplEnv,
    as_text,
    chunk,
    components_for_spec,
    describe_component,
    describe_object,
    describe_state,
    estimate_tokens,
    find_symbol,
    grep,
    jsonable,
    keys_of,
    local_sub_model,
    measure_json,
    open_repl,
    peek,
    plan_recursion,
    recursive_query,
    resolve_component,
    size,
    system_map,
    window,
)
from symphony.rlm import introspect as introspect_mod
from symphony.rlm.introspect import ComponentSpec

# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
def fake_registry(monkeypatch):
    """Replace the component registry so absence tests are deterministic.

    Sibling modules are landing on disk while this suite runs, so a test that
    asserts "module X is missing" against the real registry would be a race.
    """

    def apply(*components: ComponentSpec) -> None:
        monkeypatch.setattr(introspect_mod, "REGISTRY", tuple(components))
        monkeypatch.setattr(introspect_mod, "_BY_MODULE", {c.module: c for c in components})

    return apply


def strict_json(payload) -> str:
    """Serialize with no ``default=`` hook — proves the payload is JSON-safe."""
    return json.dumps(payload, ensure_ascii=False)


def sample_state() -> OrchestratorState:
    issue = Issue(
        id="i-1",
        identifier="ENG-101",
        title="Wire up retry backoff",
        state="In Progress",
        dispatchable=True,
        labels=("agent",),
        url="https://example.invalid/ENG-101",
    )
    return OrchestratorState(
        running={
            "i-1": RunningEntry(
                issue=issue,
                identifier="ENG-101",
                started_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
                session=LiveSession(session_id="t1-u1", turn_count=3),
                phase=RunPhase.STREAMING_TURN,
                workspace_path="/tmp/ws/ENG-101",
                recent_events=[{"event": "thread/started"}],
            )
        },
        claimed={"i-1", "i-2"},
        retry_attempts={
            "i-9": RetryEntry(issue_id="i-9", identifier="ENG-9", attempt=2, due_at_ms=1234.0)
        },
        completed={"i-3"},
        codex_totals=CodexTotals(input_tokens=10, output_tokens=20, total_tokens=30),
    )


PARAGRAPHS = "".join(f"section {i}: alpha beta gamma delta\nline two of {i}\n\n" for i in range(60))


# --------------------------------------------------------------------------
# Introspection — JSON safety and exact self-measurement
# --------------------------------------------------------------------------


def test_system_map_is_json_safe_and_reports_its_own_exact_size():
    result = system_map(depth=0)
    assert measure_json(result) == result["_meta"]["chars"]
    assert len(strict_json(result)) == result["_meta"]["chars"]
    assert result["_meta"]["truncated"] is False


def test_system_map_covers_every_registry_component_with_a_presence_verdict():
    result = system_map(depth=0, max_chars=None)
    listed = {c["module"] for c in result["components"]}
    assert listed == {c.module for c in introspect_mod.REGISTRY}
    for entry in result["components"]:
        assert entry["present"] in (True, False)
    counts = result["counts"]
    assert counts["present"] + counts["absent"] + counts["import_failed"] == counts["components"]


def test_depth_ladder_is_strictly_more_expensive_at_each_level():
    sizes = [
        describe_component("symphony.models", depth=d, max_chars=None)["_meta"]["chars"]
        for d in (0, 1, 2, 3)
    ]
    assert sizes == sorted(sizes)
    assert sizes[0] < sizes[-1]


def test_max_chars_is_enforced_by_shrinking_not_by_hoping():
    unbounded = system_map(depth=2, max_chars=None)
    assert unbounded["_meta"]["chars"] > 4_000
    bounded = system_map(depth=2, max_chars=4_000)
    assert bounded["_meta"]["truncated"] is True
    assert bounded["_meta"]["chars"] <= 4_000
    assert measure_json(bounded) == bounded["_meta"]["chars"]


def test_shrinking_never_damages_addressability():
    bounded = system_map(depth=2, max_chars=3_000)
    assert bounded["_meta"]["truncated"] is True
    for entry in bounded["components"]:
        if "module" not in entry:
            continue  # the elision marker appended by the list clamp
        assert entry["module"] in {c.module for c in introspect_mod.REGISTRY}
        assert not entry["module"].endswith("...")


def test_system_map_is_stable_across_calls():
    first = system_map(depth=1)
    second = system_map(depth=1)
    assert first == second


def test_absent_component_is_data_not_an_exception(fake_registry):
    fake_registry(
        ComponentSpec("symphony.orchestrator.core", "orchestrator", "poll loop", ("7", "8.1")),
        ComponentSpec("symphony.not_written_yet", "phantom", "not on disk", ("99",)),
    )
    result = describe_component("symphony.not_written_yet")
    assert result["present"] is False
    assert result["importable"] is False
    assert result["reason"] == "not_present"
    assert result["expected_path"] == "src/symphony/not_written_yet.py"
    assert result["spec_sections"] == ["99"]
    assert "CONTRACTS.md" in result["contract"]
    strict_json(result)

    mapped = system_map(depth=1, max_chars=None)
    assert mapped["counts"] == {
        "components": 2,
        "present": 1,
        "absent": 1,
        "import_failed": 0,
    }


def test_import_failure_is_reported_as_data(tmp_path, monkeypatch, fake_registry):
    package = tmp_path / "rlm_probe_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "broken.py").write_text("raise RuntimeError('boom at import')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    fake_registry(ComponentSpec("rlm_probe_pkg.broken", "probe", "broken on purpose"))

    result = describe_component("rlm_probe_pkg.broken")
    assert result["present"] is True
    assert result["importable"] is False
    assert result["reason"] == "import_failed"
    assert result["error"]["type"] == "RuntimeError"
    assert "boom at import" in result["error"]["message"]


def test_unknown_and_ambiguous_names_resolve_without_raising():
    unknown = describe_component("symphony.utterly_unknown_thing")
    assert unknown["reason"] == "unknown_component"

    assert resolve_component("loader").module == "symphony.workflow.loader"
    assert resolve_component("symphony.workflow.template").module == "symphony.workflow.template"
    assert resolve_component("core").module == "symphony.orchestrator.core"
    candidates = resolve_component("http")
    assert isinstance(candidates, list)
    assert len(candidates) > 1
    assert describe_component("http")["reason"] == "ambiguous"


def test_describe_component_reports_signatures_and_class_internals():
    detail = describe_component("symphony.models", depth=3, max_chars=None)
    by_symbol = {s["symbol"]: s for s in detail["symbols"]}

    issue = by_symbol["Issue"]
    assert issue["kind"] == "dataclass"
    field_names = {f["name"] for f in issue["fields"]}
    assert {"id", "identifier", "title", "state", "dispatchable"} <= field_names
    assert any(m["symbol"] == "has_labels" for m in issue["methods"])

    phase = by_symbol["RunPhase"]
    assert phase["kind"] == "enum"
    assert phase["members"]["SUCCEEDED"] == "Succeeded"

    fn = by_symbol["workspace_key"]
    assert fn["kind"] == "function"
    assert fn["signature"] == "(identifier: 'str') -> 'str'"
    assert fn["doc"].startswith("Derive a collision-resistant workspace directory name")


def test_components_for_spec_maps_sections_to_owners():
    assert "symphony.observability.snapshot" in components_for_spec("13.3")
    owners_of_16 = components_for_spec("16")
    assert "symphony.orchestrator.core" in owners_of_16
    assert "symphony.agent.runner" in owners_of_16
    assert components_for_spec("") == []
    assert components_for_spec("99.9") == []


def test_find_symbol_locates_a_public_name_across_present_modules():
    result = find_symbol("^workspace_key$")
    hits = {(m["module"], m["symbol"]) for m in result["matches"]}
    assert ("symphony.models", "workspace_key") in hits
    assert result["counts"]["modules_scanned"] >= 1
    assert find_symbol("([")["reason"] == "bad_pattern"


def test_describe_state_indexes_runtime_without_duplicating_spec_13_3():
    state = sample_state()

    cheap = describe_state(state, depth=0, max_chars=None)
    assert cheap["counts"] == {"running": 1, "claimed": 2, "retry_queued": 1, "completed": 1}
    assert cheap["codex_totals"]["total_tokens"] == 30
    assert "build_snapshot" in cheap["note"]
    assert "running" not in cheap

    rows = describe_state(state, depth=1, max_chars=None)
    assert rows["running"][0]["identifier"] == "ENG-101"
    assert rows["running"][0]["phase"] == "StreamingTurn"
    assert rows["running"][0]["turn_count"] == 3
    assert "recent_events" not in rows["running"][0]
    assert rows["retrying"][0]["attempt"] == 2

    deep = describe_state(state, depth=2, max_chars=None)
    assert deep["running"][0]["recent_events"] == [{"event": "thread/started"}]
    strict_json(deep)


def test_describe_state_falls_back_to_object_description():
    result = describe_state({"a": 1, "b": 2}, max_chars=None)
    assert result["kind"] == "object"
    assert result["len"] == 2


def test_describe_object_handles_live_instances_and_classes():
    state = sample_state()
    entry = describe_object(state.running["i-1"], depth=2, max_chars=None)
    assert entry["type"] == "RunningEntry"
    assert entry["values"]["identifier"] == "ENG-101"

    klass = describe_object(RunPhase, depth=2, max_chars=None)
    assert klass["members"]["FAILED"] == "Failed"


def test_jsonable_survives_hostile_values():
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    payload = {
        "when": datetime(2026, 7, 28, tzinfo=UTC),
        "phase": RunPhase.FAILED,
        "path": Path("a/b"),
        "unique": {3, 1, 2},
        "nan": math.nan,
        "raw": b"\xff\xfe",
        "cyclic": cyclic,
        "callable": sample_state,
    }
    coerced = jsonable(payload)
    strict_json(coerced)
    assert coerced["phase"] == "Failed"
    assert coerced["nan"] == "nan"
    assert coerced["raw"] == {"__bytes__": 2}


def test_refresh_reopens_discovery_after_a_module_lands(tmp_path, monkeypatch, fake_registry):
    package = tmp_path / "rlm_late_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    fake_registry(ComponentSpec("rlm_late_pkg.arrives", "probe", "lands mid-session"))

    introspect_mod.refresh()
    assert describe_component("rlm_late_pkg.arrives")["present"] is False

    (package / "arrives.py").write_text('"""Landed."""\nVALUE = 1\n', encoding="utf-8")
    introspect_mod.refresh()
    landed = describe_component("rlm_late_pkg.arrives", depth=1)
    assert landed["present"] is True
    assert landed["summary"] == "Landed."


# --------------------------------------------------------------------------
# REPL primitives
# --------------------------------------------------------------------------


def test_size_prices_a_payload_in_chars_and_tokens():
    measured = size("abcdefgh")
    assert measured == {"type": "str", "chars": 8, "tokens": 2, "lines": 1, "items": 8}
    assert size(None)["chars"] == 0
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcde") == 2


def test_as_text_keeps_strings_verbatim_so_offsets_stay_valid():
    raw = "line one\nline two"
    assert as_text(raw) is raw
    rendered = as_text({"b": 1, "a": [1, 2]})
    assert json.loads(rendered) == {"b": 1, "a": [1, 2]}


def test_peek_and_window_address_a_payload_by_position():
    text = "0123456789"
    assert peek(text, 4) == "0123"
    assert peek(text, 4, tail=True) == "6789"
    assert peek(text, 99) == text
    assert window(text, 2, 5) == "234"
    assert window([10, 20, 30, 40], 1, 3) == [20, 30]
    assert window({"a": 1, "b": 2, "c": 3}, 1, 3) == {"b": 2, "c": 3}


def test_keys_of_exposes_addressable_names():
    assert keys_of({"x": 1, "y": 2}) == ["x", "y"]
    assert "identifier" in keys_of(sample_state().running["i-1"])


def test_chunks_are_budget_bounded_and_reassemble_exactly():
    chunks = chunk(PARAGRAPHS, max_chars=500)
    assert chunks
    assert max(c.chars for c in chunks) <= 500
    assert "".join(c.text for c in chunks) == PARAGRAPHS
    assert all(PARAGRAPHS[c.start : c.end] == c.text for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_chunking_prefers_natural_boundaries_over_hard_cuts():
    text = "alpha alpha\n\nbeta beta\n\ngamma gamma\n\n"
    chunks = chunk(text, max_chars=16)
    assert len(chunks) == 3
    assert [c.text for c in chunks] == ["alpha alpha\n\n", "beta beta\n\n", "gamma gamma\n\n"]


def test_chunking_falls_through_to_a_hard_slice_for_opaque_blobs():
    blob = "x" * 1000
    chunks = chunk(blob, max_chars=128)
    assert len(chunks) == 8
    assert [c.chars for c in chunks] == [128] * 7 + [104]
    assert "".join(c.text for c in chunks) == blob


def test_overlap_stays_inside_the_budget_and_remains_addressable():
    chunks = chunk(PARAGRAPHS, max_chars=400, overlap=80)
    assert max(c.chars for c in chunks) <= 400
    assert all(PARAGRAPHS[c.start : c.end] == c.text for c in chunks)
    assert chunks[1].start < chunks[0].end
    assert chunk("", max_chars=10) == []
    with pytest.raises(ValueError):
        chunk("abc", max_chars=0)


def test_grep_returns_line_addressed_hits_with_context():
    text = "alpha\nbeta\ngamma\nbeta again\ndelta"
    hits = grep(text, "beta", context=1)
    assert [h["line"] for h in hits] == [2, 4]
    assert hits[0]["before"] == ["alpha"]
    assert hits[0]["after"] == ["gamma"]
    assert len(grep(text, "a", max_hits=2)) == 2
    assert grep(text, "BETA", ignore_case=False) == []
    assert grep(text, "b.ta", regex=False) == []


# --------------------------------------------------------------------------
# REPL environment
# --------------------------------------------------------------------------


def test_repl_binds_the_payload_and_captures_the_last_expression():
    env = ReplEnv(context=PARAGRAPHS)
    result = env.run("size(ctx)")
    assert result.ok is True
    assert result.value["chars"] == len(PARAGRAPHS)
    assert result.value_type == "dict"
    assert env.context is PARAGRAPHS


def test_repl_captures_stdout_and_statement_only_snippets():
    env = ReplEnv()
    result = env.run("total = 2 + 3\nprint('total', total)")
    assert result.ok is True
    assert result.stdout == "total 5\n"
    assert result.value is None
    assert env.namespace["total"] == 5


def test_repl_contains_failures_including_systemexit():
    env = ReplEnv()
    boom = env.run("1 / 0")
    assert boom.ok is False
    assert boom.error["type"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in boom.error["traceback"]

    syntax = env.run("def (")
    assert syntax.ok is False
    assert syntax.error["type"] == "SyntaxError"

    exiting = env.run("raise SystemExit(3)")
    assert exiting.ok is False
    assert exiting.error["type"] == "SystemExit"

    assert env.run("'still alive'").value == "still alive"


def test_repl_caps_runaway_output():
    env = ReplEnv(max_output_chars=64)
    result = env.run("print('x' * 5000)")
    assert result.output_truncated is True
    assert len(result.stdout) < 200


def test_repl_history_is_replayable():
    env = ReplEnv(context="seed")
    env.run("acc = []")
    env.run("acc.append(len(ctx))")
    env.run("sum(acc)")
    assert env.codes() == ["acc = []", "acc.append(len(ctx))", "sum(acc)"]
    assert env.last.value == 4

    clone = env.replay()
    assert clone.codes() == env.codes()
    assert clone.last.value == 4
    assert clone is not env

    rebound = env.replay(context="a longer seed")
    assert rebound.last.value == len("a longer seed")


def test_repl_namespace_is_a_complete_action_space():
    env = open_repl(context="hello", state=sample_state())
    bindings = set(env.bindings())
    assert {"ctx", "state", "size", "chunk", "grep", "peek", "window"} <= bindings
    assert {"system_map", "describe_component", "describe_state"} <= bindings
    assert {"recursive_query", "RecursionBudget", "plan_recursion"} <= bindings
    assert env.run("describe_state(state)['counts']['running']").value == 1
    with pytest.raises(ValueError):
        env.bind("not an identifier", 1)


def test_exec_result_and_env_summaries_are_json_safe():
    env = ReplEnv(context=sample_state())
    result = env.run("ctx.running['i-1']")
    strict_json(result.to_dict())
    assert result.to_dict()["value_preview"]["identifier"] == "ENG-101"
    strict_json(env.to_dict())
    assert env.to_dict()["history_len"] == 1
    assert ">>> [0]" in env.transcript()


# --------------------------------------------------------------------------
# Recursion
# --------------------------------------------------------------------------


def make_recorder():
    calls: list[tuple[str, str]] = []

    def recorder(query: str, context: str) -> str:
        calls.append((query, context))
        return f"<{len(context)}>"

    return recorder, calls


def test_sub_model_is_injected_and_never_sees_more_than_the_leaf_budget():
    recorder, calls = make_recorder()
    budget = RecursionBudget(max_chunk_tokens=32, max_calls=500)
    result = recursive_query(PARAGRAPHS, "gamma", sub_model=recorder, budget=budget)

    assert calls, "the injected sub-model must actually be called"
    assert all(q == "gamma" for q, _ in calls)
    assert max(len(ctx) for _, ctx in calls) <= budget.max_chunk_chars
    assert result.calls == len(calls)
    assert result.partial is False
    assert result.answer


def test_default_sub_model_is_offline_and_deterministic():
    first = recursive_query(PARAGRAPHS, "section 7", budget=RecursionBudget(max_chunk_tokens=128))
    second = recursive_query(PARAGRAPHS, "section 7", budget=RecursionBudget(max_chunk_tokens=128))
    assert first.answer == second.answer
    assert first.calls == second.calls
    assert local_sub_model("beta", "alpha\nbeta here\n") == "[1/2 lines] beta here"
    assert local_sub_model("nothing", "alpha\n") == "[0/1 lines]"


def test_max_calls_is_enforced_and_recorded():
    recorder, calls = make_recorder()
    budget = RecursionBudget(max_chunk_tokens=16, max_calls=5)
    result = recursive_query(PARAGRAPHS, "gamma", sub_model=recorder, budget=budget)

    assert len(calls) <= 5
    assert result.calls == len(calls)
    assert result.partial is True
    assert "max_calls" in result.stops


def test_max_total_tokens_is_enforced():
    recorder, calls = make_recorder()
    budget = RecursionBudget(max_chunk_tokens=32, max_calls=10_000, max_total_tokens=100)
    result = recursive_query(PARAGRAPHS, "gamma", sub_model=recorder, budget=budget)

    assert result.tokens <= 100
    assert sum(estimate_tokens(ctx) for _, ctx in calls) <= 100
    assert "max_total_tokens" in result.stops


def test_max_depth_truncates_the_leaf_rather_than_silently_overflowing():
    recorder, calls = make_recorder()
    budget = RecursionBudget(max_depth=0, max_chunk_tokens=16, max_calls=50)
    result = recursive_query(PARAGRAPHS, "gamma", sub_model=recorder, budget=budget)

    assert len(calls) == 1
    assert calls[0][1] == PARAGRAPHS[: budget.max_chunk_chars]
    assert result.depth_reached == 0
    assert "max_depth_truncated" in result.stops
    assert result.partial is True


def test_strict_budgets_raise_instead_of_degrading():
    budget = RecursionBudget(max_depth=0, max_chunk_tokens=16, on_exceeded="raise")
    with pytest.raises(BudgetExceeded) as excinfo:
        recursive_query(PARAGRAPHS, "gamma", budget=budget)
    payload = excinfo.value.to_dict()
    assert payload["category"] == "rlm_budget_exceeded"
    assert payload["details"]["reason"] == "max_depth_truncated"


def test_recursion_actually_descends_more_than_one_level():
    recorder, _ = make_recorder()
    budget = RecursionBudget(max_chunk_tokens=16, fanout=2, max_calls=500)
    result = recursive_query(PARAGRAPHS, "gamma", sub_model=recorder, budget=budget)
    assert result.depth_reached >= 2
    assert result.chunks > 2
    kinds = {entry["kind"] for entry in result.trace}
    assert "split" in kinds
    assert "leaf" in kinds


def test_plan_predicts_the_call_count_before_paying_for_it():
    budget = RecursionBudget(max_chunk_tokens=128, max_calls=500)
    plan = plan_recursion(PARAGRAPHS, budget=budget)
    assert plan["fits_budget"] is True

    recorder, calls = make_recorder()
    result = recursive_query(PARAGRAPHS, "gamma", sub_model=recorder, budget=budget)
    assert plan["estimated_calls"] <= len(calls) <= plan["max_calls_upper_bound"]
    assert result.partial is False
    strict_json(plan)


def test_plan_flags_a_budget_that_would_bind():
    plan = plan_recursion(PARAGRAPHS, budget=RecursionBudget(max_chunk_tokens=8, max_calls=4))
    assert plan["fits_budget"] is False
    assert "max_calls" in plan["would_stop_on"]


def test_empty_payload_costs_nothing():
    recorder, calls = make_recorder()
    result = recursive_query("", "gamma", sub_model=recorder)
    assert calls == []
    assert result.calls == 0
    assert result.answer == ""
    assert result.partial is False


def test_sub_model_failures_propagate_rather_than_becoming_partial_answers():
    def broken(query: str, context: str) -> str:
        raise RuntimeError("model client is down")

    with pytest.raises(RuntimeError, match="model client is down"):
        recursive_query(PARAGRAPHS, "gamma", sub_model=broken)


def test_recursive_result_is_json_safe_and_fully_accounted():
    result = recursive_query(PARAGRAPHS, "gamma", budget=RecursionBudget(max_chunk_tokens=64))
    payload = result.to_dict()
    strict_json(payload)
    assert payload["budget"]["max_chunk_chars"] == 64 * 4
    assert payload["calls"] == result.calls
    assert isinstance(payload["trace"], list)


def test_custom_combiner_shapes_the_merge():
    def combine(query: str, answers, depth: int) -> str:
        return f"({depth}:" + ",".join(answers) + ")"

    result = recursive_query(
        PARAGRAPHS,
        "gamma",
        sub_model=lambda q, c: "A",
        budget=RecursionBudget(max_chunk_tokens=64, max_calls=500),
        combine=combine,
    )
    assert result.answer.startswith("(0:")


def test_budget_validation_rejects_incoherent_bounds():
    for kwargs in (
        {"max_depth": -1},
        {"max_chunk_tokens": 0},
        {"max_calls": 0},
        {"max_total_tokens": 0},
        {"fanout": 1},
        {"overlap_tokens": -1},
        {"on_exceeded": "explode"},
    ):
        with pytest.raises(ValueError):
            RecursionBudget(**kwargs)


def test_recursive_query_works_on_structured_payloads_not_just_text():
    recorder, calls = make_recorder()
    payload = [{"issue": f"ENG-{i}", "state": "In Progress"} for i in range(200)]
    result = recursive_query(
        payload, "ENG-42", sub_model=recorder, budget=RecursionBudget(max_chunk_tokens=64)
    )
    assert calls
    assert "ENG-42" in "".join(ctx for _, ctx in calls)
    assert result.calls == len(calls)


# --------------------------------------------------------------------------
# Package-level contract
# --------------------------------------------------------------------------


def test_every_exported_callable_documents_itself():
    package = importlib.import_module("symphony.rlm")
    undocumented = []
    for name in package.__all__:
        obj = getattr(package, name)
        if not (inspect.isfunction(obj) or inspect.isclass(obj)):
            continue
        if not (inspect.getdoc(obj) or "").strip():
            undocumented.append(name)
    assert undocumented == []


def test_the_surface_does_not_shadow_spec_mandated_modules():
    """The RLM package observes; it must not re-export a spec module's names."""
    package = importlib.import_module("symphony.rlm")
    models = importlib.import_module("symphony.models")
    overlap = set(package.__all__) & set(models.__all__)
    assert overlap == set()
