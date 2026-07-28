"""Strict Liquid prompt rendering (SPEC 5.4, SPEC 12).

The Markdown body of ``WORKFLOW.md`` is the per-issue prompt template
(SPEC 5.4). This module turns that body plus a normalized :class:`Issue` into
the exact string handed to the coding agent's first turn, and separately
produces the continuation guidance used for later turns on the same live
thread (SPEC 7.1, SPEC 10.2).

Strictness is the point. SPEC 5.4 states that unknown variables MUST fail
rendering and unknown filters MUST fail rendering. A permissive engine would
silently substitute an empty string, and the failure mode of that is a coding
agent dispatched with a prompt that is missing its description, its labels, or
its whole context — with no error anywhere. So the environment is built with
``StrictUndefined``, ``strict_filters=True``, and ``Mode.STRICT``, and
``tests/test_template.py`` proves each of those raises rather than trusting the
library's defaults to stay put.

Two failure classes, deliberately distinct (SPEC 5.5):

``TemplateParseError`` (``template_parse_error``)
    The template text is malformed. Detected while compiling, before any issue
    data is consulted.

``TemplateRenderError`` (``template_render_error``)
    The template compiled but named something that does not exist — unknown
    variable, unknown filter, invalid interpolation.

Both fail only the affected run attempt (SPEC 5.5, SPEC 12.4). Unlike a
workflow read/YAML error, neither blocks future dispatches.
"""

from __future__ import annotations

import re
import textwrap
from typing import Any, Final

from liquid import BoundTemplate, Environment, Mode, StrictUndefined
from liquid.exceptions import LiquidError

from symphony.errors import TemplateParseError, TemplateRenderError
from symphony.models import Issue

__all__ = [
    "DEFAULT_PROMPT",
    "PROMPT_ENVIRONMENT",
    "TEMPLATE_VARIABLES",
    "build_environment",
    "build_template_context",
    "render_continuation_prompt",
    "render_prompt",
]


DEFAULT_PROMPT: Final[str] = "You are working on an issue from the configured tracker."
"""Minimal fallback used when the workflow prompt body is empty (SPEC 5.4).

The string is verbatim from SPEC 5.4. It applies *only* to an empty body. A
workflow file that fails to read or parse is a configuration error and MUST NOT
fall back to a prompt (SPEC 5.4); that is the loader's concern, not this
module's.
"""

TEMPLATE_VARIABLES: Final[tuple[str, ...]] = ("issue", "attempt")
"""Every top-level name a prompt template may reference (SPEC 5.4).

Under strict mode this tuple is exhaustive by construction: anything not in the
render context raises. Named here so the RLM surface can introspect the
contract without parsing this module.
"""


_WRAP_WIDTH: Final[int] = 80
_NUMBERED_ITEM: Final[re.Pattern[str]] = re.compile(r"^\d+\.\s+")


def build_environment() -> Environment:
    """Construct the strict Liquid environment required by SPEC 5.4.

    Every argument is load-bearing:

    ``undefined=StrictUndefined``
        Reading a name that is not in the render context raises
        ``UndefinedError`` instead of rendering an empty string. This holds
        inside ``{% if %}`` and ``{% for %}`` too, which is the case most
        engines quietly treat as falsy.

    ``strict_filters=True``
        Piping through a filter this environment does not define raises
        ``UnknownFilterError`` instead of passing the value through unchanged.

    ``tolerance=Mode.STRICT``
        Malformed markup raises instead of being emitted as literal text.

    ``loader=None``
        ``{% include %}`` and ``{% render %}`` have no filesystem to reach, so
        a prompt template cannot pull arbitrary files into an agent prompt.

    ``extra=False``
        Only the standard Shopify-compatible tag and filter set, so a workflow
        that renders here renders anywhere Liquid-compatible (SPEC 5.4).
    """
    return Environment(
        tolerance=Mode.STRICT,
        undefined=StrictUndefined,
        strict_filters=True,
        loader=None,
        extra=False,
        autoescape=False,
    )


PROMPT_ENVIRONMENT: Final[Environment] = build_environment()
"""Process-wide strict environment. Stateless across renders; safe to share."""


def build_template_context(issue: Issue, attempt: int | None) -> dict[str, Any]:
    """Assemble the render context (SPEC 12.1, SPEC 12.2).

    ``issue`` becomes a plain string-keyed mapping with nested lists preserved
    so templates can iterate ``issue.labels`` and ``issue.blocked_by``
    (SPEC 12.2); ``Issue.to_template_context`` owns that conversion.

    ``attempt`` is the 1-based retry/continuation count and is ``None`` on the
    first run (SPEC 12.3). It is placed in the context even when ``None`` so
    that ``{% if attempt %}`` is a legal, non-raising test — an absent key
    would raise under ``StrictUndefined``.
    """
    return {"issue": issue.to_template_context(), "attempt": attempt}


def render_prompt(template: str, issue: Issue, attempt: int | None) -> str:
    """Render the per-issue task prompt (SPEC 5.4, SPEC 12.1-12.4).

    Args:
        template: The workflow prompt body, i.e. ``WorkflowDefinition.prompt_template``.
        issue: The normalized issue being dispatched.
        attempt: ``None`` on the first run, an integer on any retry or
            continuation run (SPEC 12.3).

    Returns:
        The rendered prompt, or :data:`DEFAULT_PROMPT` when ``template`` is
        empty or whitespace-only (SPEC 5.4).

    Raises:
        TemplateParseError: ``template`` is malformed.
        TemplateRenderError: ``template`` referenced an unknown variable or an
            unknown filter, or a filter rejected its argument.

    Either failure fails just this run attempt (SPEC 12.4); the orchestrator
    treats it like any other worker failure and decides retry behavior.
    """
    if not template.strip():
        return DEFAULT_PROMPT

    bound = _parse(template, issue_identifier=issue.identifier)
    context = build_template_context(issue, attempt)

    try:
        return bound.render(**context)
    except LiquidError as exc:
        raise TemplateRenderError(
            _message(exc),
            **_position(exc),
            issue_identifier=issue.identifier,
            attempt=attempt,
        ) from exc


def render_continuation_prompt(issue: Issue, turn_number: int, max_turns: int) -> str:
    """Build the guidance sent to a *continuation* turn (SPEC 7.1, SPEC 10.2).

    This is a different object from the task prompt, not a re-render of it.
    SPEC 7.1 and SPEC 10.2 both require that later in-worker turns send only
    continuation guidance to the existing thread rather than resending the
    original issue prompt, which is already in that thread's history. Repeating
    it would burn context and invite the agent to restart work it has already
    done.

    The issue passed here is the one the worker re-fetched after the previous
    turn (SPEC 16.5), so ``issue.state`` and ``issue.title`` are genuinely new
    information — the thread's history holds the *stale* values.

    Args:
        issue: The freshly refetched issue.
        turn_number: 1-based index of the turn about to start.
        max_turns: ``agent.max_turns`` — the session's hard turn ceiling.

    Returns:
        Plain guidance text. Deliberately not Liquid-rendered: it is
        implementation-owned, takes no workflow input, and therefore has no
        reason to be able to fail.

    Raises:
        TemplateRenderError: ``turn_number`` or ``max_turns`` is below 1.
            Callers hitting this have an off-by-one in the turn loop; emitting
            "turn 0 of 0" to a coding agent would hide it.
    """
    if turn_number < 1 or max_turns < 1:
        raise TemplateRenderError(
            "continuation turn counters must be >= 1",
            issue_identifier=issue.identifier,
            turn_number=turn_number,
            max_turns=max_turns,
        )

    steps = (
        "1. Re-read what is actually on disk in this workspace, not what you intended"
        " to write. Confirm the edits from your last turn actually landed.",
        "2. Re-run the project's tests and linters and read the real output. Do not"
        " assume the previous turn left them green.",
        "3. Fix anything broken or half-finished before starting new work.",
        "4. If the work is genuinely complete, move the ticket to its next handoff"
        " state and record what changed, what you verified, and what you deliberately"
        " left alone.",
    )

    blocks: list[str] = [
        _wrap(f"Continue working on {issue.identifier}: {issue.title}"),
        _wrap(
            f"This is turn {turn_number} of at most {max_turns} in this session. Your"
            " original task prompt is already in this thread's history above and is"
            " not repeated here."
        ),
        _wrap(
            f"The tracker was re-checked after your last turn and {issue.identifier}"
            f" is still active in state `{issue.state}`. Treat that as ground truth:"
            " either the work is not finished, or it is finished and the ticket was"
            " never moved to its handoff state."
        ),
        _wrap("Before writing anything new:"),
        "\n".join(_wrap(step) for step in steps),
        _wrap(
            "Do not restart the task from scratch and do not redo work that is already"
            " committed. If you are blocked, or the requirements are ambiguous, stop"
            " and say so plainly in your handoff notes rather than guessing."
        ),
    ]

    if turn_number >= max_turns:
        blocks.append(
            _wrap(
                "This is the final turn of this session. Leave the workspace coherent"
                " and self-consistent and write your handoff notes now; there is no"
                " later turn in which to tidy up."
            )
        )

    return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _parse(template: str, *, issue_identifier: str | None = None) -> BoundTemplate:
    """Compile a template, mapping Liquid syntax failures to SPEC 5.5's parse class."""
    try:
        return PROMPT_ENVIRONMENT.from_string(template)
    except LiquidError as exc:
        raise TemplateParseError(
            _message(exc),
            **_position(exc),
            issue_identifier=issue_identifier,
        ) from exc


def _wrap(block: str) -> str:
    """Hard-wrap one paragraph, hanging-indenting numbered items.

    Wrapping happens *after* interpolation so a long issue title or state name
    cannot blow out a line; hand-wrapped literals would only look right for the
    identifier lengths the author happened to test with.
    """
    marker = _NUMBERED_ITEM.match(block)
    indent = " " * len(marker.group(0)) if marker else ""
    return textwrap.fill(
        block,
        width=_WRAP_WIDTH,
        subsequent_indent=indent,
        break_on_hyphens=False,
        break_long_words=False,
    )


def _message(exc: LiquidError) -> str:
    """Concise cause without the multi-line source excerpt ``str(exc)`` appends."""
    message = getattr(exc, "message", None)
    text = message if isinstance(message, str) and message else str(exc)
    return f"{type(exc).__name__}: {text}"


def _position(exc: LiquidError) -> dict[str, Any]:
    """Best-effort 1-based line number for operator-facing logs.

    Only positional metadata is carried into ``details``; rendered values never
    are, because a template may interpolate tracker content (SPEC 15.3).
    """
    token = getattr(exc, "token", None)
    source = getattr(token, "source", None)
    start = getattr(token, "start_index", None)
    if isinstance(source, str) and isinstance(start, int):
        return {"line": source.count("\n", 0, start) + 1}
    return {}
