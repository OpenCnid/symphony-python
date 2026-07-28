"""Symphony trackers package.

Importing this package registers every bundled adapter. Registration is an
import side effect of each adapter module — the ``@register_adapter`` decorator
runs at module scope — so ``build_adapter("linear", ...)`` raises
``UnsupportedTrackerKind`` if the module was never imported. The eager imports
below are what make ``tracker.kind`` resolvable from a bare ``WORKFLOW.md``
without the caller knowing which module to reach for.

Third-party adapters register the same way: import the module, and its ``kind``
becomes available to :func:`symphony.trackers.base.build_adapter`.
"""

from __future__ import annotations

from symphony.trackers import github, linear, memory
from symphony.trackers.base import (
    ToolContext,
    ToolResult,
    ToolSpec,
    TrackerAdapter,
    adapter_kinds,
    build_adapter,
    register_adapter,
)

__all__ = [
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "TrackerAdapter",
    "adapter_kinds",
    "build_adapter",
    "register_adapter",
    "github",
    "linear",
    "memory",
]
