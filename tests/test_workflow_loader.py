"""Conformance tests for ``symphony.workflow.loader`` (SPEC 5.1, 5.2, 5.5, 17.1).

The loader depends only on the pre-written ``symphony.models`` and
``symphony.errors``; no sibling module is imported, so nothing here is faked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from symphony.errors import (
    MissingWorkflowFile,
    WorkflowFrontMatterNotAMap,
    WorkflowParseError,
)
from symphony.models import WorkflowDefinition
from symphony.workflow.loader import (
    DEFAULT_WORKFLOW_FILENAME,
    load_workflow,
    parse_workflow_text,
    resolve_workflow_path,
)

FULL_WORKFLOW = """---
tracker:
  kind: memory
  required_labels: [ready]
polling:
  interval_ms: 5000
---

# Task

Fix {{ issue.identifier }}.
"""


def write(path: Path, text: str, *, newline: str = "\n") -> Path:
    """Write *text* with an explicit newline convention (bypasses translation)."""
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


# --------------------------------------------------------------------------
# SPEC 5.1 — path precedence (SPEC 17.1: "Workflow file path precedence")
# --------------------------------------------------------------------------


def test_default_path_is_workflow_md_in_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_workflow_path() == tmp_path / DEFAULT_WORKFLOW_FILENAME


def test_explicit_path_wins_over_cwd_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    explicit = tmp_path / "nested" / "OTHER.md"
    assert resolve_workflow_path(str(explicit)) == explicit


def test_explicit_relative_path_is_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = resolve_workflow_path("conf/WORKFLOW.md")
    assert resolved.is_absolute()
    assert resolved == tmp_path / "conf" / "WORKFLOW.md"


def test_blank_explicit_path_falls_back_to_cwd_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_workflow_path("   ") == tmp_path / DEFAULT_WORKFLOW_FILENAME


def test_explicit_path_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert resolve_workflow_path("~/WORKFLOW.md") == tmp_path / DEFAULT_WORKFLOW_FILENAME


def test_resolution_does_not_touch_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = resolve_workflow_path()
    assert not resolved.exists()


# --------------------------------------------------------------------------
# SPEC 5.1 / 5.5 — missing_workflow_file (SPEC 17.1: "Missing WORKFLOW.md")
# --------------------------------------------------------------------------


def test_missing_file_raises_missing_workflow_file(tmp_path: Path) -> None:
    with pytest.raises(MissingWorkflowFile) as excinfo:
        load_workflow(tmp_path / "WORKFLOW.md")
    assert excinfo.value.category == "missing_workflow_file"
    assert excinfo.value.to_dict()["details"]["path"].endswith("WORKFLOW.md")


def test_directory_at_workflow_path_raises_missing_workflow_file(tmp_path: Path) -> None:
    directory = tmp_path / "WORKFLOW.md"
    directory.mkdir()
    with pytest.raises(MissingWorkflowFile):
        load_workflow(directory)


def test_undecodable_bytes_raise_workflow_parse_error(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_bytes(b"---\nkind: \xff\xfe\n---\nbody\n")
    with pytest.raises(WorkflowParseError) as excinfo:
        load_workflow(path)
    assert excinfo.value.category == "workflow_parse_error"


# --------------------------------------------------------------------------
# SPEC 5.2 — front matter detection and body split
# --------------------------------------------------------------------------


def test_front_matter_and_body_are_split(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", FULL_WORKFLOW)
    defn = load_workflow(path)
    assert isinstance(defn, WorkflowDefinition)
    assert defn.config == {
        "tracker": {"kind": "memory", "required_labels": ["ready"]},
        "polling": {"interval_ms": 5000},
    }
    assert defn.prompt_template == "# Task\n\nFix {{ issue.identifier }}."
    assert defn.source_path == str(path)


def test_config_is_the_front_matter_root_not_nested(tmp_path: Path) -> None:
    """SPEC 5.2: ``config`` is the front-matter root object itself."""
    path = write(tmp_path / "WORKFLOW.md", "---\ntracker:\n  kind: memory\n---\nbody\n")
    defn = load_workflow(path)
    assert "config" not in defn.config
    assert defn.config["tracker"]["kind"] == "memory"


def test_delimiter_after_first_line_is_body_content(tmp_path: Path) -> None:
    """Only a *leading* ``---`` opens front matter; a later one is a Markdown rule."""
    text = "# Task\n\n---\n\nDo the work.\n"
    path = write(tmp_path / "WORKFLOW.md", text)
    defn = load_workflow(path)
    assert defn.config == {}
    assert defn.prompt_template == "# Task\n\n---\n\nDo the work."


def test_no_front_matter_yields_empty_config_and_whole_body(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", "Just a prompt.\n")
    defn = load_workflow(path)
    assert defn.config == {}
    assert defn.prompt_template == "Just a prompt."


def test_indented_delimiter_does_not_close_front_matter(tmp_path: Path) -> None:
    text = (
        "---\n"
        "hooks:\n"
        "  after_run: |\n"
        "    echo one\n"
        "    ---\n"
        "    echo two\n"
        "agent:\n"
        "  max_turns: 3\n"
        "---\n"
        "body\n"
    )
    path = write(tmp_path / "WORKFLOW.md", text)
    defn = load_workflow(path)
    assert defn.config["agent"] == {"max_turns": 3}
    assert defn.config["hooks"]["after_run"] == "echo one\n---\necho two\n"
    assert defn.prompt_template == "body"


def test_body_delimiter_after_front_matter_is_preserved(tmp_path: Path) -> None:
    text = "---\nagent:\n  max_turns: 2\n---\nintro\n\n---\n\nend\n"
    path = write(tmp_path / "WORKFLOW.md", text)
    defn = load_workflow(path)
    assert defn.config == {"agent": {"max_turns": 2}}
    assert defn.prompt_template == "intro\n\n---\n\nend"


def test_unterminated_front_matter_raises_workflow_parse_error(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", "---\ntracker:\n  kind: memory\n\nbody text\n")
    with pytest.raises(WorkflowParseError) as excinfo:
        load_workflow(path)
    assert excinfo.value.category == "workflow_parse_error"
    assert "unterminated" in excinfo.value.message


def test_prompt_body_is_trimmed(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", "---\na: 1\n---\n\n\n   prompt   \n\n\n")
    assert load_workflow(path).prompt_template == "prompt"


def test_empty_body_is_empty_string_not_the_fallback_prompt(tmp_path: Path) -> None:
    """SPEC 5.4's fallback belongs to the renderer, not the loader."""
    path = write(tmp_path / "WORKFLOW.md", "---\ntracker:\n  kind: memory\n---\n\n\n")
    defn = load_workflow(path)
    assert defn.prompt_template == ""
    assert defn.config == {"tracker": {"kind": "memory"}}


def test_empty_file_parses_to_empty_config_and_empty_body(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", "")
    defn = load_workflow(path)
    assert defn.config == {}
    assert defn.prompt_template == ""


def test_utf8_bom_does_not_hide_the_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_bytes(b"\xef\xbb\xbf---\ntracker:\n  kind: memory\n---\nbody\n")
    defn = load_workflow(path)
    assert defn.config == {"tracker": {"kind": "memory"}}
    assert defn.prompt_template == "body"


# --------------------------------------------------------------------------
# SPEC 5.2 — CRLF (Windows-authored workflow files)
# --------------------------------------------------------------------------


def test_crlf_file_parses_identically_to_lf(tmp_path: Path) -> None:
    crlf = write(tmp_path / "crlf.md", FULL_WORKFLOW, newline="\r\n")
    lf = write(tmp_path / "lf.md", FULL_WORKFLOW)
    crlf_defn = load_workflow(crlf)
    lf_defn = load_workflow(lf)
    assert crlf_defn.config == lf_defn.config
    assert crlf_defn.prompt_template == lf_defn.prompt_template
    assert "\r" not in crlf_defn.prompt_template


def test_crlf_body_delimiter_is_not_mistaken_for_a_terminator(tmp_path: Path) -> None:
    text = "---\nagent:\n  max_turns: 2\n---\nintro\n\n---\n\nend\n"
    path = write(tmp_path / "WORKFLOW.md", text, newline="\r\n")
    defn = load_workflow(path)
    assert defn.config == {"agent": {"max_turns": 2}}
    assert defn.prompt_template == "intro\n\n---\n\nend"


def test_crlf_text_parsed_directly_is_normalized() -> None:
    defn = parse_workflow_text("---\r\na: 1\r\n---\r\nline one\r\nline two\r\n")
    assert defn.config == {"a": 1}
    assert defn.prompt_template == "line one\nline two"


# --------------------------------------------------------------------------
# SPEC 5.2 / 5.5 — YAML decode failures (SPEC 17.1: "Invalid YAML front matter")
# --------------------------------------------------------------------------


def test_invalid_yaml_raises_workflow_parse_error(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", "---\ntracker:\n  kind: [memory\n---\nbody\n")
    with pytest.raises(WorkflowParseError) as excinfo:
        load_workflow(path)
    assert excinfo.value.category == "workflow_parse_error"
    assert "invalid YAML front matter" in excinfo.value.message


def test_tab_indented_yaml_raises_workflow_parse_error(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", "---\ntracker:\n\tkind: memory\n---\nbody\n")
    with pytest.raises(WorkflowParseError):
        load_workflow(path)


def test_yaml_error_reports_file_relative_line_number(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", "---\na: 1\nb: 2\n\tc: 3\n---\nbody\n")
    with pytest.raises(WorkflowParseError) as excinfo:
        load_workflow(path)
    assert "line 4" in excinfo.value.message


def test_yaml_error_message_does_not_echo_the_offending_line(tmp_path: Path) -> None:
    """SPEC 15.3: front matter can hold credentials; errors must not print them."""
    secret = "sk-live-DO-NOT-LEAK-0001"
    path = write(tmp_path / "WORKFLOW.md", f'---\ntoken: "{secret}\nkind: memory\n---\nbody\n')
    with pytest.raises(WorkflowParseError) as excinfo:
        load_workflow(path)
    rendered = f"{excinfo.value.message} {excinfo.value.to_dict()}"
    assert secret not in rendered


# --------------------------------------------------------------------------
# SPEC 5.2 / 5.5 — non-map front matter (SPEC 17.1: "Front matter non-map")
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("front_matter", "decoded_type"),
    [
        ("- one\n- two", "list"),
        ("just a scalar", "str"),
        ("42", "int"),
        ("null", "NoneType"),
        ("# only a comment", "NoneType"),
    ],
)
def test_non_map_front_matter_raises_typed_error(
    tmp_path: Path, front_matter: str, decoded_type: str
) -> None:
    path = write(tmp_path / "WORKFLOW.md", f"---\n{front_matter}\n---\nbody\n")
    with pytest.raises(WorkflowFrontMatterNotAMap) as excinfo:
        load_workflow(path)
    assert excinfo.value.category == "workflow_front_matter_not_a_map"
    assert excinfo.value.details["decoded_type"] == decoded_type


def test_empty_front_matter_block_is_an_empty_config_map(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", "---\n---\nbody\n")
    defn = load_workflow(path)
    assert defn.config == {}
    assert defn.prompt_template == "body"


def test_whitespace_only_front_matter_block_is_an_empty_config_map(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", "---\n   \n\n---\nbody\n")
    assert load_workflow(path).config == {}


# --------------------------------------------------------------------------
# Call-surface details
# --------------------------------------------------------------------------


def test_load_workflow_accepts_str_and_pathlike(tmp_path: Path) -> None:
    path = write(tmp_path / "WORKFLOW.md", "---\na: 1\n---\nbody\n")
    assert load_workflow(str(path)).config == load_workflow(path).config == {"a": 1}


def test_source_path_is_absolute_for_a_relative_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "WORKFLOW.md", "---\na: 1\n---\nbody\n")
    monkeypatch.chdir(tmp_path)
    defn = load_workflow(DEFAULT_WORKFLOW_FILENAME)
    assert Path(defn.source_path or "").is_absolute()
    assert defn.source_path == str(tmp_path / DEFAULT_WORKFLOW_FILENAME)


def test_parse_workflow_text_defaults_source_path_to_none() -> None:
    defn = parse_workflow_text("---\na: 1\n---\nbody\n")
    assert defn.source_path is None
    assert defn.config == {"a": 1}
    assert defn.prompt_template == "body"
