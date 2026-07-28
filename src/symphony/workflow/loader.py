"""``WORKFLOW.md`` discovery and parsing (SPEC 5.1, 5.2).

This module owns exactly two things: deciding *which* file is the workflow file
(SPEC 5.1 precedence) and turning that file's bytes into a
:class:`~symphony.models.WorkflowDefinition` (SPEC 5.2 parsing rules).

It deliberately does **not** apply defaults, coerce types, or render the prompt.
Front-matter schema handling is ``symphony.workflow.config`` (SPEC 5.3, 6.1) and
the empty-body fallback prompt is ``symphony.workflow.template`` (SPEC 5.4), so a
workflow whose body is empty parses to an empty ``prompt_template`` here rather
than to the fallback text.

Every failure raises a typed error from :mod:`symphony.errors` (SPEC 5.5); the
loader never silently degrades to a default config or a default prompt, because
SPEC 5.4 requires read/parse failures to surface as configuration errors.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from symphony.errors import MissingWorkflowFile, WorkflowFrontMatterNotAMap, WorkflowParseError
from symphony.models import WorkflowDefinition

__all__ = [
    "DEFAULT_WORKFLOW_FILENAME",
    "FRONT_MATTER_DELIMITER",
    "load_workflow",
    "parse_workflow_text",
    "resolve_workflow_path",
]

DEFAULT_WORKFLOW_FILENAME = "WORKFLOW.md"
"""SPEC 5.1 precedence step 2: the cwd default."""

FRONT_MATTER_DELIMITER = "---"
"""SPEC 5.2: the front-matter open/close marker, matched as a whole line."""


def resolve_workflow_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the workflow file path (SPEC 5.1).

    Precedence:

    1. ``explicit`` — the application/runtime setting supplied by the CLI.
    2. ``WORKFLOW.md`` in the current process working directory.

    The result is absolute and lexically normalized so that later consumers can
    use its parent as the base directory for relative config paths
    (SPEC 5.3.3). Resolution is pure: it never touches the filesystem, so a
    nonexistent path resolves happily and the ``missing_workflow_file`` error
    (SPEC 5.1) is raised by :func:`load_workflow` instead.
    """
    if explicit is not None:
        candidate = os.fspath(explicit)
        if candidate.strip():
            return _absolute(candidate)
    return _absolute(DEFAULT_WORKFLOW_FILENAME)


def load_workflow(path: str | os.PathLike[str]) -> WorkflowDefinition:
    """Read and parse a workflow file (SPEC 5.1, 5.2).

    Raises:
        MissingWorkflowFile: the file cannot be read (SPEC 5.1).
        WorkflowParseError: the bytes are not decodable text, the front matter
            is unterminated, or the YAML fails to decode (SPEC 5.5).
        WorkflowFrontMatterNotAMap: the front matter decodes to a non-map
            (SPEC 5.2, 5.5).
    """
    source = _absolute(os.fspath(path))
    try:
        # Universal-newline mode normalizes CRLF for us; ``utf-8-sig`` tolerates
        # a UTF-8 BOM, which would otherwise hide the leading ``---``.
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkflowParseError(
            "workflow file is not valid UTF-8 text",
            path=str(source),
            reason=exc.reason,
        ) from exc
    except OSError as exc:
        raise MissingWorkflowFile(
            "workflow file cannot be read",
            path=str(source),
            reason=exc.strerror or type(exc).__name__,
        ) from exc
    return parse_workflow_text(text, source_path=str(source))


def parse_workflow_text(text: str, *, source_path: str | None = None) -> WorkflowDefinition:
    """Parse workflow file *contents* (SPEC 5.2).

    Split out from :func:`load_workflow` so callers holding the text already —
    tests, an RLM inspecting a candidate document — can parse without a file.

    Rules applied, in order:

    - Front matter exists only if the *first* line is exactly ``---``; a ``---``
      further down is ordinary Markdown and stays in the body.
    - The block ends at the next line that is exactly ``---`` at column zero, so
      an indented ``---`` inside a YAML block scalar does not terminate it.
    - Missing front matter means an empty config map and a whole-file body.
    - The front matter MUST decode to a map; anything else is an error.
    - The body is trimmed.
    """
    normalized = _normalize_newlines(text)
    front_matter, body = _split_front_matter(normalized, source_path)
    config: dict[str, Any] = (
        {} if front_matter is None else _decode_front_matter(front_matter, source_path)
    )
    return WorkflowDefinition(
        config=config,
        prompt_template=body.strip(),
        source_path=source_path,
    )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _absolute(value: str) -> Path:
    """Expand ``~`` and make absolute without resolving symlinks.

    ``Path.resolve()`` is avoided so the returned path keeps the name the
    operator configured; only lexical normalization is applied.
    """
    return Path(os.path.abspath(os.path.expanduser(value)))


def _normalize_newlines(text: str) -> str:
    """Collapse CRLF/CR to LF.

    :func:`load_workflow` already reads in universal-newline mode, but
    :func:`parse_workflow_text` accepts strings from anywhere and the SPEC 5.2
    line rules are only well defined against a single newline convention. A
    Windows-authored ``WORKFLOW.md`` must parse identically to a POSIX one.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_front_matter(text: str, source_path: str | None) -> tuple[str | None, str]:
    """Return ``(front_matter_text_or_None, body)`` for LF-normalized *text*."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != FRONT_MATTER_DELIMITER:
        return None, text

    for index in range(1, len(lines)):
        if lines[index].rstrip() == FRONT_MATTER_DELIMITER:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])

    raise WorkflowParseError(
        "unterminated YAML front matter: opening '---' has no closing '---'",
        path=source_path,
    )


def _decode_front_matter(raw: str, source_path: str | None) -> dict[str, Any]:
    """Decode the front-matter block to a map (SPEC 5.2)."""
    if not raw.strip():
        # A delimited but empty block carries no YAML document at all, so it is
        # the "front matter is absent" case: an empty config map, not a null.
        return {}
    try:
        decoded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(
            _yaml_error_message(exc),
            path=source_path,
        ) from exc

    if isinstance(decoded, dict):
        return decoded

    raise WorkflowFrontMatterNotAMap(
        "workflow front matter must decode to a map",
        path=source_path,
        decoded_type=type(decoded).__name__,
    )


def _yaml_error_message(exc: yaml.YAMLError) -> str:
    """Describe a YAML failure without echoing the offending source line.

    PyYAML's ``str(exc)`` embeds a snippet of the input, which for a workflow
    file may be a credential (SPEC 15.3). Only the problem description and a
    file-relative location are reported. Line numbers are offset by two: the
    mark is zero-based within the block, which starts on file line 2 because
    line 1 is the opening ``---``.
    """
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem:
        mark = exc.problem_mark
        if mark is not None:
            return (
                f"invalid YAML front matter: {exc.problem} "
                f"(line {mark.line + 2}, column {mark.column + 1})"
            )
        return f"invalid YAML front matter: {exc.problem}"
    return f"invalid YAML front matter ({type(exc).__name__})"
