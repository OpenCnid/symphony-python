"""Dispatch eligibility, sort order, and concurrency accounting — SPEC 8.2, 8.3.

Everything exported here is a pure function of ``(issue, orchestrator state,
config)``. Nothing in this module performs I/O, awaits, or mutates state, so
each predicate can be called in isolation — from a REPL, from a test, or from
the reconciliation and retry paths — to answer exactly one question about one
issue without running a poll tick.

The boundary between :func:`issue_routable` and :func:`should_dispatch` is
load-bearing and deliberate. SPEC 8.2 defines routability as *only* the
adapter's ``dispatchable`` flag plus required-label match, because state,
claims, and concurrency are checked separately by the surrounding algorithm
(SPEC 16.2, 16.3, 8.4). Reconciliation calls :func:`issue_routable` on issues
that are already running; the retry path calls it on issues that are already
claimed. Folding a state or claim check into it would make both of those
callers wrong.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from symphony.models import Issue, OrchestratorState

if TYPE_CHECKING:  # pragma: no cover - config is a sibling module, typing only
    from symphony.workflow.config import ServiceConfig

__all__ = [
    "PRIORITY_BUCKET_MAX",
    "PRIORITY_BUCKET_MIN",
    "REQUIRED_ISSUE_FIELDS",
    "available_slots",
    "dispatch_sort_key",
    "has_state_slot",
    "issue_routable",
    "issue_well_formed",
    "should_dispatch",
    "sort_for_dispatch",
]

# SPEC 8.2 sort tier 1: only these priority values form the privileged,
# ascending bucket. Every other integer -- 0, 5, negatives -- ranks with null.
PRIORITY_BUCKET_MIN = 1
PRIORITY_BUCKET_MAX = 4

# SPEC 8.2 eligibility bullet 1: the four fields an issue MUST carry.
REQUIRED_ISSUE_FIELDS = ("id", "identifier", "title", "state")

# Rank halves used by the sort key. 0 == "in the privileged bucket",
# 1 == "sorts after it". Kept as plain ints so the whole key stays a flat
# comparable tuple with no custom __lt__ anywhere in it.
_RANKED = 0
_UNRANKED = 1


# --------------------------------------------------------------------------
# SPEC 8.2 — sort order
# --------------------------------------------------------------------------


def _priority_rank(priority: object) -> tuple[int, int]:
    """Tier 1 of the SPEC 8.2 sort key.

    Values ``1..4`` sort ascending inside bucket ``0``. Everything else --
    other integers *and* null -- lands in bucket ``1`` with an identical
    secondary value, so they compare equal to each other at this tier and fall
    through to ``created_at``. That is what "all other integers and null sort
    after that bucket" requires: they rank *with* null, not merely after 4.

    ``bool`` is excluded from the bucket for the same reason
    :func:`symphony.trackers.base.coerce_priority` rejects it -- ``True`` is an
    ``int`` subclass that would otherwise masquerade as priority 1.
    """
    if isinstance(priority, bool) or not isinstance(priority, int):
        return (_UNRANKED, 0)
    if PRIORITY_BUCKET_MIN <= priority <= PRIORITY_BUCKET_MAX:
        return (_RANKED, priority)
    return (_UNRANKED, 0)


def _created_rank(created_at: object) -> tuple[int, float]:
    """Tier 2 of the SPEC 8.2 sort key: oldest first, null last.

    The instant is reduced to a POSIX float so a mix of timezone-aware and
    naive datetimes cannot raise ``TypeError`` mid-sort. Naive values are read
    as UTC, matching :func:`symphony.trackers.base.parse_rfc3339`.
    """
    if not isinstance(created_at, datetime):
        return (_UNRANKED, 0.0)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return (_RANKED, created_at.timestamp())


def dispatch_sort_key(issue: Issue) -> tuple[int, int, int, float, str]:
    """The full SPEC 8.2 ordering key for one issue.

    All three tiers compose into one flat tuple so a single stable pass orders
    the whole candidate list:

    ``(priority_bucket, priority_value, created_bucket, created_epoch, identifier)``

    Exported because "why did this issue sort there?" is a question worth
    answering without re-deriving the sort.
    """
    priority_bucket, priority_value = _priority_rank(issue.priority)
    created_bucket, created_value = _created_rank(issue.created_at)
    return (priority_bucket, priority_value, created_bucket, created_value, issue.identifier)


def sort_for_dispatch(issues: Iterable[Issue]) -> list[Issue]:
    """Order dispatch candidates per SPEC 8.2.

    1. ``priority`` ascending for values ``1..4``; all other integers and null
       sort after that bucket.
    2. ``created_at`` oldest first; null sorts last.
    3. ``identifier`` lexicographic tie-breaker.

    ``sorted`` is stable, which satisfies the spec's "stable intent": issues
    whose keys are fully equal keep their input order.
    """
    return sorted(issues, key=dispatch_sort_key)


# --------------------------------------------------------------------------
# SPEC 8.2 — candidate selection
# --------------------------------------------------------------------------


def issue_well_formed(issue: Issue) -> bool:
    """SPEC 8.2 bullet 1: ``id``, ``identifier``, ``title``, and ``state`` present.

    :class:`symphony.models.Issue` already enforces this at construction
    (SPEC 11.3), so this is a redundant guard by design -- the spec states it
    as a dispatch precondition, and a record reaching the scheduler from any
    path that bypassed normalization must not be dispatched.
    """
    for name in REQUIRED_ISSUE_FIELDS:
        value = getattr(issue, name, None)
        if not isinstance(value, str) or not value.strip():
            return False
    return True


def issue_routable(issue: Issue, cfg: ServiceConfig) -> bool:
    """SPEC 8.2: adapter ``dispatchable`` is true and every required label matches.

    Nothing else. This is intentionally narrower than :func:`should_dispatch`:
    state, claims, and concurrency are checked by the surrounding algorithm.
    Active-run reconciliation (SPEC 8.5 / 16.3) and the retry refresh path
    (SPEC 8.4) both call this on issues that are already running or already
    claimed, and would terminate or drop live work if it silently also checked
    state or claims.

    Label matching is delegated to :meth:`symphony.models.Issue.has_labels`,
    which folds case and surrounding whitespace and treats a blank configured
    label as matching no issue (SPEC 5.3.1).
    """
    return issue.dispatchable is True and issue.has_labels(cfg.required_labels)


def should_dispatch(issue: Issue, state: OrchestratorState, cfg: ServiceConfig) -> bool:
    """Full SPEC 8.2 dispatch eligibility -- every bullet, in spec order.

    An issue is dispatch-eligible only if all are true:

    - it has ``id``, ``identifier``, ``title``, and ``state``;
    - its state is in ``active_states`` and not in ``terminal_states``;
    - its adapter-provided ``dispatchable`` value is true;
    - it contains every label in ``tracker.required_labels``;
    - it is not already in ``running``;
    - it is not already in ``claimed``;
    - global concurrency slots are available (SPEC 8.3);
    - per-state concurrency slots are available (SPEC 8.3).

    Retry-queued issues are excluded by the ``claimed`` check: SPEC 16.4 adds
    an issue to ``claimed`` on dispatch and a claim is only dropped by an
    explicit release (SPEC 8.4), so a queued retry is still claimed.
    """
    if not issue_well_formed(issue):
        return False
    if not cfg.is_active(issue.state) or cfg.is_terminal(issue.state):
        return False
    if not issue_routable(issue, cfg):
        return False
    if issue.id in state.running or issue.id in state.claimed:
        return False
    if available_slots(state, cfg) <= 0:
        return False
    return has_state_slot(issue, state, cfg)


# --------------------------------------------------------------------------
# SPEC 8.3 — concurrency control
# --------------------------------------------------------------------------


def available_slots(state_: OrchestratorState, cfg: ServiceConfig) -> int:
    """SPEC 8.3 global limit: ``max(max_concurrent_agents - running_count, 0)``.

    The limit is read from ``cfg`` rather than from
    ``state_.max_concurrent_agents`` so a re-applied config change takes effect
    on the next dispatch decision without a separate state write (SPEC 5.3.5,
    6.2). ``state_`` supplies the live running count and nothing else.
    """
    return max(cfg.max_concurrent_agents - state_.running_count(), 0)


def has_state_slot(issue: Issue, state_: OrchestratorState, cfg: ServiceConfig) -> bool:
    """SPEC 8.3 per-state limit.

    The limit is ``agent.max_concurrent_agents_by_state[state]`` when the state
    has an override, otherwise the global limit; the override lookup and that
    fallback live in :meth:`ServiceConfig.slot_limit_for_state` (CONTRACTS 3),
    which also normalizes the state key.

    The count is taken over issues' *currently tracked* state in the ``running``
    map -- the state on each running entry's latest issue snapshot, which
    reconciliation refreshes every tick (SPEC 8.5), not the state the issue
    held when it was dispatched.
    """
    return state_.running_count_for_state(issue.state) < cfg.slot_limit_for_state(issue.state)
