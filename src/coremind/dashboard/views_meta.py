"""HTTP handlers for the meta-loop dashboard page (L8).

Provides ``/meta`` HTML page and ``/api/meta/*`` JSON endpoints.
All mutation endpoints (approve, deny, rollback) require authentication.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from aiohttp import web

from coremind.dashboard.data import DashboardDataSources, MetaSource
from coremind.dashboard.views import AUTH_KEY, DATA_SOURCES_KEY, _render

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

_META_BODY = """\
<div class="card">
  <h2>Meta-Loop Status</h2>
  <p><strong>Status:</strong> {{ "ENABLED" if status.enabled else "DISABLED" }}</p>
  {% if status.last_tick %}
  <p><strong>Last tick:</strong> {{ status.last_tick | localtime }}</p>
  {% endif %}
  <p><strong>Observations (window):</strong> {{ status.observations_count }}</p>
  <p><strong>Adjustments (window):</strong> {{ status.adjustments_count }}</p>
  <p><strong>Pending proposals:</strong> {{ status.pending_proposals_count }}</p>
</div>

{% if proposals %}
<div class="card">
  <h2>Pending Proposals</h2>
  <table>
    <thead><tr>
      <th>Policy</th><th>Parameter</th><th>Change</th><th>Actions</th>
    </tr></thead>
    <tbody>
    {% for p in proposals %}
    <tr>
      <td>{{ p.policy.name }}</td>
      <td>{{ p.parameter_path }}</td>
      <td>{{ p.old_value }} → {{ p.new_value }}</td>
      <td>
        <form method="post" action="/api/meta/proposals/{{ loop.index0 }}/approve" style="display:inline">
          <button type="submit">Approve</button>
        </form>
        <form method="post" action="/api/meta/proposals/{{ loop.index0 }}/deny" style="display:inline">
          <button type="submit">Deny</button>
        </form>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endif %}

<div class="card">
  <h2>Recent Observations</h2>
  {% if observations %}
  <table>
    <thead><tr>
      <th>Timestamp</th><th>Kind</th><th>Value</th><th>Threshold</th><th>Triggered</th>
    </tr></thead>
    <tbody>
    {% for obs in observations %}
    <tr>
      <td>{{ obs.observed_at | localtime }}</td>
      <td>{{ obs.kind }}</td>
      <td>{{ "%.2f" | format(obs.value) }}</td>
      <td>{{ "%.2f" | format(obs.threshold) }}</td>
      <td>{{ "YES" if obs.triggers_policy else "NO" }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p>No observations recorded.</p>
  {% endif %}
</div>

<div class="card">
  <h2>Adjustment History</h2>
  {% if adjustments %}
  <table>
    <thead><tr>
      <th>Timestamp</th><th>Policy</th><th>Parameter</th><th>Old → New</th><th>Actions</th>
    </tr></thead>
    <tbody>
    {% for adj in adjustments %}
    <tr>
      <td>{{ adj.applied_at | localtime }}</td>
      <td>{{ adj.policy_name }}</td>
      <td>{{ adj.parameter_path }}</td>
      <td>{{ adj.old_value }} → {{ adj.new_value }}{{ " (rolled back)" if adj.rollback_at else "" }}</td>
      <td>
        {% if not adj.rollback_at %}
        <form method="post" action="/api/meta/adjustments/{{ adj.adjustment_id }}/rollback" style="display:inline">
          <button type="submit">Rollback</button>
        </form>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p>No adjustments recorded.</p>
  {% endif %}
</div>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta_source(request: web.Request) -> MetaSource | None:
    """Extract the MetaSource from the request's app data."""
    sources: DashboardDataSources = request.app[DATA_SOURCES_KEY]
    return sources.meta


def _require_auth(request: web.Request) -> None:
    """Enforce authentication on mutation endpoints."""
    from coremind.dashboard.auth import DashboardAuth

    auth: DashboardAuth | None = request.app[AUTH_KEY]
    if auth is None:
        raise web.HTTPServiceUnavailable(reason="No auth configured")

    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not auth.token_matches(token):
        raise web.HTTPUnauthorized(reason="Invalid or missing token")

    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not auth.origin_allowed(origin):
        raise web.HTTPForbidden(reason="Origin not allowed")


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------


async def meta_page(request: web.Request) -> web.Response:
    """Render the /meta dashboard page."""

    source = _meta_source(request)
    if source is None:
        html = _render("/meta", "Meta-Loop", "<p>Meta-loop data source not configured.</p>")
        return web.Response(text=html, content_type="text/html")

    status = await source.get_status()
    observations = await source.list_observations(limit=50)
    adjustments = await source.list_adjustments(limit=50)
    proposals = await source.list_proposals()

    html = _render(
        "/meta",
        "Meta-Loop",
        _META_BODY,
        status=status,
        observations=observations,
        adjustments=adjustments,
        proposals=proposals,
    )
    return web.Response(text=html, content_type="text/html")


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


async def meta_status_json(request: web.Request) -> web.Response:
    """GET /api/meta/status → meta-loop status summary."""
    source = _meta_source(request)
    if source is None:
        raise web.HTTPServiceUnavailable(reason="Meta source not configured")

    status = await source.get_status()
    return web.json_response(status.model_dump(mode="json"))


async def meta_observations_json(request: web.Request) -> web.Response:
    """GET /api/meta/observations → list of observations."""
    source = _meta_source(request)
    if source is None:
        raise web.HTTPServiceUnavailable(reason="Meta source not configured")

    kind = request.query.get("kind")
    since_str = request.query.get("since")
    since = datetime.fromisoformat(since_str) if since_str else None
    limit = int(request.query.get("limit", "100"))

    observations = await source.list_observations(kind=kind, since=since, limit=limit)
    return web.json_response([o.model_dump(mode="json") for o in observations])


async def meta_adjustments_json(request: web.Request) -> web.Response:
    """GET /api/meta/adjustments → list of adjustments."""
    source = _meta_source(request)
    if source is None:
        raise web.HTTPServiceUnavailable(reason="Meta source not configured")

    since_str = request.query.get("since")
    since = datetime.fromisoformat(since_str) if since_str else None
    limit = int(request.query.get("limit", "100"))

    adjustments = await source.list_adjustments(since=since, limit=limit)
    return web.json_response([a.model_dump(mode="json") for a in adjustments])


async def meta_proposals_json(request: web.Request) -> web.Response:
    """GET /api/meta/proposals → list of pending proposals."""
    source = _meta_source(request)
    if source is None:
        raise web.HTTPServiceUnavailable(reason="Meta source not configured")

    proposals = await source.list_proposals()
    return web.json_response([p.model_dump(mode="json") for p in proposals])


async def meta_approve_json(request: web.Request) -> web.Response:
    """POST /api/meta/proposals/{id}/approve → approve a proposal."""
    _require_auth(request)
    source = _meta_source(request)
    if source is None:
        raise web.HTTPServiceUnavailable(reason="Meta source not configured")

    proposal_id = request.match_info["id"]
    await source.approve_proposal(proposal_id)
    return web.json_response({"status": "approved", "proposal_id": proposal_id})


async def meta_deny_json(request: web.Request) -> web.Response:
    """POST /api/meta/proposals/{id}/deny → deny a proposal."""
    _require_auth(request)
    source = _meta_source(request)
    if source is None:
        raise web.HTTPServiceUnavailable(reason="Meta source not configured")

    proposal_id = request.match_info["id"]
    await source.deny_proposal(proposal_id)
    return web.json_response({"status": "denied", "proposal_id": proposal_id})


async def meta_rollback_json(request: web.Request) -> web.Response:
    """POST /api/meta/adjustments/{id}/rollback → rollback an adjustment."""
    _require_auth(request)
    source = _meta_source(request)
    if source is None:
        raise web.HTTPServiceUnavailable(reason="Meta source not configured")

    adjustment_id = request.match_info["id"]
    await source.rollback_adjustment(adjustment_id)
    return web.json_response({"status": "rolled_back", "adjustment_id": adjustment_id})
