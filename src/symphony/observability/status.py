"""Human-readable status surface — SPEC 13.4.

Renders the orchestrator's runtime state as a plain-text terminal report. Two
constraints from SPEC 13.4 shape the whole module:

* It draws from orchestrator state/metrics only — every value shown comes from
  :func:`symphony.observability.snapshot.build_snapshot` or directly off
  :class:`~symphony.models.OrchestratorState`. There is no separate bookkeeping
  to drift out of sync.
* It MUST NOT be required for correctness. Nothing here mutates state, and
  rendering tolerates half-populated sessions (a worker that has not reported
  a session id yet) without raising.

Output is ASCII-only and uncolored so it is readable in a Windows console, a
CI log, and a Recursive Language Model's transcript alike.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, TextIO

from symphony.models import OrchestratorState

from .snapshot import build_snapshot, elapsed_seconds

__all__ = [
    "format_duration",
    "format_tokens",
    "render_status",
    "print_status",
]

_MAX_CELL_CHARS = 48
_TRUNCATION_SUFFIX = "..."


def format_duration(seconds: float | int | None) -> str:
    """Compact duration: ``45s``, ``30m34s``, ``2h13m``, ``-`` for unknown."""
    if seconds is None:
        return "-"
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if total < 0:
        total = 0.0
    whole = int(total)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{(whole % 3600) // 60:02d}m"


def format_tokens(count: Any) -> str:
    """Group thousands so long counters stay scannable."""
    try:
        return f"{int(count):,}"
    except (TypeError, ValueError):
        return "-"


def _cell(value: Any, limit: int = _MAX_CELL_CHARS) -> str:
    if value is None or value == "":
        return "-"
    text = " ".join(str(value).split())
    if len(text) > limit:
        return text[: limit - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX
    return text


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], indent: str = "  ") -> list[str]:
    """Left-aligned fixed-width table; the last column is not padded."""
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def line(cells: Sequence[str]) -> str:
        parts = [
            cell if index == len(cells) - 1 else cell.ljust(widths[index])
            for index, cell in enumerate(cells)
        ]
        return (indent + "  ".join(parts)).rstrip()

    return [line(headers), *(line(row) for row in rows)]


def render_status(
    state: OrchestratorState,
    *,
    now: datetime | None = None,
    monotonic_ms: float | None = None,
) -> str:
    """Render the SPEC 13.4 status report as text.

    Pure: *state* is only read. ``now`` and ``monotonic_ms`` are injectable so
    a caller (or a test) gets deterministic output.
    """
    reference = now if now is not None else datetime.now(UTC)
    snapshot = build_snapshot(state, now=reference, monotonic_ms=monotonic_ms)

    counts = snapshot["counts"]
    totals = snapshot["codex_totals"]
    lines = [
        f"Symphony status  generated_at={snapshot['generated_at']}",
        f"  running={counts['running']}/{state.max_concurrent_agents}"
        f"  retrying={counts['retrying']}"
        f"  claimed={len(state.claimed)}"
        f"  completed={len(state.completed)}"
        f"  poll_interval_ms={state.poll_interval_ms}",
        "",
    ]

    lines.append(f"RUNNING ({counts['running']})")
    if snapshot["running"]:
        rows = []
        for row in snapshot["running"]:
            entry = state.running.get(str(row["issue_id"]))
            started = entry.started_at if entry is not None else None
            rows.append(
                [
                    _cell(row["issue_identifier"], 20),
                    _cell(row["state"], 18),
                    str(row["turn_count"]),
                    format_duration(elapsed_seconds(started, reference)),
                    format_tokens(row["tokens"]["total_tokens"]),
                    _cell(row["last_event"], 24),
                    _cell(row["last_message"], 40),
                ]
            )
        lines.extend(
            _table(
                ["ISSUE", "STATE", "TURNS", "ELAPSED", "TOKENS", "LAST EVENT", "LAST MESSAGE"],
                rows,
            )
        )
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"RETRYING ({counts['retrying']})")
    if snapshot["retrying"]:
        rows = []
        for row in snapshot["retrying"]:
            retry_entry = state.retry_attempts.get(str(row["issue_id"]))
            due_in = _due_in_seconds(retry_entry, monotonic_ms)
            rows.append(
                [
                    _cell(row["issue_identifier"] or row["issue_id"], 20),
                    str(row["attempt"]),
                    format_duration(due_in) if due_in is not None else "-",
                    _cell(row["error"], 48),
                ]
            )
        lines.extend(_table(["ISSUE", "ATTEMPT", "DUE IN", "ERROR"], rows))
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(
        "TOTALS  "
        f"input={format_tokens(totals['input_tokens'])}"
        f"  output={format_tokens(totals['output_tokens'])}"
        f"  total={format_tokens(totals['total_tokens'])}"
        f"  runtime={format_duration(totals['seconds_running'])}"
    )
    lines.append(f"RATE LIMITS  {_format_rate_limits(snapshot['rate_limits'])}")
    return "\n".join(lines)


def _due_in_seconds(entry: Any, monotonic_ms: float | None) -> float | None:
    """Seconds until a retry fires, from its monotonic due time (SPEC 4.1.7)."""
    if entry is None:
        return None
    reference = monotonic_ms if monotonic_ms is not None else time.monotonic() * 1000.0
    try:
        return max(0.0, (float(entry.due_at_ms) - float(reference)) / 1000.0)
    except (TypeError, ValueError):
        return None


def _format_rate_limits(rate_limits: Any) -> str:
    """SPEC 13.5: presentation of rate-limit data is implementation-defined."""
    if not rate_limits:
        return "(none)"
    if isinstance(rate_limits, dict):
        parts = [f"{key}={_cell(value, 24)}" for key, value in sorted(rate_limits.items())]
        return "  ".join(parts) if parts else "(none)"
    return _cell(rate_limits, 80)


def print_status(
    state: OrchestratorState,
    *,
    stream: TextIO | None = None,
    now: datetime | None = None,
    monotonic_ms: float | None = None,
) -> None:
    """Write :func:`render_status` to *stream* (stdout by default)."""
    target = stream if stream is not None else sys.stdout
    target.write(render_status(state, now=now, monotonic_ms=monotonic_ms) + "\n")
    target.flush()
