"""HTTP handlers and HTML templates for the dashboard.

The handlers are intentionally minimal: each renders a small, server-side
HTML page (Jinja2) plus, where appropriate, a JSON sibling under ``/api``.
The browser-side code is one short script per page — just enough to drive
the SSE event stream and the approval buttons.

Output safety: every value rendered into the DOM ultimately originates
from plugin-supplied :class:`WorldEvent`\\s and must be treated as
**tainted** input.  Server-side rendering relies on Jinja's autoescape;
the SSE client script never uses ``innerHTML`` on event-derived strings —
rows are constructed with :func:`document.createElement` and
``textContent`` so HTML in an event subject (e.g. an attacker-controlled
email) cannot execute in the dashboard origin.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiohttp import web
from jinja2 import Environment, select_autoescape

from coremind.dashboard.auth import DashboardAuth
from coremind.dashboard.data import DashboardDataSources
from coremind.notify.port import ApprovalResponse

log = structlog.get_logger(__name__)

DATA_SOURCES_KEY: web.AppKey[DashboardDataSources] = web.AppKey(
    "coremind.dashboard.data_sources",
)
AUTH_KEY: web.AppKey[DashboardAuth | None] = web.AppKey(
    "coremind.dashboard.auth",
)

# How far back ``/events`` looks when the page first loads.
_INITIAL_EVENT_WINDOW = timedelta(days=1)
# Maximum entries surfaced per page; keeps payloads bounded.
_PAGE_LIMIT = 100


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


_BASE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CoreMind — {{ title }}</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.4 system-ui, sans-serif; margin: 0; }
  header { background: #1d1f24; color: #fff; padding: 12px 20px; }
  header a { color: #9ad; margin-right: 14px; text-decoration: none; }
  header a.active { color: #fff; font-weight: 600; }
  main { padding: 20px; max-width: 1100px; }
  h1 { margin-top: 0; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border-bottom: 1px solid #ddd; padding: 6px 8px; text-align: left;
           vertical-align: top; }
  th { background: #f5f5f5; }
  pre { background: #f7f7f9; padding: 8px; overflow-x: auto; }
  .muted { color: #888; }
  .pill { display: inline-block; padding: 1px 6px; border-radius: 8px;
          font-size: 12px; background: #e0e0e0; }
  .pill.ask { background: #fde2cf; }
  .pill.suggest { background: #d6e4ff; }
  .pill.safe { background: #d6f5dc; }
  button { font: inherit; padding: 4px 10px; cursor: pointer; }
  .row-actions { display: flex; gap: 6px; }
</style>
</head>
<body>
<header>
  <strong>CoreMind</strong>
  {% for key, label in nav %}
    <a href="{{ key }}" class="{% if key == active %}active{% endif %}">{{ label }}</a>
  {% endfor %}
</header>
<main>
{{ body | safe }}
</main>
</body>
</html>
"""


_OVERVIEW_BODY = """
<h1>Overview</h1>
<p class="muted">Read-only view of the running daemon.</p>
<table>
  <tr><th>World entities</th>
    <td data-testid="entity-count">{{ entity_count }}</td></tr>
  <tr><th>Relationships</th>
    <td data-testid="relationship-count">{{ relationship_count }}</td></tr>
  <tr><th>Recent events (last 24h)</th>
    <td data-testid="recent-event-count">{{ recent_event_count }}</td></tr>
  <tr><th>Pending intents</th>
    <td data-testid="pending-intents">{{ pending_intents }}</td></tr>
  <tr><th>Journal entries (recent)</th>
    <td data-testid="journal-entries">{{ journal_entries }}</td></tr>
  <tr><th>Reflection reports</th>
    <td data-testid="reflection-reports">{{ reflection_reports }}</td></tr>
  <tr><th>Pending approvals</th>
    <td data-testid="pending-approvals">{{ pending_notifications }}</td></tr>
</table>
"""


_EVENTS_BODY = """
<h1>Live events</h1>
<p class="muted">Streaming via Server-Sent Events from <code>/api/events/stream</code>.
Initial window: last 24 hours.</p>
<table id="events">
  <thead><tr><th>Timestamp</th><th>Source</th><th>Entity</th>
  <th>Attribute</th><th>Value</th></tr></thead>
  <tbody>
  {% for event in initial_events %}
    <tr>
      <td>{{ event.timestamp.isoformat() }}</td>
      <td>{{ event.source }}</td>
      <td>{{ event.entity.type }}:{{ event.entity.id }}</td>
      <td>{{ event.attribute }}</td>
      <td><code>{{ event.value | tojson }}</code></td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<script>
  // Event payloads originate from plugin-supplied WorldEvents and are treated
  // as tainted input.  We construct cells with createElement + textContent so
  // an HTML-bearing subject line / entity id / attribute name cannot execute
  // script in the dashboard origin (which is the only origin authorized to
  // submit approvals).  See views.py module docstring.
  const tbody = document.querySelector('#events tbody');
  const src = new EventSource('/api/events/stream');
  function appendCell(row, text) {
    const td = document.createElement('td');
    td.textContent = text;
    row.appendChild(td);
  }
  src.addEventListener('event', (msg) => {
    let e;
    try { e = JSON.parse(msg.data); } catch (err) { return; }
    const row = document.createElement('tr');
    appendCell(row, String(e.timestamp ?? ''));
    appendCell(row, String(e.source ?? ''));
    const entity = e.entity || {};
    appendCell(row, String(entity.type ?? '') + ':' + String(entity.id ?? ''));
    appendCell(row, String(e.attribute ?? ''));
    const valueCell = document.createElement('td');
    const code = document.createElement('code');
    code.textContent = JSON.stringify(e.value);
    valueCell.appendChild(code);
    row.appendChild(valueCell);
    tbody.insertBefore(row, tbody.firstChild);
  });
</script>
"""


_GRAPH_BODY = """
<h1>World graph</h1>
<p class="muted">Force-directed layout is delegated to the JSON consumer at
<code>/api/graph</code>. This server-side preview lists the same nodes and edges.</p>
<h2>Entities ({{ entities | length }})</h2>
<table>
  <thead><tr><th>Type</th><th>Display name</th><th>Sources</th></tr></thead>
  <tbody>
  {% for entity in entities %}
    <tr>
      <td>{{ entity.type }}</td>
      <td>{{ entity.display_name }}</td>
      <td>{{ entity.source_plugins | join(', ') }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<h2>Relationships ({{ relationships | length }})</h2>
<table>
  <thead><tr><th>From</th><th>To</th><th>Type</th><th>Weight</th></tr></thead>
  <tbody>
  {% for rel in relationships %}
    <tr>
      <td>{{ rel.from_entity.type }}:{{ rel.from_entity.id }}</td>
      <td>{{ rel.to_entity.type }}:{{ rel.to_entity.id }}</td>
      <td>{{ rel.type }}</td>
      <td>{{ "%.2f"|format(rel.weight) }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
"""


_REASONING_BODY = """
<h1>Reasoning cycles</h1>
<table>
  <thead><tr><th>Cycle</th><th>Timestamp</th><th>Model</th>
  <th>Patterns</th><th>Anomalies</th><th>Predictions</th></tr></thead>
  <tbody>
  {% for cycle in cycles %}
    <tr>
      <td>{{ cycle.cycle_id }}</td>
      <td>{{ cycle.timestamp.isoformat() }}</td>
      <td>{{ cycle.model_used }}</td>
      <td>{{ cycle.patterns | length }}</td>
      <td>{{ cycle.anomalies | length }}</td>
      <td>{{ cycle.predictions | length }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
"""


_INTENTS_BODY = """
<h1>Intents</h1>
<table>
  <thead><tr><th>Created</th><th>Question</th><th>Category</th>
  <th>Status</th><th>Salience</th><th>Confidence</th></tr></thead>
  <tbody>
  {% for intent in intents %}
    <tr>
      <td>{{ intent.created_at.isoformat() }}</td>
      <td>{{ intent.question.text }}</td>
      <td><span class="pill {{ intent.category }}">{{ intent.category }}</span></td>
      <td>{{ intent.status }}</td>
      <td>{{ "%.2f"|format(intent.salience) }}</td>
      <td>{{ "%.2f"|format(intent.confidence) }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% if pending_notifications %}
<h2>Pending approvals</h2>
<table>
  <thead><tr><th>Sent</th><th>Message</th><th>Intent</th><th>Actions</th></tr></thead>
  <tbody>
  {% for note in pending_notifications %}
    <tr>
      <td>{{ note.sent_at.isoformat() }}</td>
      <td>{{ note.message }}</td>
      <td>{{ note.intent_id or '—' }}</td>
      <td>
        <div class="row-actions">
        {% for action in note.actions %}
          <button data-intent="{{ note.intent_id }}" data-decision="{{ action.value }}">
            {{ action.label }}
          </button>
        {% endfor %}
        </div>
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<script>
  document.querySelectorAll('button[data-intent]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const body = JSON.stringify({
        intent_id: btn.dataset.intent,
        decision: btn.dataset.decision,
      });
      const r = await fetch('/api/approvals', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body,
      });
      if (r.ok) { btn.disabled = true; btn.textContent = '✓ ' + btn.textContent; }
    });
  });
</script>
{% endif %}
"""


_ACTIONS_BODY = """
<h1>Action journal</h1>
<form method="get">
  <label>Search:
    <input type="text" name="q" value="{{ query }}" placeholder="action_class or operation">
  </label>
  <button type="submit">Filter</button>
</form>
<table>
  <thead><tr><th>Seq</th><th>Timestamp</th><th>Kind</th>
  <th>Operation</th><th>Class</th><th>Outcome</th></tr></thead>
  <tbody>
  {% for entry in entries %}
    <tr>
      <td>{{ entry.seq }}</td>
      <td>{{ entry.timestamp.isoformat() }}</td>
      <td>{{ entry.kind }}</td>
      <td>{{ entry.operation }}</td>
      <td>{{ entry.action_class }}</td>
      <td>{{ entry.outcome }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
"""


_REFLECTION_BODY = """
<h1>Reflection reports</h1>
{% if not reports %}
<p class="muted">No reports archived yet.</p>
{% endif %}
{% for stored in reports %}
<section>
  <h2>{{ stored.report.window_start.isoformat() }} → {{ stored.report.window_end.isoformat() }}</h2>
  <p class="muted">Cycle id: <code>{{ stored.report.cycle_id }}</code> ·
  Predictions evaluated: {{ stored.report.predictions.evaluated }} ·
  {% set brier = stored.report.calibration.brier_score %}
  Brier: {{ brier if brier is not none else "n/a" }}</p>
  <pre>{{ stored.report.markdown }}</pre>
</section>
{% endfor %}
"""


_NAV: list[tuple[str, str]] = [
    ("/", "Overview"),
    ("/events", "Events"),
    ("/graph", "Graph"),
    ("/reasoning", "Reasoning"),
    ("/intents", "Intents"),
    ("/actions", "Actions"),
    ("/reflection", "Reflection"),
]


_env = Environment(
    autoescape=select_autoescape(["html"]),
    enable_async=False,
    keep_trailing_newline=False,
)
# Override Jinja's built-in ``tojson``: the default refuses non-JSON-native
# types (e.g. ``datetime``).  We accept those by stringifying them, and rely
# on Jinja's autoescape on the surrounding HTML to keep the output safe.
_env.filters["tojson"] = lambda value: json.dumps(value, default=str, sort_keys=True)


def _render(active: str, title: str, body_template: str, **context: Any) -> str:
    body = _env.from_string(body_template).render(**context)
    return _env.from_string(_BASE_HTML).render(
        title=title,
        active=active,
        nav=_NAV,
        body=body,
    )


def _data(request: web.Request) -> DashboardDataSources:
    return request.app[DATA_SOURCES_KEY]


def _auth(request: web.Request) -> DashboardAuth | None:
    return request.app[AUTH_KEY]


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


# A strict CSP is enforceable because every page's client script is inlined
# from a server-controlled template (no third-party JS, no remote assets) and
# autoescape covers every user-derived string that reaches the DOM.  Even so,
# we forbid framing and external loads so a future XSS regression cannot be
# weaponised into clickjacking or data exfiltration.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)


@web.middleware
async def security_headers_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    """Attach defense-in-depth security headers to every response.

    The dashboard binds to loopback by default, but loopback is not a
    sufficient trust boundary on multi-user hosts.  These headers harden
    the surface against clickjacking, MIME sniffing, and stray referrer
    leaks, and constrain script execution if a future regression
    reintroduces an XSS sink.
    """
    response: web.StreamResponse = await handler(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


# ---------------------------------------------------------------------------
# Page handlers
# ---------------------------------------------------------------------------


async def overview(request: web.Request) -> web.Response:
    """Render the landing page with summary counts."""
    data = _data(request)
    entity_count = 0
    relationship_count = 0
    recent_event_count = 0
    if data.world is not None:
        snapshot = await data.world.snapshot()
        entity_count = len(snapshot.entities)
        relationship_count = len(snapshot.relationships)
        since = datetime.now(UTC) - _INITIAL_EVENT_WINDOW
        recent_event_count = len(await data.world.recent_events(since=since, limit=10_000))
    pending_intents = 0
    if data.intents is not None:
        pending_intents = len(await data.intents.list(status="pending", limit=10_000))
    journal_entries = 0
    if data.journal is not None:
        journal_entries = len(await data.journal.read_recent(limit=_PAGE_LIMIT))
    reflection_reports = 0
    if data.reflection is not None:
        reflection_reports = len(await data.reflection.list_reports(limit=10_000))
    pending_notifications = 0
    if data.notifications is not None:
        # ``pending()`` reflects unresolved approvals (entries are dropped
        # when a matching :class:`ApprovalResponse` is submitted), unlike
        # ``history`` which is the lifetime delivery log.
        pending_notifications = len(data.notifications.pending())
    html = _render(
        "/",
        "Overview",
        _OVERVIEW_BODY,
        entity_count=entity_count,
        relationship_count=relationship_count,
        recent_event_count=recent_event_count,
        pending_intents=pending_intents,
        journal_entries=journal_entries,
        reflection_reports=reflection_reports,
        pending_notifications=pending_notifications,
    )
    return web.Response(text=html, content_type="text/html")


async def events_page(request: web.Request) -> web.Response:
    """Render the live events page with a one-day backfill."""
    data = _data(request)
    initial: list[Any] = []
    if data.world is not None:
        since = datetime.now(UTC) - _INITIAL_EVENT_WINDOW
        initial = list(reversed(await data.world.recent_events(since=since, limit=_PAGE_LIMIT)))
    html = _render("/events", "Events", _EVENTS_BODY, initial_events=initial)
    return web.Response(text=html, content_type="text/html")


async def events_stream(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events endpoint backed by an :class:`EventSubscriber`."""
    data = _data(request)
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    if data.events is None:
        # No live source; close the stream immediately so the client falls
        # back to a no-op rather than reconnecting forever.
        await response.write(b"event: end\ndata: {}\n\n")
        return response
    iterator = data.events.subscribe()
    try:
        async for event in iterator:
            payload = event.model_dump(mode="json")
            line = f"event: event\ndata: {json.dumps(payload)}\n\n".encode()
            await response.write(line)
    except (asyncio.CancelledError, ConnectionResetError):
        log.debug("dashboard.events_stream.client_disconnected")
        raise
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()
    return response


async def graph_page(request: web.Request) -> web.Response:
    """Render the world-graph view (server-side table + JSON sibling)."""
    data = _data(request)
    entities: list[Any] = []
    relationships: list[Any] = []
    if data.world is not None:
        snapshot = await data.world.snapshot()
        entities = snapshot.entities
        relationships = snapshot.relationships
    html = _render(
        "/graph",
        "Graph",
        _GRAPH_BODY,
        entities=entities,
        relationships=relationships,
    )
    return web.Response(text=html, content_type="text/html")


async def graph_json(request: web.Request) -> web.Response:
    """Return the world graph as ``{nodes: [...], edges: [...]}`` JSON."""
    data = _data(request)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    if data.world is not None:
        snapshot = await data.world.snapshot()
        for entity in snapshot.entities:
            nodes.append(
                {
                    "type": entity.type,
                    "display_name": entity.display_name,
                    "properties": entity.properties,
                },
            )
        for rel in snapshot.relationships:
            edges.append(
                {
                    "from": f"{rel.from_entity.type}:{rel.from_entity.id}",
                    "to": f"{rel.to_entity.type}:{rel.to_entity.id}",
                    "type": rel.type,
                    "weight": rel.weight,
                },
            )
    return web.json_response({"nodes": nodes, "edges": edges})


async def reasoning_page(request: web.Request) -> web.Response:
    """Render the recent reasoning cycles."""
    data = _data(request)
    cycles: list[Any] = []
    if data.cycles is not None:
        cycles = await data.cycles.list_cycles(limit=_PAGE_LIMIT)
    html = _render("/reasoning", "Reasoning", _REASONING_BODY, cycles=cycles)
    return web.Response(text=html, content_type="text/html")


async def intents_page(request: web.Request) -> web.Response:
    """Render the intent queue plus any pending dashboard approvals."""
    data = _data(request)
    intents: list[Any] = []
    if data.intents is not None:
        intents = await data.intents.list(limit=_PAGE_LIMIT)
    pending = list(data.notifications.pending()) if data.notifications is not None else []
    html = _render(
        "/intents",
        "Intents",
        _INTENTS_BODY,
        intents=intents,
        pending_notifications=pending,
    )
    return web.Response(text=html, content_type="text/html")


async def actions_page(request: web.Request) -> web.Response:
    """Render the action journal with a simple substring filter."""
    data = _data(request)
    query = request.query.get("q", "").strip().lower()
    rows: list[dict[str, Any]] = []
    if data.journal is not None:
        # Read-recent caps the journal scan to the page window so this
        # handler stays O(_PAGE_LIMIT) regardless of journal age.
        for entry in await data.journal.read_recent(limit=_PAGE_LIMIT):
            payload = entry.payload
            operation = str(payload.get("operation", ""))
            action_class = str(payload.get("action_class", ""))
            result = payload.get("result")
            outcome = ""
            if isinstance(result, dict):
                outcome = str(result.get("status", ""))
            haystack = f"{operation} {action_class}".lower()
            if query and query not in haystack:
                continue
            rows.append(
                {
                    "seq": entry.seq,
                    "timestamp": entry.timestamp,
                    "kind": entry.kind,
                    "operation": operation,
                    "action_class": action_class,
                    "outcome": outcome,
                },
            )
    rows.sort(key=lambda r: r["seq"], reverse=True)
    html = _render(
        "/actions",
        "Actions",
        _ACTIONS_BODY,
        entries=rows[:_PAGE_LIMIT],
        query=query,
    )
    return web.Response(text=html, content_type="text/html")


async def reflection_page(request: web.Request) -> web.Response:
    """Render the archived reflection reports newest-first."""
    data = _data(request)
    reports: list[Any] = []
    if data.reflection is not None:
        reports = await data.reflection.list_reports(limit=_PAGE_LIMIT)
    html = _render("/reflection", "Reflection", _REFLECTION_BODY, reports=reports)
    return web.Response(text=html, content_type="text/html")


# ---------------------------------------------------------------------------
# Approval submission (the only state-changing endpoint).
# ---------------------------------------------------------------------------


def _extract_bearer_token(request: web.Request) -> str | None:
    """Pull the bearer token out of the ``Authorization`` header."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def _request_origin(request: web.Request) -> str | None:
    """Return the request origin for CSRF policy checks.

    Browsers always populate ``Origin`` for ``fetch``-driven POSTs; we fall
    back to ``Referer`` (origin component only) so curl-style smoke tests
    can still authenticate when paired with a valid token, but we never
    treat a missing origin as "trusted".
    """
    origin = request.headers.get("Origin")
    if origin:
        return origin
    referer = request.headers.get("Referer")
    if not referer:
        return None
    # Coarse origin extraction (scheme://host[:port]) without pulling in
    # urllib for a single field; sufficient for an exact-match policy.
    parts = referer.split("/")
    # Expect at least ``scheme:``, ``""`` (the empty token between ``//``),
    # and the host segment — i.e. three slash-separated parts.
    min_referer_parts = 3
    if len(parts) < min_referer_parts:
        return None
    return f"{parts[0]}//{parts[2]}"


async def submit_approval(request: web.Request) -> web.Response:  # noqa: PLR0911 — each return reports a distinct authorization or validation failure mode.
    """Forward a user decision to :class:`DashboardNotificationPort`.

    Authentication and CSRF policy:

    - Requires ``Authorization: Bearer <token>`` matching the configured
      :attr:`DashboardAuth.api_token`.  Loopback binding alone is not a
      sufficient trust boundary; without this gate any local UID could
      approve forced-approval-class intents.
    - Requires ``Origin`` (or ``Referer`` when ``Origin`` is absent) to
      match :attr:`DashboardAuth.allowed_origins` so a drive-by request
      from another localhost service cannot CSRF an approval.
    - Records the configured operator identity as the journal
      ``responder`` instead of a hardcoded ``"dashboard"`` literal, so
      audit entries attribute *who* approved.

    The endpoint never writes to a store directly; it hands the response
    to the existing Phase 3 channel adapter, which the daemon's
    notification router already consumes — preserving the "every
    side-effect is signed and journaled" invariant.
    """
    data = _data(request)
    if data.notifications is None:
        return web.json_response({"error": "no dashboard adapter"}, status=503)

    auth = _auth(request)
    if auth is None:
        # Fail closed: a dashboard started without an explicit auth policy
        # cannot authorize approvals, period.
        log.warning("dashboard.approval_denied", reason="auth_not_configured")
        return web.json_response({"error": "auth not configured"}, status=503)

    token = _extract_bearer_token(request)
    if not auth.token_matches(token):
        log.warning("dashboard.approval_denied", reason="bad_token")
        return web.json_response({"error": "unauthorized"}, status=401)

    if not auth.origin_allowed(_request_origin(request)):
        log.warning("dashboard.approval_denied", reason="bad_origin")
        return web.json_response({"error": "forbidden origin"}, status=403)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)
    intent_id = body.get("intent_id")
    decision = body.get("decision")
    if not isinstance(intent_id, str) or decision not in ("approve", "deny", "snooze"):
        return web.json_response({"error": "invalid payload"}, status=400)
    snooze_seconds = body.get("snooze_seconds")
    if snooze_seconds is not None and not (isinstance(snooze_seconds, int) and snooze_seconds > 0):
        return web.json_response({"error": "invalid snooze_seconds"}, status=400)
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        return web.json_response({"error": "invalid note"}, status=400)
    response = ApprovalResponse(
        intent_id=intent_id,
        decision=decision,
        snooze_seconds=snooze_seconds,
        note=note,
        responder=auth.operator,
    )
    await data.notifications.submit_response(response)
    log.info(
        "dashboard.approval_submitted",
        intent_id=intent_id,
        decision=decision,
        responder=auth.operator.id,
    )
    return web.json_response({"ok": True})


__all__ = [
    "AUTH_KEY",
    "DATA_SOURCES_KEY",
    "actions_page",
    "events_page",
    "events_stream",
    "graph_json",
    "graph_page",
    "intents_page",
    "overview",
    "reasoning_page",
    "reflection_page",
    "security_headers_middleware",
    "submit_approval",
]
