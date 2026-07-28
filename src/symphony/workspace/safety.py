"""Filesystem safety invariants — SPEC 9.5, 15.2.

SPEC 9.5 calls itself "the most important portability constraint". Two of its
three invariants are enforced here; the third (Invariant 3, workspace-key
sanitization) is already implemented by :func:`symphony.models.workspace_key`.

The containment check is deliberately paranoid, because the obvious
implementations are wrong in ways that still pass casual tests:

* A string ``startswith`` comparison accepts ``/base/root-evil`` as being under
  ``/base/root``, and accepts ``/base/root/../rootkit`` before normalization.
* A lexical normalization that never resolves symlinks accepts a workspace
  directory that is a symlink pointing anywhere on the filesystem.
* A case-sensitive comparison rejects legitimate Windows paths, and an
  unexpanded 8.3 short name (``C:\\PROGRA~1``) fails to match its long form.

So both operands are made absolute *and* symlink-resolved, then compared as
path **components** under :func:`os.path.normcase`, never as strings.
"""

from __future__ import annotations

import os
from pathlib import Path

from symphony.errors import InvalidWorkspaceCwd, WorkspacePathEscapesRoot

__all__ = ["assert_launch_cwd", "assert_within_root", "normalize_abs"]


def normalize_abs(path: Path | str) -> Path:
    """Absolute, symlink-resolved, normalized form of *path* (SPEC 9.1, 9.5).

    ``Path.resolve()`` is non-strict: it resolves every component that exists,
    including symlinks and Windows 8.3 short names, and normalizes the rest
    lexically. That is what the invariants need — a workspace path is normally
    checked *before* it exists.
    """
    return Path(path).resolve()


def _components(path: Path) -> tuple[str, ...]:
    """Case-folded path components for comparison (never compare paths as strings).

    ``os.path.normcase`` is identity on POSIX and lowercases plus unifies
    separators on Windows, which is exactly the platform difference that
    matters here.
    """
    return tuple(os.path.normcase(part) for part in path.parts)


def assert_within_root(path: Path | str, root: Path | str) -> Path:
    """Enforce SPEC 9.5 Invariant 2: the workspace path stays inside the root.

    Returns the normalized absolute path on success so callers can use the
    validated value rather than the raw one. Raises
    :class:`~symphony.errors.WorkspacePathEscapesRoot` otherwise.

    Containment is *strict*: the root itself is not "inside" the root. That
    closes the case where a degenerate workspace key (``.``) would resolve onto
    the root and make :meth:`WorkspaceManager.cleanup` delete every workspace.
    """
    resolved = normalize_abs(path)
    resolved_root = normalize_abs(root)

    target = _components(resolved)
    prefix = _components(resolved_root)

    if len(target) <= len(prefix) or target[: len(prefix)] != prefix:
        raise WorkspacePathEscapesRoot(
            "workspace path is not inside the configured workspace root",
            path=str(resolved),
            root=str(resolved_root),
        )
    return resolved


def assert_launch_cwd(cwd: Path | str, workspace_path: Path | str) -> None:
    """Enforce SPEC 9.5 Invariant 1: agent cwd == the per-issue workspace path.

    Called immediately before launching the coding-agent subprocess (SPEC 10.1,
    15.2). Raises :class:`~symphony.errors.InvalidWorkspaceCwd` on mismatch.
    """
    resolved_cwd = normalize_abs(cwd)
    resolved_workspace = normalize_abs(workspace_path)

    if _components(resolved_cwd) != _components(resolved_workspace):
        raise InvalidWorkspaceCwd(
            "coding agent cwd must be the per-issue workspace path",
            cwd=str(resolved_cwd),
            workspace_path=str(resolved_workspace),
        )
