"""Conformance tests for the workspace manager and safety invariants.

Covers SPEC 9.1, 9.2, 9.3, 9.5 and 15.2, against the SPEC 17.2 matrix.

The hook runner is faked rather than imported: ``symphony.workspace.hooks`` is
owned by another module author, and these tests assert *this* module's half of
the contract (which hook, which cwd, which ``fatal`` flag, in which order).
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import tempfile
from pathlib import Path

import pytest

from symphony.errors import (
    HookError,
    InvalidWorkspaceCwd,
    WorkspaceCreationError,
    WorkspaceError,
    WorkspacePathEscapesRoot,
)
from symphony.models import workspace_key
from symphony.workspace.manager import WorkspaceManager
from symphony.workspace.safety import assert_launch_cwd, assert_within_root, normalize_abs

# ---------------------------------------------------------------------------
# Test doubles and helpers
# ---------------------------------------------------------------------------


class FakeHookRunner:
    """Stand-in for ``symphony.workspace.hooks.HookRunner`` (CONTRACTS section 3).

    Records every invocation as ``(name, cwd, fatal, cwd_was_directory)`` so
    tests can assert both the arguments and the ordering relative to filesystem
    effects.
    """

    def __init__(self, *, fail_on: str | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, Path, bool, bool]] = []
        self.fail_on = fail_on
        self.error = error or HookError("hook exited non-zero", hook=fail_on or "unknown")

    async def run(self, name: str, cwd: Path, *, fatal: bool) -> None:
        self.calls.append((name, Path(cwd), fatal, Path(cwd).is_dir()))
        if name == self.fail_on:
            raise self.error

    @property
    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def _symlinks_supported() -> bool:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "target"
        target.mkdir()
        try:
            (Path(td) / "link").symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
        return True


SYMLINKS = _symlinks_supported()
requires_symlinks = pytest.mark.skipif(not SYMLINKS, reason="host cannot create symlinks")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


def _short_path(path: Path) -> Path | None:
    """Windows 8.3 short form of *path*, or None when the volume has none."""
    buf = ctypes.create_unicode_buffer(1024)
    written = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)  # type: ignore[attr-defined]
    if not written or buf.value == str(path):
        return None
    return Path(buf.value)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "workspaces"
    r.mkdir()
    return r


@pytest.fixture
def hooks() -> FakeHookRunner:
    return FakeHookRunner()


@pytest.fixture
def manager(root: Path, hooks: FakeHookRunner) -> WorkspaceManager:
    return WorkspaceManager(root, hooks)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SPEC 9.5 Invariant 2 / 15.2 — root containment
# ---------------------------------------------------------------------------


def test_within_root_accepts_direct_child(root: Path) -> None:
    assert assert_within_root(root / "ENG-1", root) == root / "ENG-1"


def test_within_root_accepts_nested_descendant(root: Path) -> None:
    nested = root / "ENG-1" / "src" / "main.py"
    assert assert_within_root(nested, root) == nested


def test_within_root_returns_normalized_absolute_path(root: Path, monkeypatch) -> None:
    monkeypatch.chdir(root)
    resolved = assert_within_root(Path("ENG-1"), root)
    assert resolved.is_absolute()
    assert resolved == root / "ENG-1"


def test_within_root_rejects_the_root_itself(root: Path) -> None:
    """Strict containment: the root is not "inside" the root (see cleanup)."""
    with pytest.raises(WorkspacePathEscapesRoot):
        assert_within_root(root, root)


def test_within_root_rejects_parent_traversal(root: Path) -> None:
    with pytest.raises(WorkspacePathEscapesRoot):
        assert_within_root(root / ".." / "rootkit", root)


def test_within_root_rejects_bare_dotdot(root: Path) -> None:
    with pytest.raises(WorkspacePathEscapesRoot):
        assert_within_root(root / "..", root)


def test_within_root_rejects_sibling_sharing_a_string_prefix(tmp_path: Path) -> None:
    """A ``startswith`` check would wrongly accept ``.../workspaces-evil``."""
    base = tmp_path / "workspaces"
    base.mkdir()
    evil = tmp_path / "workspaces-evil"
    evil.mkdir()
    with pytest.raises(WorkspacePathEscapesRoot):
        assert_within_root(evil / "ENG-1", base)


def test_within_root_rejects_absolute_path_outside_root(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "ENG-1"
    with pytest.raises(WorkspacePathEscapesRoot):
        assert_within_root(outside, root)


@requires_symlinks
def test_within_root_rejects_symlink_escape(root: Path, tmp_path: Path) -> None:
    """A lexical-only check would accept this: every component looks contained."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "ENG-1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspacePathEscapesRoot):
        assert_within_root(root / "ENG-1", root)
    with pytest.raises(WorkspacePathEscapesRoot):
        assert_within_root(root / "ENG-1" / "src", root)


@requires_symlinks
def test_within_root_accepts_child_when_the_root_is_reached_via_symlink(
    tmp_path: Path,
) -> None:
    """Both operands resolve, so a symlinked root is not a false positive."""
    real = tmp_path / "real-root"
    real.mkdir()
    linked = tmp_path / "linked-root"
    linked.symlink_to(real, target_is_directory=True)
    assert assert_within_root(linked / "ENG-1", linked) == real / "ENG-1"
    assert assert_within_root(real / "ENG-1", linked) == real / "ENG-1"


@windows_only
def test_within_root_matches_case_insensitively_on_windows(root: Path) -> None:
    shouted = Path(str(root).upper())
    assert assert_within_root(shouted / "ENG-1", root) == root / "ENG-1"


@windows_only
def test_within_root_expands_windows_short_names(tmp_path: Path) -> None:
    long_root = tmp_path / "symphony workspace root"
    long_root.mkdir()
    short_root = _short_path(long_root)
    if short_root is None:
        pytest.skip("volume does not generate 8.3 short names")
    assert assert_within_root(short_root / "ENG-1", long_root) == long_root / "ENG-1"


# ---------------------------------------------------------------------------
# SPEC 9.5 Invariant 1 / 15.2 — agent launch cwd
# ---------------------------------------------------------------------------


def test_launch_cwd_accepts_the_workspace_path(root: Path) -> None:
    ws = root / "ENG-1"
    ws.mkdir()
    assert assert_launch_cwd(ws, ws) is None


def test_launch_cwd_accepts_an_unnormalized_equivalent(root: Path) -> None:
    ws = root / "ENG-1"
    ws.mkdir()
    assert_launch_cwd(root / "ENG-1" / ".", ws)


def test_launch_cwd_rejects_the_root(root: Path) -> None:
    with pytest.raises(InvalidWorkspaceCwd):
        assert_launch_cwd(root, root / "ENG-1")


def test_launch_cwd_rejects_a_sibling_workspace(root: Path) -> None:
    with pytest.raises(InvalidWorkspaceCwd):
        assert_launch_cwd(root / "ENG-2", root / "ENG-1")


def test_launch_cwd_rejects_a_child_of_the_workspace(root: Path) -> None:
    with pytest.raises(InvalidWorkspaceCwd):
        assert_launch_cwd(root / "ENG-1" / "src", root / "ENG-1")


# ---------------------------------------------------------------------------
# SPEC 9.1 / 9.5 Invariant 3 — deterministic, sanitized, contained paths
# ---------------------------------------------------------------------------


def test_path_for_is_deterministic_and_a_direct_child_of_root(manager: WorkspaceManager) -> None:
    first = manager.path_for("ENG-42")
    assert first == manager.path_for("ENG-42")
    assert first == manager.root / workspace_key("ENG-42")
    assert first.parent == manager.root


def test_path_for_keeps_plain_key_when_nothing_could_collide(
    manager: WorkspaceManager,
) -> None:
    """A key needing neither sanitization nor case-fold protection stays readable."""
    assert manager.path_for("eng-42").name == "eng-42"


def test_case_variant_identifiers_get_distinct_workspaces(
    manager: WorkspaceManager,
) -> None:
    """SPEC 9.5 Invariant 3 on a case-insensitive filesystem.

    Sanitization is not the last normalizer between an identifier and a
    directory: Windows and macOS fold case. Without the hash suffix, ``ENG-42``
    and ``eng-42`` produce two distinct keys that resolve to one directory,
    putting two coding agents in one workspace.
    """
    upper = manager.path_for("ENG-42")
    lower = manager.path_for("eng-42")

    assert upper != lower
    assert os.path.normcase(str(upper)) != os.path.normcase(str(lower))
    assert upper.parent == manager.root and lower.parent == manager.root


def test_trailing_dot_identifiers_get_distinct_workspaces(
    manager: WorkspaceManager,
) -> None:
    """Windows strips trailing dots and spaces from directory names."""
    keys = {manager.path_for(i).name for i in ("eng-1", "eng-1.", "eng-1...", "eng-1 ")}
    folded = {os.path.normcase(k).rstrip(". ") for k in keys}

    assert len(folded) == 4, f"identifiers collapsed onto one directory: {folded}"


def test_path_for_distinguishes_identifiers_that_sanitize_alike(
    manager: WorkspaceManager,
) -> None:
    """SPEC 17.2: distinct identifiers colliding under sanitization keep distinct keys."""
    a = manager.path_for("AB/1")
    b = manager.path_for("AB:1")
    assert a != b
    assert a.name.startswith("AB_1-") and b.name.startswith("AB_1-")
    assert a.parent == manager.root and b.parent == manager.root


def test_path_for_neutralizes_separators_in_the_identifier(manager: WorkspaceManager) -> None:
    path = manager.path_for("../../etc/passwd")
    assert path.parent == manager.root
    assert os.sep not in path.name


@pytest.mark.parametrize("identifier", ["..", "."])
def test_path_for_rejects_identifiers_that_survive_sanitization_as_traversal(
    manager: WorkspaceManager, identifier: str
) -> None:
    """``.`` and ``..`` contain only allowed characters, so only containment stops them."""
    assert workspace_key(identifier) == identifier
    with pytest.raises(WorkspacePathEscapesRoot):
        manager.path_for(identifier)


@requires_symlinks
def test_path_for_rejects_a_workspace_symlinked_out_of_root(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    manager.path_for("ENG-1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspacePathEscapesRoot):
        manager.path_for("ENG-1")


# ---------------------------------------------------------------------------
# SPEC 9.2 — creation, reuse, and the after_create gate
# ---------------------------------------------------------------------------


async def test_create_makes_the_missing_directory(
    manager: WorkspaceManager, hooks: FakeHookRunner
) -> None:
    ws = await manager.create_for_issue("ENG-1")

    assert ws.created_now is True
    assert ws.workspace_key == workspace_key("ENG-1")
    assert Path(ws.path).is_absolute()
    assert Path(ws.path).is_dir()
    assert Path(ws.path) == manager.path_for("ENG-1")
    assert hooks.names == ["after_create"]


async def test_create_creates_the_root_on_demand(tmp_path: Path, hooks: FakeHookRunner) -> None:
    missing_root = tmp_path / "never" / "made"
    manager = WorkspaceManager(missing_root, hooks)  # type: ignore[arg-type]
    ws = await manager.create_for_issue("ENG-1")
    assert Path(ws.path).is_dir()


async def test_create_reuses_an_existing_directory(
    manager: WorkspaceManager, hooks: FakeHookRunner
) -> None:
    first = await manager.create_for_issue("ENG-1")
    second = await manager.create_for_issue("ENG-1")

    assert first.created_now is True
    assert second.created_now is False
    assert second.path == first.path
    assert hooks.names == ["after_create"], "after_create must not run for a reused workspace"


async def test_reuse_never_resets_workspace_contents(manager: WorkspaceManager) -> None:
    """SPEC 9.1/9.3: workspaces persist across runs and are not destructively reset."""
    first = await manager.create_for_issue("ENG-1")
    marker = Path(first.path) / "state.txt"
    marker.write_text("carried over", encoding="utf-8")

    second = await manager.create_for_issue("ENG-1")

    assert second.created_now is False
    assert marker.read_text(encoding="utf-8") == "carried over"


async def test_after_create_runs_fatally_inside_the_new_workspace(
    manager: WorkspaceManager, hooks: FakeHookRunner
) -> None:
    ws = await manager.create_for_issue("ENG-1")
    name, cwd, fatal, cwd_was_dir = hooks.calls[0]

    assert name == "after_create"
    assert cwd == Path(ws.path)
    assert fatal is True, "SPEC 9.4: after_create failure is fatal to workspace creation"
    assert cwd_was_dir is True, "SPEC 9.4: hooks run with the workspace directory as cwd"


async def test_after_create_failure_aborts_creation_and_discards_the_new_directory(
    root: Path,
) -> None:
    hooks = FakeHookRunner(fail_on="after_create")
    manager = WorkspaceManager(root, hooks)  # type: ignore[arg-type]

    with pytest.raises(HookError):
        await manager.create_for_issue("ENG-1")

    assert not (root / "ENG-1").exists(), "SPEC 9.3: partially prepared new workspace is removed"


async def test_after_create_reruns_on_the_next_attempt_after_a_failure(root: Path) -> None:
    failing = FakeHookRunner(fail_on="after_create")
    manager = WorkspaceManager(root, failing)  # type: ignore[arg-type]
    with pytest.raises(HookError):
        await manager.create_for_issue("ENG-1")

    ok = FakeHookRunner()
    manager = WorkspaceManager(root, ok)  # type: ignore[arg-type]
    ws = await manager.create_for_issue("ENG-1")

    assert ws.created_now is True
    assert ok.names == ["after_create"]


async def test_concurrent_creates_run_after_create_exactly_once(
    manager: WorkspaceManager, hooks: FakeHookRunner
) -> None:
    """``created_now`` gates the hook, so an exists-then-mkdir race must not exist."""
    results = await asyncio.gather(*(manager.create_for_issue("ENG-1") for _ in range(8)))

    assert sum(1 for ws in results if ws.created_now) == 1
    assert hooks.names == ["after_create"]


async def test_create_treats_a_directory_appearing_mid_call_as_reuse(
    manager: WorkspaceManager, hooks: FakeHookRunner, monkeypatch
) -> None:
    """The loser of a ``mkdir`` race must observe reuse, not crash.

    The interleaving is forced rather than hoped for: the directory is created
    out-of-band at the exact moment the manager tries to create it. An
    ``exists()``-then-``mkdir()`` implementation passes its existence check and
    then dies on the mkdir; only catching the exists-error is correct.
    """
    target = manager.path_for("ENG-1")
    real_mkdir = Path.mkdir
    raced = False

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        nonlocal raced
        if self == target and not raced:
            raced = True
            real_mkdir(self)  # another worker wins the race
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    ws = await manager.create_for_issue("ENG-1")

    assert raced is True
    assert ws.created_now is False, "created_now must be true only for the creating call"
    assert hooks.calls == [], "SPEC 9.2: after_create is gated on created_now"


async def test_create_fails_with_a_typed_error_when_the_root_is_a_file(
    tmp_path: Path, hooks: FakeHookRunner
) -> None:
    root_file = tmp_path / "workspaces"
    root_file.write_text("not a directory", encoding="utf-8")
    manager = WorkspaceManager(root_file, hooks)  # type: ignore[arg-type]

    with pytest.raises(WorkspaceCreationError):
        await manager.create_for_issue("ENG-1")

    assert root_file.is_file()
    assert hooks.calls == []


async def test_non_directory_at_the_workspace_path_fails_and_is_not_unlinked(
    manager: WorkspaceManager, hooks: FakeHookRunner
) -> None:
    """Documented policy (SPEC 17.2, CONTRACTS section 5): fail, never unlink."""
    occupied = manager.path_for("ENG-1")
    occupied.write_text("not a workspace", encoding="utf-8")

    with pytest.raises(WorkspaceCreationError):
        await manager.create_for_issue("ENG-1")

    assert occupied.is_file()
    assert occupied.read_text(encoding="utf-8") == "not a workspace"
    assert hooks.calls == []


async def test_create_rejects_a_traversal_identifier_before_touching_the_filesystem(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    with pytest.raises(WorkspacePathEscapesRoot):
        await manager.create_for_issue("..")
    assert sorted(p.name for p in manager.root.iterdir()) == []


# ---------------------------------------------------------------------------
# SPEC 8.6 / 9.4 — cleanup and the before_remove hook
# ---------------------------------------------------------------------------


async def test_cleanup_removes_the_workspace_and_reports_true(
    manager: WorkspaceManager,
) -> None:
    ws = await manager.create_for_issue("ENG-1")
    (Path(ws.path) / "nested" / "deep").mkdir(parents=True)
    (Path(ws.path) / "nested" / "file.txt").write_text("x", encoding="utf-8")

    assert await manager.cleanup("ENG-1") is True
    assert not Path(ws.path).exists()
    assert manager.root.is_dir()


async def test_cleanup_runs_before_remove_non_fatally_while_the_workspace_exists(
    manager: WorkspaceManager, hooks: FakeHookRunner
) -> None:
    ws = await manager.create_for_issue("ENG-1")
    await manager.cleanup("ENG-1")

    assert hooks.names == ["after_create", "before_remove"]
    name, cwd, fatal, cwd_was_dir = hooks.calls[-1]
    assert (name, cwd) == ("before_remove", Path(ws.path))
    assert fatal is False, "SPEC 9.4: before_remove failure is logged and ignored"
    assert cwd_was_dir is True, "before_remove runs before the directory is removed"


async def test_cleanup_of_a_missing_workspace_returns_false_and_runs_no_hook(
    manager: WorkspaceManager, hooks: FakeHookRunner
) -> None:
    assert await manager.cleanup("ENG-404") is False
    assert hooks.calls == []


async def test_cleanup_ignores_a_before_remove_failure(root: Path) -> None:
    hooks = FakeHookRunner(fail_on="before_remove")
    manager = WorkspaceManager(root, hooks)  # type: ignore[arg-type]
    ws = await manager.create_for_issue("ENG-1")

    assert await manager.cleanup("ENG-1") is True
    assert not Path(ws.path).exists()


async def test_cleanup_refuses_to_unlink_a_non_directory(
    manager: WorkspaceManager, hooks: FakeHookRunner
) -> None:
    occupied = manager.path_for("ENG-1")
    occupied.write_text("not a workspace", encoding="utf-8")

    with pytest.raises(WorkspaceError):
        await manager.cleanup("ENG-1")

    assert occupied.is_file()
    assert hooks.calls == []


@requires_symlinks
async def test_cleanup_refuses_to_follow_a_symlinked_workspace(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me", encoding="utf-8")
    manager.path_for("ENG-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspacePathEscapesRoot):
        await manager.cleanup("ENG-1")

    assert (outside / "precious.txt").exists()


async def test_cleanup_rejects_a_traversal_identifier(manager: WorkspaceManager) -> None:
    with pytest.raises(WorkspacePathEscapesRoot):
        await manager.cleanup("..")
    assert manager.root.is_dir()
    assert manager.root.parent.is_dir()


async def test_cleanup_after_recreate_is_a_full_round_trip(
    manager: WorkspaceManager, hooks: FakeHookRunner
) -> None:
    await manager.create_for_issue("ENG-1")
    await manager.cleanup("ENG-1")
    recreated = await manager.create_for_issue("ENG-1")

    assert recreated.created_now is True
    assert hooks.names == ["after_create", "before_remove", "after_create"]


# ---------------------------------------------------------------------------
# SPEC 9.1 — normalization of the configured root
# ---------------------------------------------------------------------------


def test_manager_normalizes_a_relative_root(
    tmp_path: Path, hooks: FakeHookRunner, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    manager = WorkspaceManager(Path("relative-root"), hooks)  # type: ignore[arg-type]
    assert manager.root == normalize_abs(tmp_path / "relative-root")
    assert manager.root.is_absolute()
