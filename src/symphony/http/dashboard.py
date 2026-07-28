"""Server-rendered dashboard for the OPTIONAL HTTP server extension — SPEC 13.7.1.

The document is fully self-contained: styles are inlined, there are no scripts,
no fonts, no images and no external requests of any kind, so it renders on an
air-gapped host. The only "liveness" mechanism is a ``<meta http-equiv=refresh>``
tick, which needs nothing from the network.

The renderer reads the SPEC 13.7.2 ``/api/v1/state`` snapshot mapping and
nothing else (SPEC 13.4: a human-readable status surface SHOULD draw from
orchestrator state/metrics only and MUST NOT be REQUIRED for correctness). Every
lookup is defensive — a partial or oddly-typed snapshot degrades to a visible
placeholder instead of raising, because a dashboard render error must not crash
the orchestrator (SPEC 14.2).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from html import escape
from typing import Any

__all__ = [
    "DEFAULT_REFRESH_SECONDS",
    "format_duration",
    "render_dashboard",
    "render_error_page",
]

DEFAULT_REFRESH_SECONDS = 5

_EMPTY = "—"

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #0f1115; --panel: #171a21; --line: #262b36; --text: #e6e9ef;
  --muted: #9aa3b2; --ok: #3fb950; --warn: #d29922; --err: #f85149; --accent: #58a6ff;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --line: #dfe3e9; --text: #1b1f27;
    --muted: #5c6673; --ok: #1a7f37; --warn: #9a6700; --err: #cf222e; --accent: #0969da;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 3rem; background: var(--bg); color: var(--text);
  font: 14px/1.5 ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
header { padding: 1.25rem 1.5rem; border-bottom: 1px solid var(--line); }
h1 { margin: 0 0 .35rem; font-size: 1.05rem; letter-spacing: .04em; text-transform: uppercase; }
h2 {
  margin: 0 0 .6rem; font-size: .8rem; letter-spacing: .09em;
  text-transform: uppercase; color: var(--muted);
}
main { padding: 1.5rem; display: flex; flex-direction: column; gap: 1.75rem; }
.sub { color: var(--muted); font-size: .8rem; }
.pill {
  display: inline-block; padding: .1rem .55rem; border-radius: 999px;
  border: 1px solid currentColor; font-size: .75rem; letter-spacing: .04em;
}
.pill.ok { color: var(--ok); } .pill.warn { color: var(--warn); } .pill.err { color: var(--err); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: .75rem; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: .8rem;
}
.card .k {
  color: var(--muted); font-size: .72rem; letter-spacing: .08em; text-transform: uppercase;
}
.card .v { font-size: 1.35rem; margin-top: .2rem; }
table { width: 100%; border-collapse: collapse; font-size: .82rem; }
th, td {
  text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--line);
  vertical-align: top;
}
th {
  color: var(--muted); font-weight: 600; font-size: .72rem; letter-spacing: .06em;
  text-transform: uppercase;
}
tbody tr:last-child td { border-bottom: none; }
.wrap {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow-x: auto;
}
.empty { padding: .9rem; color: var(--muted); }
a { color: var(--accent); }
code, pre { font-family: inherit; }
pre { margin: 0; padding: .9rem; white-space: pre-wrap; word-break: break-word; }
.err-text { color: var(--err); }
.muted { color: var(--muted); }
ul.events { list-style: none; margin: 0; padding: 0; }
ul.events li { padding: .45rem .6rem; border-bottom: 1px solid var(--line); }
ul.events li:last-child { border-bottom: none; }
footer { padding: 0 1.5rem; color: var(--muted); font-size: .75rem; }
"""


# --------------------------------------------------------------------------
# Defensive accessors — a partial snapshot must render, not raise
# --------------------------------------------------------------------------


def _get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return default


def _rows(snapshot: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = snapshot.get(key)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _text(value: Any) -> str:
    if value is None or value == "":
        return _EMPTY
    return escape(str(value))


def _number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return _EMPTY


def format_duration(seconds: Any) -> str:
    """Render aggregate runtime seconds (SPEC 13.3 ``seconds_running``)."""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return _EMPTY
    if total < 0:
        return _EMPTY
    whole = int(total)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{total:.1f}s"


def _tokens(row: Mapping[str, Any]) -> str:
    tokens = _get(row, "tokens")
    if not isinstance(tokens, Mapping):
        return _EMPTY
    return (
        f"{_number(tokens.get('input_tokens'))} in"
        f" / {_number(tokens.get('output_tokens'))} out"
        f" / {_number(tokens.get('total_tokens'))} total"
    )


def _identifier_cell(row: Mapping[str, Any]) -> str:
    identifier = _get(row, "issue_identifier") or _get(row, "issue_id")
    label = _text(identifier)
    url = _get(row, "issue_url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        # SPEC 13.3: rows SHOULD carry the tracker-provided issue URL.
        return f'<a href="{escape(url, quote=True)}" rel="noreferrer noopener">{label}</a>'
    return label


# --------------------------------------------------------------------------
# Health / error indicators (SPEC 13.7.1)
# --------------------------------------------------------------------------


def _health(snapshot: Mapping[str, Any], running: Iterable[Mapping[str, Any]]) -> tuple[str, str]:
    error = snapshot.get("error")
    if error:
        return "err", f"snapshot error: {error}"
    failing = [r for r in running if _get(r, "last_error")]
    if failing:
        return "err", f"{len(failing)} session error(s)"
    if snapshot.get("degraded"):
        return "warn", "degraded"
    return "ok", "healthy"


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def _cards(snapshot: Mapping[str, Any], running: list[Any], retrying: list[Any]) -> str:
    counts = snapshot.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    running_n = counts.get("running", len(running))
    retrying_n = counts.get("retrying", len(retrying))

    totals = snapshot.get("codex_totals")
    totals = totals if isinstance(totals, Mapping) else {}

    cells = [
        ("running sessions", _number(running_n)),
        ("retry queue", _number(retrying_n)),
        ("input tokens", _number(totals.get("input_tokens"))),
        ("output tokens", _number(totals.get("output_tokens"))),
        ("total tokens", _number(totals.get("total_tokens"))),
        ("runtime", format_duration(totals.get("seconds_running"))),
    ]
    body = "".join(
        f'<div class="card"><div class="k">{escape(k)}</div><div class="v">{v}</div></div>'
        for k, v in cells
    )
    return f'<section><h2>totals</h2><div class="cards">{body}</div></section>'


_RUNNING_COLUMNS = (
    "issue",
    "state",
    "session",
    "turns",
    "started",
    "last event",
    "last event at",
    "tokens",
)


def _running_section(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return _section("running sessions", '<div class="empty">no active sessions</div>')
    head = "".join(f"<th>{escape(c)}</th>" for c in _RUNNING_COLUMNS)
    body = []
    for row in rows:
        message = _get(row, "last_message")
        error = _get(row, "last_error")
        detail = ""
        if error:
            detail = f'<div class="err-text">{_text(error)}</div>'
        elif message:
            detail = f'<div class="muted">{_text(message)}</div>'
        body.append(
            "<tr>"
            f"<td>{_identifier_cell(row)}</td>"
            f"<td>{_text(_get(row, 'state'))}</td>"
            f"<td>{_text(_get(row, 'session_id'))}</td>"
            f"<td>{_number(_get(row, 'turn_count', 0))}</td>"
            f"<td>{_text(_get(row, 'started_at'))}</td>"
            f"<td>{_text(_get(row, 'last_event'))}{detail}</td>"
            f"<td>{_text(_get(row, 'last_event_at'))}</td>"
            f"<td>{_tokens(row)}</td>"
            "</tr>"
        )
    table = f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    return _section("running sessions", table)


_RETRY_COLUMNS = ("issue", "attempt", "due at", "error")


def _retry_section(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return _section("retry queue", '<div class="empty">no queued retries</div>')
    head = "".join(f"<th>{escape(c)}</th>" for c in _RETRY_COLUMNS)
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_identifier_cell(row)}</td>"
            f"<td>{_number(_get(row, 'attempt', 0))}</td>"
            f"<td>{_text(_get(row, 'due_at'))}</td>"
            f'<td class="err-text">{_text(_get(row, "error"))}</td>'
            "</tr>"
        )
    table = f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    return _section("retry queue", table)


def _events_section(snapshot: Mapping[str, Any], running: list[Mapping[str, Any]]) -> str:
    """Recent activity.

    Prefers a top-level ``recent_events`` list if the snapshot provider supplies
    one (implementations MAY add fields, SPEC 13.7.2); otherwise it is derived
    from the last-event columns the baseline shape guarantees.
    """
    events: list[tuple[str, str, str]] = []
    for event in _rows(snapshot, "recent_events"):
        events.append(
            (
                str(_get(event, "at", "") or ""),
                str(_get(event, "issue_identifier", "") or ""),
                f"{_get(event, 'event', '') or ''} {_get(event, 'message', '') or ''}".strip(),
            )
        )
    if not events:
        for row in running:
            last_event = _get(row, "last_event")
            if not last_event:
                continue
            events.append(
                (
                    str(_get(row, "last_event_at", "") or ""),
                    str(_get(row, "issue_identifier", "") or ""),
                    f"{last_event} {_get(row, 'last_message', '') or ''}".strip(),
                )
            )

    if not events:
        return _section("recent events", '<div class="empty">no events recorded</div>')

    events.sort(key=lambda e: e[0], reverse=True)
    items = "".join(
        f'<li><span class="muted">{_text(at)}</span> '
        f"<strong>{_text(who)}</strong> {_text(what)}</li>"
        for at, who, what in events[:50]
    )
    return _section("recent events", f'<ul class="events">{items}</ul>')


def _rate_limit_section(snapshot: Mapping[str, Any]) -> str:
    limits = snapshot.get("rate_limits")
    if not limits:
        return _section("rate limits", '<div class="empty">none reported</div>')
    try:
        rendered = json.dumps(limits, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(limits)
    return _section("rate limits", f"<pre>{escape(rendered)}</pre>")


def _section(title: str, inner: str) -> str:
    return f'<section><h2>{escape(title)}</h2><div class="wrap">{inner}</div></section>'


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def render_dashboard(
    snapshot: Mapping[str, Any],
    *,
    refresh_seconds: int | None = DEFAULT_REFRESH_SECONDS,
) -> str:
    """Render the SPEC 13.7.1 dashboard from a SPEC 13.7.2 state snapshot.

    ``refresh_seconds`` emits a meta-refresh so the page stays current without
    scripts or network calls; pass ``None`` to render a static document.
    """
    # Checked despite the annotation: the provider is a sibling module and a
    # render error must not become an orchestrator failure (SPEC 14.2).
    incoming: Any = snapshot
    snapshot = incoming if isinstance(incoming, Mapping) else {}

    running = _rows(snapshot, "running")
    retrying = _rows(snapshot, "retrying")
    level, label = _health(snapshot, running)

    meta_refresh = ""
    if refresh_seconds is not None and refresh_seconds > 0:
        meta_refresh = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'

    generated_at = _text(snapshot.get("generated_at"))
    body = "".join(
        [
            _cards(snapshot, running, retrying),
            _running_section(running),
            _retry_section(retrying),
            _events_section(snapshot, running),
            _rate_limit_section(snapshot),
        ]
    )

    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{meta_refresh}"
        "<title>Symphony — runtime dashboard</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        "<header>"
        "<h1>Symphony runtime</h1>"
        f'<div class="sub"><span class="pill {level}">{escape(label)}</span> '
        f"generated at {generated_at}</div>"
        "</header>"
        f"<main>{body}</main>"
        "<footer>observability surface only — SPEC 13.7. "
        'JSON: <a href="/api/v1/state">/api/v1/state</a></footer>'
        "</body></html>"
    )


def render_error_page(message: str) -> str:
    """Fallback document for a failed render (SPEC 14.1 class 5, SPEC 14.2).

    A dashboard render error is an observability failure. It is shown here and
    never propagated: the orchestrator keeps running.
    """
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>Symphony — dashboard unavailable</title>"
        f"<style>{_STYLE}</style></head><body>"
        "<header><h1>Symphony runtime</h1>"
        '<div class="sub"><span class="pill err">dashboard unavailable</span></div></header>'
        f'<main><section><div class="wrap"><pre>{escape(message)}</pre></div></section>'
        '<section><div class="wrap"><div class="empty">'
        "The orchestrator is unaffected; this page is an observability surface only "
        "(SPEC 13.7, 14.2).</div></div></section></main>"
        '<footer>JSON: <a href="/api/v1/state">/api/v1/state</a></footer>'
        "</body></html>"
    )
