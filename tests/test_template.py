"""Conformance tests for strict prompt rendering (SPEC 5.4, 12, 17.1).

These tests import only the already-written immutable modules
(``symphony.models``, ``symphony.errors``) and the module under test. Nothing
here depends on a sibling module that is being written concurrently: the
repo-root ``WORKFLOW.md`` front matter is split by a local helper rather than
by ``symphony.workflow.loader``.

The strictness assertions are behavioral on purpose. Asserting that the
environment was *configured* with ``StrictUndefined`` would pass even if the
library changed what that class does; asserting that an undefined variable
raises is the thing that would actually fail on a regression.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from symphony.errors import SymphonyError, TemplateParseError, TemplateRenderError
from symphony.models import BlockerRef, Issue
from symphony.workflow.template import (
    DEFAULT_PROMPT,
    TEMPLATE_VARIABLES,
    build_template_context,
    render_continuation_prompt,
    render_prompt,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_issue(**overrides: object) -> Issue:
    """A fully populated issue; every optional field is set unless overridden."""
    fields: dict[str, object] = {
        "id": "gid://item/1",
        "identifier": "ABC-123",
        "title": "Make the widget idempotent",
        "state": "In Progress",
        "dispatchable": True,
        "native_ref": {"repo": "acme/widget", "number": 123},
        "description": "The widget double-fires on retry.",
        "priority": 2,
        "branch_name": "abc-123-idempotent-widget",
        "url": "https://tracker.example/ABC-123",
        "assignee_id": "user-7",
        "labels": ("backend", "bug"),
        "blocked_by": (BlockerRef(id="gid://item/9", identifier="ABC-99", state="In Review"),),
        "created_at": datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 27, 9, 30, tzinfo=UTC),
    }
    fields.update(overrides)
    return Issue(**fields)  # type: ignore[arg-type]


def split_front_matter(text: str) -> str:
    """Return the Markdown body of a WORKFLOW.md (SPEC 5.2).

    Local on purpose — ``symphony.workflow.loader`` is owned by another author
    and may not exist yet, and this suite must not depend on it.
    """
    if not text.startswith("---"):
        return text
    _, _, rest = text.partition("\n")
    _, sep, body = rest.partition("\n---")
    if not sep:
        return text
    return body.partition("\n")[2]


# --------------------------------------------------------------------------
# SPEC 5.4 / 12.1-12.2 — the happy path
# --------------------------------------------------------------------------


def test_renders_issue_and_attempt() -> None:
    """SPEC 17.1: prompt template renders ``issue`` and ``attempt``."""
    out = render_prompt(
        "{{ issue.identifier }}|{{ issue.title }}|{{ issue.state }}|{{ attempt }}",
        make_issue(),
        3,
    )
    assert out == "ABC-123|Make the widget idempotent|In Progress|3"


def test_attempt_is_none_on_first_run() -> None:
    """SPEC 12.3: ``attempt`` is null/absent on the first run."""
    issue = make_issue()
    assert render_prompt("{% if attempt %}retry{% else %}first{% endif %}", issue, None) == "first"
    assert render_prompt("{% if attempt %}retry{% else %}first{% endif %}", issue, 1) == "retry"


def test_attempt_none_is_defined_not_missing() -> None:
    """``attempt=None`` must be a *defined* falsy value, not an absent key.

    Under strict mode an absent key raises, so omitting it would make the
    documented ``{% if attempt %}`` idiom explode on every first run.
    """
    assert render_prompt("[{{ attempt }}]", make_issue(), None) == "[]"


def test_nested_collections_are_iterable() -> None:
    """SPEC 12.2: preserve nested arrays/maps so templates can iterate."""
    template = (
        "{% for l in issue.labels %}{{ l }};{% endfor %}"
        "{% for b in issue.blocked_by %}{{ b.identifier }}={{ b.state }}{% endfor %}"
        "|{{ issue.labels.size }}"
    )
    assert render_prompt(template, make_issue(), None) == "backend;bug;ABC-99=In Review|2"


def test_native_ref_map_is_addressable() -> None:
    """SPEC 4.1.1: ``native_ref`` is preserved for prompt context."""
    assert render_prompt("{{ issue.native_ref.repo }}", make_issue(), None) == "acme/widget"


def test_context_keys_are_strings_and_exhaustive() -> None:
    """SPEC 12.2: issue object keys are converted to strings for the template."""
    context = build_template_context(make_issue(), None)
    assert set(context) == set(TEMPLATE_VARIABLES)
    assert all(isinstance(key, str) for key in context["issue"])


def test_null_fields_render_as_empty_and_work_with_default_filter() -> None:
    """A null description is *defined*, so ``default`` supplies the fallback."""
    issue = make_issue(description=None, priority=None, url=None)
    template = '{{ issue.description | default: "(none)" }}|{{ issue.url }}'
    assert render_prompt(template, issue, None) == "(none)|"


def test_standard_filters_are_available() -> None:
    """Strictness must not have been bought by stripping the standard filter set."""
    out = render_prompt(
        "{{ issue.identifier | downcase }}|{{ issue.labels | join: ',' }}"
        "|{{ issue.title | size }}|{{ issue.priority | plus: 1 }}",
        make_issue(),
        None,
    )
    assert out == "abc-123|backend,bug|26|3"


# --------------------------------------------------------------------------
# SPEC 5.4 / 17.1 — unknown variables MUST fail rendering
# --------------------------------------------------------------------------


def test_unknown_top_level_variable_raises_render_error() -> None:
    """SPEC 17.1: prompt rendering fails on unknown variables (strict mode)."""
    with pytest.raises(TemplateRenderError) as exc:
        render_prompt("Hello {{ workflow_name }}", make_issue(), None)
    assert "workflow_name" in exc.value.message


def test_unknown_issue_field_raises_render_error() -> None:
    with pytest.raises(TemplateRenderError):
        render_prompt("{{ issue.estimate }}", make_issue(), None)


def test_unknown_variable_inside_if_raises() -> None:
    """The trap case: many engines treat an undefined name in a conditional as falsy."""
    with pytest.raises(TemplateRenderError):
        render_prompt("{% if sprint %}yes{% endif %}", make_issue(), None)


def test_unknown_variable_inside_for_raises() -> None:
    with pytest.raises(TemplateRenderError):
        render_prompt("{% for r in reviewers %}{{ r }}{% endfor %}", make_issue(), None)


def test_unknown_variable_does_not_render_empty() -> None:
    """The whole reason strictness matters: silent emptiness must be impossible."""
    with pytest.raises(TemplateRenderError):
        render_prompt("Context: {{ nope }}", make_issue(), None)


def test_unknown_loop_variable_after_loop_raises() -> None:
    """Loop-local names must not leak into the outer scope as defined-but-empty."""
    with pytest.raises(TemplateRenderError):
        render_prompt("{% for l in issue.labels %}{% endfor %}{{ l }}", make_issue(), None)


# --------------------------------------------------------------------------
# SPEC 5.4 / 17.1 — unknown filters MUST fail rendering
# --------------------------------------------------------------------------


def test_unknown_filter_raises_render_error() -> None:
    with pytest.raises(TemplateRenderError) as exc:
        render_prompt("{{ issue.title | shoutify }}", make_issue(), None)
    assert "shoutify" in exc.value.message


def test_unknown_filter_does_not_pass_value_through() -> None:
    """A permissive engine returns the unfiltered value; that must not happen."""
    with pytest.raises(TemplateRenderError):
        render_prompt("{{ issue.identifier | no_such_filter }}", make_issue(), None)


def test_unknown_filter_in_assign_tag_raises() -> None:
    """Strictness must hold in tag expressions, not just output statements."""
    with pytest.raises(TemplateRenderError):
        render_prompt("{% assign t = issue.title | bogus %}{{ t }}", make_issue(), None)


def test_bad_filter_argument_raises_render_error() -> None:
    with pytest.raises(TemplateRenderError):
        render_prompt("{{ issue.title | slice }}", make_issue(), None)


# --------------------------------------------------------------------------
# SPEC 5.5 — parse failures and render failures are different classes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "{% if issue.title %}unterminated",
        "{% endfor %}",
        "{{ issue.title ",
        "{% bogus_tag %}",
        "{% for x in %}{% endfor %}",
    ],
)
def test_malformed_template_raises_parse_error(template: str) -> None:
    """SPEC 5.5: a malformed template is ``template_parse_error``."""
    with pytest.raises(TemplateParseError):
        render_prompt(template, make_issue(), None)


def test_parse_error_is_not_a_render_error() -> None:
    """The two classes are siblings, not a hierarchy; callers may branch on them."""
    with pytest.raises(TemplateParseError) as exc:
        render_prompt("{% endif %}", make_issue(), None)
    assert not isinstance(exc.value, TemplateRenderError)


def test_render_error_is_not_a_parse_error() -> None:
    with pytest.raises(TemplateRenderError) as exc:
        render_prompt("{{ ghost }}", make_issue(), None)
    assert not isinstance(exc.value, TemplateParseError)


def test_parse_failure_precedes_render_failure() -> None:
    """Syntax is checked before data, so a template that is both is a parse error."""
    with pytest.raises(TemplateParseError):
        render_prompt("{{ ghost }}{% endfor %}", make_issue(), None)


def test_error_categories_are_the_spec_slugs() -> None:
    """SPEC 5.5 names these categories; operators and the HTTP API depend on them."""
    with pytest.raises(TemplateParseError) as parse_exc:
        render_prompt("{% endunless %}", make_issue(), None)
    with pytest.raises(TemplateRenderError) as render_exc:
        render_prompt("{{ ghost }}", make_issue(), None)
    assert parse_exc.value.category == "template_parse_error"
    assert render_exc.value.category == "template_render_error"


def test_errors_carry_issue_context_but_leak_no_rendered_values() -> None:
    """SPEC 15.3: details are structured context, never interpolated content."""
    issue = make_issue(description="token=super-secret-value")
    with pytest.raises(TemplateRenderError) as exc:
        render_prompt("{{ issue.description }}{{ ghost }}", issue, 2)
    details = exc.value.to_dict()
    assert details["details"]["issue_identifier"] == "ABC-123"
    assert details["details"]["attempt"] == 2
    assert "super-secret-value" not in repr(details)


def test_no_raw_liquid_exception_escapes() -> None:
    """Every failure mode surfaces as a SymphonyError, never a library error."""
    for template in ("{% endif %}", "{{ ghost }}", "{{ x | nope }}", "{% include 'x' %}"):
        with pytest.raises(SymphonyError):
            render_prompt(template, make_issue(), None)


def test_include_tag_cannot_reach_the_filesystem() -> None:
    """No loader is configured, so a prompt cannot pull arbitrary files in."""
    with pytest.raises(TemplateRenderError):
        render_prompt("{% include 'secrets' %}", make_issue(), None)


def test_render_error_reports_a_line_number() -> None:
    with pytest.raises(TemplateRenderError) as exc:
        render_prompt("line one\nline two\n{{ ghost }}\n", make_issue(), None)
    assert exc.value.details["line"] == 3


# --------------------------------------------------------------------------
# SPEC 5.4 — empty-body fallback
# --------------------------------------------------------------------------


@pytest.mark.parametrize("template", ["", "   ", "\n\n", "\t \n"])
def test_empty_body_uses_the_spec_fallback_prompt(template: str) -> None:
    assert render_prompt(template, make_issue(), None) == DEFAULT_PROMPT
    assert DEFAULT_PROMPT == "You are working on an issue from the configured tracker."


def test_template_that_renders_to_nothing_is_not_replaced() -> None:
    """The fallback keys off an empty *body*, not an empty *result*."""
    template = "{% if attempt %}retry{% endif %}"
    assert render_prompt(template, make_issue(), None) == ""


# --------------------------------------------------------------------------
# SPEC 7.1 / 10.2 — continuation guidance is a distinct object
# --------------------------------------------------------------------------


def test_continuation_prompt_does_not_resend_the_task_prompt() -> None:
    """SPEC 7.1: continuation turns send guidance only, not the original prompt."""
    body = split_front_matter((REPO_ROOT / "WORKFLOW.md").read_text(encoding="utf-8"))
    issue = make_issue()
    task_prompt = render_prompt(body, issue, None)
    continuation = render_continuation_prompt(issue, 2, 20)

    assert task_prompt not in continuation
    for marker in ("## What to do", "## Description", "Understand the issue"):
        assert marker in task_prompt
        assert marker not in continuation


def test_continuation_prompt_carries_the_refreshed_issue_facts() -> None:
    """SPEC 16.5 refetches before each continuation; state/title may have moved."""
    issue = make_issue(state="In Review", title="Renamed after the first turn")
    out = render_continuation_prompt(issue, 4, 20)
    assert "ABC-123" in out
    assert "Renamed after the first turn" in out
    assert "In Review" in out


def test_continuation_prompt_states_the_turn_budget() -> None:
    out = render_continuation_prompt(make_issue(), 3, 20)
    assert "turn 3 of at most 20" in out


def test_continuation_prompt_warns_only_on_the_final_turn() -> None:
    """SPEC 16.5 breaks the loop at ``max_turns``; the agent should know it is last."""
    assert "final turn" not in render_continuation_prompt(make_issue(), 19, 20)
    assert "final turn" in render_continuation_prompt(make_issue(), 20, 20)


def test_continuation_prompt_stays_wrapped_for_a_long_identifier_and_state() -> None:
    """Wrapping happens after interpolation, so provider-native names cannot blow it out."""
    issue = make_issue(
        identifier="PLATFORM-INFRASTRUCTURE-MIGRATION-2026-4471",
        state="Blocked On External Dependency Review",
        title="Cut over the primary write path to the new idempotency ledger",
    )
    out = render_continuation_prompt(issue, 5, 20)
    assert max(len(line) for line in out.splitlines()) <= 80
    assert out.isascii(), "continuation guidance must survive any transport encoding"


def test_continuation_prompt_is_deterministic() -> None:
    """No clocks, no randomness — the same inputs give byte-identical guidance."""
    first = render_continuation_prompt(make_issue(), 2, 20)
    second = render_continuation_prompt(make_issue(), 2, 20)
    assert first == second


@pytest.mark.parametrize(("turn_number", "max_turns"), [(0, 20), (-1, 20), (2, 0)])
def test_continuation_prompt_rejects_impossible_counters(
    turn_number: int,
    max_turns: int,
) -> None:
    with pytest.raises(TemplateRenderError):
        render_continuation_prompt(make_issue(), turn_number, max_turns)


# --------------------------------------------------------------------------
# The repo's own WORKFLOW.md must render (SPEC 5.4 end to end)
# --------------------------------------------------------------------------


def test_repo_workflow_md_renders_against_a_populated_issue() -> None:
    body = split_front_matter((REPO_ROOT / "WORKFLOW.md").read_text(encoding="utf-8"))
    out = render_prompt(body, make_issue(), 2)

    first_line = out.strip().splitlines()[0]
    assert first_line == "You are working on `ABC-123`: Make the widget idempotent."
    assert "- State: `In Progress`" in out
    assert "P2" in out
    assert "https://tracker.example/ABC-123" in out
    assert "`backend`, `bug`" in out
    assert "continuation/retry attempt 2" in out
    assert "The widget double-fires on retry." in out
    assert "ABC-99 (In Review)" in out
    assert "{{" not in out and "{%" not in out


def test_repo_workflow_md_renders_with_every_optional_field_absent() -> None:
    """The shipped template must survive a minimal issue without raising."""
    body = split_front_matter((REPO_ROOT / "WORKFLOW.md").read_text(encoding="utf-8"))
    bare = Issue(id="1", identifier="ABC-1", title="Bare", state="Todo", dispatchable=True)
    out = render_prompt(body, bare, None)

    assert "(no description provided)" in out
    assert "unset" in out
    assert "Blocked by" not in out
    assert "continuation/retry attempt" not in out
