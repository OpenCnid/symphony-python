"""Per-issue workspace creation, reuse, and removal — SPEC 9.1, 9.2, 9.3.

The manager owns exactly two hook edges: ``after_create`` (SPEC 9.2 step 5) and
``before_remove`` (SPEC 9.4, driven by the startup terminal cleanup of SPEC
8.6). ``before_run`` and ``after_run`` bracket an *attempt*, not a workspace, so
they belong to the agent runner (SPEC 16.5).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from symphony.errors import WorkspaceCreationError, WorkspaceError
from symphony.models import Workspace, workspace_key
from symphony.workspace.safety import assert_within_root, normalize_abs

if TYPE_CHECKING:  # imported for typing only so the sibling module stays optional
    from symphony.workspace.hooks import HookRunner

__all__ = ["WorkspaceManager"]

_log = logging.getLogger(__name__)


class WorkspaceManager:
    """Maps issue identifiers to durable filesystem workspaces (SPEC 9.1).

    Workspaces are reused across runs for the same issue and are never deleted
    by a successful run (SPEC 9.1). Blocking filesystem work runs in a worker
    thread so the orchestrator event loop is never parked.
    """

    def __init__(self, root: Path, hooks: HookRunner) -> None:
        # SPEC 9.1: the workspace root is a normalized absolute path.
        self.root: Path = normalize_abs(root)
        self.hooks: HookRunner = hooks

    def apply_root(self, root: Path) -> bool:
        """Adopt a reloaded ``workspace.root`` (SPEC 6.2). Returns True if it moved.

        Existing workspaces under the previous root are deliberately left
        untouched: SPEC 9.1 preserves workspaces across runs, and silently
        relocating or deleting live work on a config edit would be far worse
        than leaving directories an operator can see and remove. Runs already
        in flight keep the path they were launched with (SPEC 6.2 does not
        require restarting in-flight sessions).
        """
        new_root = normalize_abs(root)
        if new_root == self.root:
            return False
        self.root = new_root
        return True

    def path_for(self, identifier: str) -> Path:
        """Deterministic absolute workspace path for *identifier* (SPEC 9.1, 9.5).

        The key is derived by :func:`symphony.models.workspace_key` (SPEC 4.2),
        so path derivation is pure and repeatable. Containment (SPEC 9.5
        Invariant 2) is enforced here rather than at the call sites, because
        sanitization alone does not guarantee it: ``..`` and ``.`` survive
        sanitization untouched, and a pre-existing symlink at the workspace
        location can point anywhere.
        """
        return assert_within_root(self.root / workspace_key(identifier), self.root)

    async def create_for_issue(self, identifier: str) -> Workspace:
        """Ensure the workspace directory exists, running ``after_create`` once (SPEC 9.2).

        ``created_now`` is true only when *this* call created the directory, so
        the check and the creation are a single ``mkdir`` rather than an
        ``exists()`` test followed by a ``mkdir``; two workers racing on the
        same identifier therefore cannot both run ``after_create``.
        """
        key = workspace_key(identifier)
        path = self.path_for(identifier)

        created_now = await asyncio.to_thread(self._ensure_directory, path)

        if created_now:
            _log.info("workspace_created workspace_key=%s path=%s", key, path)
            try:
                # SPEC 9.4: after_create failure or timeout is fatal to creation.
                await self.hooks.run("after_create", path, fatal=True)
            except BaseException:
                # SPEC 9.3 permits removing a partially prepared *brand-new*
                # workspace. Doing so keeps after_create's once-per-workspace
                # contract honest: a workspace left behind here would be
                # "reused" on the next attempt and never initialized.
                await asyncio.to_thread(self._discard_new_directory, path)
                raise
        else:
            _log.debug("workspace_reused workspace_key=%s path=%s", key, path)

        return Workspace(path=str(path), workspace_key=key, created_now=created_now)

    async def cleanup(self, identifier: str) -> bool:
        """Remove the workspace for *identifier*; return whether it was removed (SPEC 8.6).

        Runs ``before_remove`` with the workspace as cwd first. Per SPEC 9.4 a
        ``before_remove`` failure or timeout is logged and ignored, so removal
        proceeds regardless. Returns ``False`` when there was nothing to remove.
        """
        path = self.path_for(identifier)

        if not await asyncio.to_thread(self._removable_directory, path):
            return False

        try:
            await self.hooks.run("before_remove", path, fatal=False)
        except Exception:
            # fatal=False means the runner already swallowed it; this guards
            # against a runner that raises anyway, since SPEC 9.4 requires the
            # failure be ignored either way.
            _log.warning("before_remove hook error ignored path=%s", path, exc_info=True)

        await asyncio.to_thread(self._remove_tree, path)
        _log.info("workspace_removed path=%s", path)
        return True

    # -- blocking helpers, always called through asyncio.to_thread -------------

    def _ensure_directory(self, path: Path) -> bool:
        """Create the workspace directory if absent; return True iff created here.

        Policy (SPEC 17.2, CONTRACTS section 5): a non-directory occupying the
        workspace path fails the attempt. Nothing is ever unlinked to make room.
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceCreationError(
                "workspace root could not be created",
                root=str(self.root),
                reason=exc.strerror or str(exc),
            ) from exc

        try:
            path.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorkspaceCreationError(
                "workspace directory could not be created",
                path=str(path),
                reason=exc.strerror or str(exc),
            ) from exc
        else:
            return True

        if not path.is_dir():
            raise WorkspaceCreationError(
                "workspace path exists and is not a directory",
                path=str(path),
            )
        return False

    def _removable_directory(self, path: Path) -> bool:
        """True if a real directory is present, False if nothing is; else raise."""
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise WorkspaceError(
                "workspace path could not be inspected",
                path=str(path),
                reason=exc.strerror or str(exc),
            ) from exc

        if not stat.S_ISDIR(st.st_mode):
            # Never unlink a file or follow a symlink out of the root.
            raise WorkspaceError(
                "workspace path exists and is not a directory; refusing to remove it",
                path=str(path),
            )
        return True

    def _remove_tree(self, path: Path) -> None:
        assert_within_root(path, self.root)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise WorkspaceError(
                "workspace directory could not be removed",
                path=str(path),
                reason=exc.strerror or str(exc),
            ) from exc

    def _discard_new_directory(self, path: Path) -> None:
        """Best-effort removal of a workspace this call created (SPEC 9.3)."""
        try:
            assert_within_root(path, self.root)
            shutil.rmtree(path)
        except Exception:
            # Must not mask the after_create failure being propagated.
            _log.warning("failed to discard partially prepared workspace path=%s", path)
