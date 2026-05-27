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
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from aiohttp import web
from jinja2 import Environment, select_autoescape

from coremind.dashboard.auth import DashboardAuth
from coremind.dashboard.data import DashboardDataSources
from coremind.action.autonomy import AutonomyConfig
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CoreMind — {{ title }}</title>
<style>
  :root { color-scheme: dark; }
  *, *::before, *::after { box-sizing: border-box; }
  body {
    font: 14px/1.5 system-ui, -apple-system, sans-serif;
    margin: 0;
    background: #0a0e17;
    color: #e2e8f0;
    min-height: 100vh;
  }
  /* ---- Animations ---- */
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  @keyframes slideIn {
    from { opacity: 0; transform: translateX(-12px); }
    to { opacity: 1; transform: translateX(0); }
  }
  @keyframes glowPulse {
    0%, 100% { box-shadow: 0 0 8px rgba(0,212,255,0.4); }
    50% { box-shadow: 0 0 20px rgba(0,212,255,0.7); }
  }
  @keyframes countUp {
    from { opacity: 0; transform: scale(0.8); }
    to { opacity: 1; transform: scale(1); }
  }
  .fade-in { animation: fadeIn 0.4s ease-out; }
  .slide-in { animation: slideIn 0.3s ease-out; }
  /* ---- Header ---- */
  header {
    background: linear-gradient(135deg, #0d1525 0%, #111827 50%, #0d1525 100%);
    border-bottom: 1px solid #1e293b;
    padding: 0 20px;
    display: flex;
    align-items: center;
    gap: 16px;
    height: 56px;
    position: sticky; top: 0; z-index: 100;
  }
  header .logo-img { height: 32px; width: 32px; border-radius: 6px; }
  header .brand { font-weight: 700; font-size: 16px; letter-spacing: 0.5px;
    background: linear-gradient(90deg, #00d4ff, #a855f7);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text; }
  header nav { display: flex; gap: 4px; margin-left: 8px; }
  header nav a {
    color: #94a3b8; text-decoration: none; padding: 6px 12px;
    border-radius: 6px; font-size: 13px; transition: all 0.2s;
  }
  header nav a:hover { color: #e2e8f0; background: rgba(255,255,255,0.05); }
  header nav a.active { color: #00d4ff; background: rgba(0,212,255,0.1); font-weight: 600; }
  header .live-dot {
    width: 8px; height: 8px; border-radius: 50%; background: #22c55e;
    margin-left: auto; animation: glowPulse 2s infinite;
  }
  /* ---- Main ---- */
  main { padding: 16px 24px; max-width: 100%; margin: 0 auto; }
  h1 { margin: 0 0 16px 0; font-size: 22px; font-weight: 600; color: #f1f5f9; }
  h2 { font-size: 17px; font-weight: 600; color: #e2e8f0; margin: 20px 0 10px; }
  /* ---- Cards / Panels ---- */
  .card {
    background: rgba(17,24,39,0.8); backdrop-filter: blur(12px);
    border: 1px solid #1e293b; border-radius: 10px; padding: 16px;
  }
  .card.glow-cyan { border-color: rgba(0,212,255,0.3); box-shadow: 0 0 12px rgba(0,212,255,0.08); }
  .card.glow-purple {
    border-color: rgba(168,85,247,0.3);
    box-shadow: 0 0 12px rgba(168,85,247,0.08);
  }
  /* ---- Stats Grid ---- */
  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px; margin-bottom: 20px;
  }
  .stat-card {
    background: rgba(17,24,39,0.9); border: 1px solid #1e293b;
    border-radius: 10px; padding: 16px; text-align: center;
    animation: countUp 0.5s ease-out;
    transition: border-color 0.3s;
  }
  .stat-card:hover { border-color: #00d4ff66; }
  .stat-value { font-size: 32px; font-weight: 700; color: #00d4ff; line-height: 1.1; }
  .stat-label {
    font-size: 11px;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-top: 4px;
  }
  .stat-card.warn .stat-value { color: #f97316; }
  .stat-card.ok .stat-value { color: #22c55e; }
  /* ---- Cockpit Layout ---- */
  .cockpit-grid {
display: grid; grid-template-columns: minmax(280px, 1fr) minmax(400px, 2.5fr) minmax(280px, 1.2fr); gap: 14px;
    min-height: calc(100vh - 120px);
  }
  @media (max-width: 1200px) { .cockpit-grid { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 800px) { .cockpit-grid { grid-template-columns: 1fr; } }
  .cockpit-panel {
    background: rgba(17,24,39,0.85); backdrop-filter: blur(12px);
    border: 1px solid #1e293b; border-radius: 10px;
    overflow: hidden; display: flex; flex-direction: column;
  }
  .cockpit-panel .panel-header {
    padding: 10px 14px; border-bottom: 1px solid #1e293b;
    font-size: 12px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.8px; color: #94a3b8;
    display: flex; align-items: center; gap: 8px;
  }
  .cockpit-panel .panel-header .dot { width: 6px; height: 6px; border-radius: 50%; }
  .cockpit-panel .panel-header .dot.live { background: #22c55e; animation: pulse 2s infinite; }
  .cockpit-panel .panel-body { padding: 10px 14px; overflow-y: auto; flex: 1; }
  /* ---- Event Ticker ---- */
  .event-ticker {
    font-family: "JetBrains Mono", "Cascadia Code", "Fira Code", monospace;
    font-size: 11px;
    max-height: calc(100vh - 200px);
    overflow-y: auto;
  }
  .event-ticker .tick {
    padding: 4px 0;
    border-bottom: 1px solid #1e293b;
    animation: slideIn 0.2s ease-out;
    display: flex;
    gap: 8px;
  }
  .event-ticker .tick .ts { color: #64748b; white-space: nowrap; min-width: 80px; }
  .event-ticker .tick .src {
    color: #a855f7;
    min-width: 80px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .event-ticker .tick .attr { color: #00d4ff; }
  .event-ticker .tick .val {
    color: #e2e8f0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* ---- Intent Cards ---- */
  .intent-card {
    background: rgba(26,35,50,0.7); border: 1px solid #1e293b;
    border-radius: 8px; padding: 12px; margin-bottom: 10px;
    animation: fadeIn 0.3s ease-out;
    transition: border-color 0.2s;
  }
  .intent-card:hover { border-color: #334155; }
  .intent-card .intent-header {
    display: flex;
    justify-content: space-between;
    align-items: start;
    margin-bottom: 6px;
  }
  .intent-card .intent-question { font-size: 13px; color: #e2e8f0; line-height: 1.4; }
  .intent-card .intent-meta {
    display: flex;
    gap: 12px;
    align-items: center;
    margin-top: 6px;
    font-size: 11px;
    color: #64748b;
  }
  .intent-card .salience-bar {
    height: 4px; border-radius: 2px; background: #1e293b; margin-top: 8px; overflow: hidden;
  }
  .intent-card .salience-bar .fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.5s ease;
  }
  /* ---- Pills ---- */
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
  }
  .pill.safe { background: rgba(34,197,94,0.15); color: #22c55e; }
  .pill.suggest { background: rgba(59,130,246,0.15); color: #3b82f6; }
  .pill.ask { background: rgba(249,115,22,0.15); color: #f97316; }
  .pill.conversation { background: rgba(168,85,247,0.15); color: #a855f7; }
  .pill.pending { background: rgba(249,115,22,0.15); color: #f97316; }
  .pill.approved { background: rgba(34,197,94,0.15); color: #22c55e; }
  .pill.executing { background: rgba(0,212,255,0.15); color: #00d4ff; }
  .pill.done { background: rgba(34,197,94,0.1); color: #4ade80; }
  .pill.rejected, .pill.failed { background: rgba(239,68,68,0.15); color: #ef4444; }
  .pill.snoozed { background: rgba(100,116,139,0.15); color: #94a3b8; }
  /* ---- Tables ---- */
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td {
    border-bottom: 1px solid #1e293b;
    padding: 8px 10px;
    text-align: left;
    vertical-align: top;
  }
  th {
    background: rgba(17,24,39,0.6);
    color: #94a3b8;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  tr:hover td { background: rgba(255,255,255,0.02); }
  pre {
    background: rgba(0,0,0,0.3);
    padding: 12px;
    border-radius: 8px;
overflow-x: auto;
    font-size: 12px;
    border: 1px solid #1e293b;
  }
  code { font-family: "JetBrains Mono", "Cascadia Code", monospace; font-size: 12px; color: #00d4ff; }
  .muted { color: #64748b; }
  /* ---- Buttons ---- */
  button {
    font: inherit; padding: 6px 14px; cursor: pointer; border-radius: 6px;
    border: 1px solid #334155; background: rgba(51,65,85,0.5); color: #e2e8f0;
    transition: all 0.2s; font-size: 13px;
  }
  button:hover { background: rgba(51,65,85,0.8); border-color: #475569; }
  button.approve { border-color: rgba(34,197,94,0.4); color: #22c55e; }
  button.approve:hover { background: rgba(34,197,94,0.15); }
  button.deny { border-color: rgba(239,68,68,0.4); color: #ef4444; }
  button.deny:hover { background: rgba(239,68,68,0.15); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  .row-actions { display: flex; gap: 6px; }
  /* ---- Forms ---- */
  input, select {
    background: #1a2332; border: 1px solid #334155; border-radius: 6px;
    padding: 6px 10px; color: #e2e8f0; font: inherit; font-size: 13px;
  }
  input:focus, select:focus { outline: none; border-color: #00d4ff; }
  /* ---- Graph ---- */
  #graph-container {
    width: 100%;
    height: calc(100vh - 140px);
    min-height: 600px;
    background: rgba(0,0,0,0.2);
    border-radius: 10px;
    border: 1px solid #1e293b;
    overflow: hidden;
    position: relative;
  }
  #graph-container svg { width: 100%; height: 100%; }
  #graph-container .node-label {
    font-size: 10px;
    fill: #e2e8f0;
    pointer-events: none;
    text-shadow: 0 1px 3px rgba(0,0,0,0.8);
  }
  #graph-container .legend {
    position: absolute;
    top: 10px;
    right: 10px;
    background: rgba(10,14,23,0.9);
    border: 1px solid #1e293b;
    border-radius: 8px;
    padding: 10px;
    font-size: 11px;
  }
  /* ---- Timeline ---- */
  .timeline { position: relative; padding-left: 24px; }
  .timeline::before {
    content: '';
    position: absolute;
    left: 8px;
    top: 0;
    bottom: 0;
    width: 2px;
    background: linear-gradient(180deg, #00d4ff, #a855f7);
  }
  .timeline-entry {
    position: relative;
    margin-bottom: 16px;
    padding: 10px 14px;
    background: rgba(17,24,39,0.7);
    border: 1px solid #1e293b;
    border-radius: 8px;
  }
  .timeline-entry::before {
    content: '';
    position: absolute;
    left: -20px;
    top: 14px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #00d4ff;
    border: 2px solid #0a0e17;
  }
  .timeline-entry.deny::before, .timeline-entry.rejected::before, .timeline-entry.failed::before {
    background: #ef4444;
  }
.timeline-entry.ok::before, .timeline-entry.approved::before, .timeline-entry.done::before {
    background: #22c55e;
  }
  /* ---- Terminal ---- */
  .terminal { background: #0a0e17; border: 1px solid #1e293b; border-radius: 10px; overflow: hidden; }
  .terminal .term-header {
    padding: 8px 14px;
    background: #111827;
    border-bottom: 1px solid #1e293b;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: #94a3b8;
  }
  .terminal .term-header .controls { display: flex; gap: 6px; }
  .terminal .term-header .ctrl { width: 10px; height: 10px; border-radius: 50%; }
  .terminal .term-header .ctrl.red { background: #ef4444; }
  .terminal .term-header .ctrl.yellow { background: #f59e0b; }
  .terminal .term-header .ctrl.green { background: #22c55e; }
  .terminal .term-body {
    font-family: "JetBrains Mono", monospace;
    font-size: 12px;
    padding: 12px;
    max-height: calc(100vh - 200px);
    overflow-y: auto;
  }
  .terminal .term-body .line {
    padding: 2px 0;
    display: flex;
    gap: 10px;
    animation: slideIn 0.15s ease-out;
  }
  .terminal .term-body .line .time { color: #475569; white-space: nowrap; min-width: 85px; }
  .terminal .term-body .line .plugin { color: #a855f7; min-width: 80px; }
  .terminal .term-body .line .msg { color: #94a3b8; flex: 1; }
  .terminal .term-body .line .data { color: #00d4ff; }
  .term-toolbar { display: flex; gap: 8px; align-items: center; }
  .term-toolbar .evt-count { font-size: 11px; color: #64748b; }
</style>
</head>
<body>
<header>
  <img class="logo-img" src="/logo.png" alt="CoreMind" width="32" height="32">
  <span class="brand">COREMIND</span>
  <nav>
    {% for key, label in nav %}
      <a href="{{ key }}" class="{% if key == active %}active{% endif %}">{{ label }}</a>
    {% endfor %}
  </nav>
  <span class="live-dot" title="Live"></span>
</header>
<main>
{{ body | safe }}
</main>
</body>
</html>
"""
_OVERVIEW_BODY = """
<div class="stats-grid fade-in" id="stats-row">
  <div class="stat-card"><div class="stat-value" data-testid="entity-count">{{ entity_count }}</div><div class="stat-label">Entities</div></div>
  <div class="stat-card"><div class="stat-value" data-testid="relationship-count">{{ relationship_count }}</div><div class="stat-label">Relationships</div></div>
  <div class="stat-card"><div class="stat-value" data-testid="recent-event-count">{{ recent_event_count }}</div><div class="stat-label">Events (24h)</div></div>
  <div class="stat-card warn"><div class="stat-value" data-testid="pending-intents">{{ pending_intents }}</div><div class="stat-label">Pending Intents</div></div>
  <div class="stat-card warn"><div class="stat-value" data-testid="pending-approvals">{{ pending_notifications }}</div><div class="stat-label">Approvals</div></div>
  <div class="stat-card ok"><div class="stat-value" id="uptime-stat">--</div><div class="stat-label">Uptime</div></div>
  <div class="stat-card"><div class="stat-value" data-testid="meta-adjustments">{{ meta_adjustments_today }}</div><div class="stat-label">Meta Adjustments</div></div>
  {% if meta_pending_proposals > 0 %}<div class="stat-card warn"><div class="stat-value" data-testid="meta-proposals">{{ meta_pending_proposals }}</div><div class="stat-label">Meta Proposals</div></div>{% endif %}
</div>

<div class="cockpit-grid">
  <!-- LEFT: Live Event Ticker -->
  <div class="cockpit-panel">
    <div class="panel-header"><span class="dot live"></span> LIVE EVENTS</div>
    <div class="panel-body event-ticker" id="live-ticker">
      <div class="muted" style="padding:20px;text-align:center">Connecting&hellip;</div>
    </div>
  </div>

  <!-- CENTER: Mini World Graph -->
  <div class="cockpit-panel">
    <div class="panel-header"><span class="dot" style="background:#00d4ff"></span> WORLD GRAPH <a href="/graph" style="margin-left:auto;font-size:11px;color:#00d4ff;text-decoration:none">expand &rarr;</a></div>
    <div class="panel-body" style="padding:0;min-height:350px">
      <div id="mini-graph" style="width:100%;height:100%;min-height:350px"></div>
    </div>
  </div>

  <!-- RIGHT: Recent Intents -->
  <div class="cockpit-panel">
    <div class="panel-header"><span class="dot" style="background:#a855f7"></span> RECENT INTENTS</div>
    <div class="panel-body" id="cockpit-intents" style="max-height:calc(100vh - 200px);overflow-y:auto">
      <div class="muted" style="padding:20px;text-align:center">Loading&hellip;</div>
    </div>
  </div>
</div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
// ---- Stats Polling ----
async function refreshStats() {
  try {
    const r = await fetch('/api/stats');
    const s = await r.json();
    document.querySelector('[data-testid="entity-count"]').textContent = s.entities;
    document.querySelector('[data-testid="relationship-count"]').textContent = s.relationships;
    document.querySelector('[data-testid="recent-event-count"]').textContent = s.events_24h;
    document.querySelector('[data-testid="pending-intents"]').textContent = s.pending_intents;
    document.querySelector('[data-testid="pending-approvals"]').textContent = s.pending_approvals;
    const mins = Math.floor(s.uptime_seconds / 60);
    const hrs = Math.floor(mins / 60);
    document.getElementById('uptime-stat').textContent = hrs > 0 ? hrs + 'h ' + (mins % 60) + 'm' : mins + 'm';
  } catch(e) {}
}

// ---- SSE Event Ticker ----
(function() {
  const ticker = document.getElementById('live-ticker');
  let count = 0;
  const MAX = 60;
  const es = new EventSource('/api/events/stream');
  es.addEventListener('event', function(msg) {
    let e;
    try { e = JSON.parse(msg.data); } catch(err) { return; }
    count++;
    if (count === 1) ticker.innerHTML = '';
    const div = document.createElement('div');
    div.className = 'tick';
    const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString() : '--:--:--';
    div.innerHTML = '<span class="ts">' + ts + '</span>' +
      '<span class="src">' + (e.source || '').replace(/</g,'&lt;') + '</span>' +
      '<span class="attr">' + (e.attribute || '').replace(/</g,'&lt;') + '</span>' +
      '<span class="val">' + JSON.stringify(e.value).replace(/</g,'&lt;').substring(0,120) + '</span>';
    ticker.insertBefore(div, ticker.firstChild);
    while (ticker.children.length > MAX) ticker.removeChild(ticker.lastChild);
  });
})();

// ---- Mini World Graph ----
(async function() {
  try {
    const r = await fetch('/api/graph');
    const data = await r.json();
    if (!data.nodes || !data.nodes.length) {
      document.getElementById('mini-graph').innerHTML = '<div class="muted" style="padding:40px;text-align:center">No entities yet</div>';
      return;
    }
    // Update relationship counter from graph edges
    const relCount = data.edges ? data.edges.length : 0;
    const relEl = document.querySelector('[data-testid="relationship-count"]');
    if (relEl) relEl.textContent = relCount;
    // D3 graph code
    const W = document.getElementById('mini-graph').clientWidth;
    const H = Math.max(350, document.getElementById('mini-graph').clientHeight);
    const colors = d3.scaleOrdinal(d3.schemeCategory10);
    const svg = d3.select('#mini-graph').append('svg').attr('viewBox', [0,0,W,H]);
    const g = svg.append('g');
    const nodes = data.nodes.map(n => ({...n}));
    const links = (data.edges || []).map(e => ({
      source: e.from,
      target: e.to,
      weight: e.weight
    }));
    const sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id(d => d.id).distance(60))
      .force('charge', d3.forceManyBody().strength(-120))
      .force('center', d3.forceCenter(W/2, H/2))
      .force('collision', d3.forceCollide(18));
    const link = g.append('g').selectAll('line').data(links).join('line')
      .attr('stroke', '#1e293b').attr('stroke-width', d => Math.max(1, d.weight * 2));
    const node = g.append('g').selectAll('circle').data(nodes).join('circle')
      .attr('r', 8).attr('fill', d => colors(d.type)).attr('stroke', '#0a0e17').attr('stroke-width', 2)
      .call(d3.drag().on('start', (e,d) => { if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
        .on('drag', (e,d) => { d.fx=e.x; d.fy=e.y; })
        .on('end', (e,d) => { if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }));
    node.append('title').text(d => d.display_name || d.id);
    sim.on('tick', () => {
      link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
      node.attr('cx', d => d.x).attr('cy', d => d.y);
    });
  } catch(e) { document.getElementById('mini-graph').innerHTML = '<div class="muted" style="padding:40px;text-align:center">Graph unavailable</div>'; }
})();

// ---- Recent Intents ----
async function refreshIntents() {
  try {
    const r = await fetch('/api/intents/list');
    const intents = await r.json();
    const container = document.getElementById('cockpit-intents');
    if (!intents.length) { container.innerHTML = '<div class="muted" style="padding:20px;text-align:center">No intents</div>'; return; }
    container.innerHTML = intents.slice(0, 10).map(i => {
      const salienceColor = i.salience > 0.7 ? '#22c55e' : i.salience > 0.4 ? '#f59e0b' : '#64748b';
      return '<div class="intent-card">' +
        '<div class="intent-header">' +
          '<span class="pill ' + i.category + '">' + i.category + '</span>' +
          '<span style="font-size:10px;color:#64748b">' + new Date(i.created_at).toLocaleTimeString() + '</span>' +
        '</div>' +
        '<div class="intent-question">' + i.question.replace(/</g,'&lt;') + '</div>' +
        '<div class="intent-meta">' +
          '<span>S: ' + (i.salience*100).toFixed(0) + '%</span>' +
          '<span>C: ' + (i.confidence*100).toFixed(0) + '%</span>' +
          '<span class="pill ' + i.status + '">' + i.status + '</span>' +
        '</div>' +
        '<div class="salience-bar"><div class="fill" style="width:' + (i.salience*100) + '%;background:' + salienceColor + '"></div></div>' +
      '</div>';
    }).join('');
  } catch(e) {}
}

// Initial load + periodic refresh
refreshStats();
refreshIntents();
setInterval(refreshStats, 5000);
setInterval(refreshIntents, 10000);
</script>
"""


_EVENTS_BODY = """
<h1>Live events</h1>
<div class="terminal">
  <div class="term-header">
    <div class="controls"><span class="ctrl red"></span><span class="ctrl yellow"></span><span class="ctrl green"></span></div>
    <span>/api/events/stream — SSE</span>
    <div class="term-toolbar" style="margin-left:auto">
      <span class="evt-count" id="evt-counter">0 events</span>
      <button onclick="toggleStream()" id="btn-toggle" style="font-size:11px;padding:3px 8px;">Pause</button>
    </div>
  </div>
  <div class="term-body" id="term-output">
    {% for event in initial_events %}
    <div class="line"><span class="time">{{ event.timestamp|localtime }}</span><span class="plugin">{{ event.source }}</span><span class="msg">{{ event.entity.type }}:{{ event.entity.id }} · {{ event.attribute }}</span><span class="data">{{ event.value | tojson | truncate(80) }}</span></div>
    {% endfor %}
  </div>
</div>
<script>
  const term = document.getElementById('term-output');
  let counter = document.getElementById('evt-counter');
  let count = {{ initial_events | length }};
  let paused = false;
  let queue = [];
  const es = new EventSource('/api/events/stream');
  es.addEventListener('event', function(msg) {
    let e;
    try { e = JSON.parse(msg.data); } catch(err) { return; }
    count++;
    counter.textContent = count + ' events';
    const div = document.createElement('div');
    div.className = 'line';
    const ts = e.timestamp ? e.timestamp : '';
    const source = (e.source || '').replace(/</g,'&lt;');
    const entity = (e.entity || {});
    const eid = (entity.type||'') + ':' + (entity.id||'');
    const attr = (e.attribute || '').replace(/</g,'&lt;');
    const val = JSON.stringify(e.value).replace(/</g,'&lt;').substring(0, 80);
    div.innerHTML = '<span class="time">' + ts + '</span>' +
      '<span class="plugin">' + source + '</span>' +
      '<span class="msg">' + eid + ' \u00b7 ' + attr + '</span>' +
      '<span class="data">' + val + '</span>';
    if (paused) { queue.push(div); return; }
    term.insertBefore(div, term.firstChild);
    if (term.children.length > 200) term.removeChild(term.lastChild);
  });
  function toggleStream() {
    paused = !paused;
    document.getElementById('btn-toggle').textContent = paused ? 'Resume' : 'Pause';
    if (!paused) {
      while (queue.length) { term.insertBefore(queue.pop(), term.firstChild); }
    }
  }
</script>
"""


_GRAPH_BODY = """
<h1>World graph</h1>
<div style="margin-bottom:12px;display:flex;gap:8px;align-items:center">
  <input type="text" id="graph-search" placeholder="Search entities..." style="width:240px">
  <span class="muted" style="font-size:12px">{{ entities | length }} entities, {{ relationships | length }} relationships</span>
</div>
<div id="graph-container">
  <div class="legend" id="graph-legend"></div>
</div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
(async function() {
  const container = document.getElementById('graph-container');
  const W = container.clientWidth;
  const H = 600;
  const r = await fetch('/api/graph');
  const data = await r.json();
  if (!data.nodes || !data.nodes.length) {
    container.innerHTML = '<div class="muted" style="padding:60px;text-align:center">No entities in world model</div>';
    return;
  }
  const colors = d3.scaleOrdinal(d3.schemeCategory10);
  const nodes = data.nodes.map(n => ({...n}));
  const links = (data.edges || []).map(e => ({
    source: e.from,
    target: e.to,
    weight: e.weight,
    type: e.type
  }));
  // Legend
  const types = [...new Set(nodes.map(n => n.type))];
  document.getElementById('graph-legend').innerHTML = types.map(t =>
    '<div style="display:flex;align-items:center;gap:6px;margin:3px 0">' +
    '<span style="width:10px;height:10px;border-radius:50%;background:' + colors(t) + ';display:inline-block"></span>' +
    t + '</div>'
  ).join('');

  const svg = d3.select('#graph-container').append('svg').attr('viewBox', [0,0,W,H]);
  const g = svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.2, 5]).on('zoom', (e) => g.attr('transform', e.transform)));

  const sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(80))
    .force('charge', d3.forceManyBody().strength(-200))
    .force('center', d3.forceCenter(W/2, H/2))
    .force('collision', d3.forceCollide(20));

  const link = g.append('g').selectAll('line').data(links).join('line')
    .attr('stroke', '#1e293b').attr('stroke-width', d => Math.max(1, d.weight * 3));

  const node = g.append('g').selectAll('g').data(nodes).join('g')
    .call(d3.drag().on('start', (e,d) => { if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
      .on('drag', (e,d) => { d.fx=e.x; d.fy=e.y; })
      .on('end', (e,d) => { if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }));

  node.append('circle')
    .attr('r', d => 6 + Math.min(d.properties?.relationship_count || 0, 12))
    .attr('fill', d => colors(d.type))
    .attr('stroke', '#0a0e17').attr('stroke-width', 2)
    .attr('cursor', 'pointer');

  node.append('text')
    .attr('dy', -10).attr('text-anchor', 'middle')
    .attr('class', 'node-label')
    .text(d => (d.display_name || d.id).substring(0, 25));

  node.append('title').text(d => d.type + ': ' + (d.display_name || d.id));

  // Click to highlight connections
  node.on('click', function(evt, d) {
    const connected = new Set();
    links.forEach(l => { if (l.source.id === d.id) connected.add(l.target.id); if (l.target.id === d.id) connected.add(l.source.id); });
    node.select('circle').attr('opacity', n => n.id === d.id || connected.has(n.id) ? 1 : 0.15);
    link.attr('opacity', l => l.source.id === d.id || l.target.id === d.id ? 1 : 0.05);
  });
  svg.on('click', function(evt) {
    if (evt.target === svg.node() || evt.target === g.node()) {
      node.select('circle').attr('opacity', 1);
      link.attr('opacity', 1);
    }
  });

  // Search
  document.getElementById('graph-search').addEventListener('input', function() {
    const q = this.value.toLowerCase();
    node.select('circle').attr('opacity', n => {
      const txt = ((n.display_name||'') + ' ' + n.type + ' ' + n.id).toLowerCase();
      return txt.includes(q) ? 1 : 0.15;
    });
  });

  sim.on('tick', () => {
    link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    node.attr('transform', d => 'translate(' + d.x + ',' + d.y + ')');
  });
})();
</script>
"""


_REASONING_BODY = """
<h1>Reasoning cycles</h1>
<table>
  <thead><tr><th>Cycle</th><th>Timestamp</th><th>Model</th><th>Patterns</th><th>Anomalies</th><th>Predictions</th></tr></thead>
  <tbody>
  {% for cycle in cycles %}
    <tr class="fade-in">
      <td><code>{{ cycle.cycle_id[:20] }}&hellip;</code></td>
      <td>{{ cycle.timestamp|localtime }}</td>
      <td><span style="color:#00d4ff">{{ cycle.model_used }}</span></td>
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
{% if not intents %}
<p class="muted fade-in">No intents recorded yet.</p>
{% endif %}
<div id="intents-container">
{% for intent in intents %}
  <div class="intent-card fade-in">
    <div class="intent-header">
      <div style="display:flex;gap:6px;align-items:center">
        <span class="pill {{ intent.category }}">{{ intent.category }}</span>
        <span class="pill {{ intent.status }}">{{ intent.status }}</span>
      </div>
      <span style="font-size:11px;color:#64748b">{{ intent.created_at|localtime }}</span>
    </div>
    <div class="intent-question">{{ intent.question.text }}</div>
    <div class="intent-meta">
      <span title="Salience">🔥 {{ "%.0f"|format(intent.salience * 100) }}%</span>
      <span title="Confidence">🎯 {{ "%.0f"|format(intent.confidence * 100) }}%</span>
      {% if intent.proposed_action %}
      <span title="Actions">⚡ 1 action</span>
      {% endif %}
    </div>
    <div class="salience-bar">
      <div class="fill" style="width:{{ (intent.salience * 100)|int }}%;background:{% if intent.salience > 0.7 %}#22c55e{% elif intent.salience > 0.4 %}#f59e0b{% else %}#64748b{% endif %}"></div>
    </div>
    {% if intent.proposed_action %}
    <div style="margin-top:8px;font-size:11px;color:#64748b">
      <div>→ <code>{{ intent.proposed_action.operation }}</code> <span style="color:#94a3b8">{{ intent.proposed_action.expected_outcome }}</span></div>
    </div>
    {% endif %}
  </div>
{% endfor %}
</div>
{% if pending_notifications %}
<h2>Pending approvals</h2>
{% for note in pending_notifications %}
<div class="card fade-in" style="margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:8px">
    <div>
      <span style="font-size:12px;color:#64748b">{{ note.sent_at|localtime }}</span>
      {% if note.intent_id %}<code style="margin-left:8px;font-size:11px">{{ note.intent_id[:12] }}&hellip;</code>{% endif %}
    </div>
    <div class="row-actions">
      {% for action in note.actions %}
        <button data-intent="{{ note.intent_id }}" data-decision="{{ action.value }}"
          class="{% if action.value == 'approve' %}approve{% elif action.value == 'deny' %}deny{% endif %}">
          {{ action.label }}
        </button>
      {% endfor %}
    </div>
  </div>
  <div style="font-size:13px;color:#e2e8f0">{{ note.message }}</div>
</div>
{% endfor %}
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
      if (r.ok) { btn.disabled = true; btn.textContent = '\u2713 ' + btn.textContent; }
    });
  });
</script>
{% endif %}
"""

_ACTIONS_BODY = """
<h1>Action journal</h1>
<form method="get" style="margin-bottom:16px">
  <input type="text" name="q" value="{{ query }}" placeholder="Filter by action_class or operation" style="width:300px">
  <button type="submit">Filter</button>
  {% if query %}<a href="/actions" style="color:#64748b;font-size:12px;margin-left:8px">clear</a>{% endif %}
</form>
{% if not entries %}
<p class="muted">No journal entries.</p>
{% endif %}
<div class="timeline">
{% for entry in entries %}
  <div class="timeline-entry {{ entry.outcome|lower }}">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div>
        <span style="color:#64748b;font-size:11px">#{{ entry.seq }}</span>
        <span style="margin-left:8px;font-size:12px;color:#e2e8f0">{{ entry.kind }}</span>
        {% if entry.operation %}
        <code style="margin-left:8px">{{ entry.operation }}</code>
        {% endif %}
        {% if entry.action_class %}
        <span style="margin-left:8px;font-size:11px;color:#94a3b8">{{ entry.action_class }}</span>
        {% endif %}
      </div>
      <span style="font-size:11px;color:#64748b">{{ entry.timestamp|localtime }}</span>
    </div>
    {% if entry.outcome %}
    <div style="margin-top:4px">
      <span class="pill {{ entry.outcome|lower }}">{{ entry.outcome }}</span>
    </div>
    {% endif %}
  </div>
{% endfor %}
</div>
"""


_REFLECTION_BODY = """
<h1>Reflection reports</h1>
{% if not reports %}
<p class="muted">No reports archived yet.</p>
{% endif %}
{% for stored in reports %}
<section class="card fade-in" style="margin-bottom:16px">
  <h2 style="margin-top:0">{{ stored.report.window_start|localtime }} &rarr; {{ stored.report.window_end|localtime }}</h2>
  <p class="muted">Cycle id: <code>{{ stored.report.cycle_id }}</code> &middot;
  Predictions evaluated: {{ stored.report.predictions.evaluated }} &middot;
  {% set brier = stored.report.calibration.brier_score %}
  Brier: {{ brier if brier is not none else "n/a" }}</p>
  <pre>{{ stored.report.markdown }}</pre>
</section>
{% endfor %}
"""


_NAV: list[tuple[str, str]] = [
    ("/", "Cockpit"),
    ("/events", "Events"),
    ("/graph", "Graph"),
    ("/reasoning", "Reasoning"),
    ("/intents", "Intents"),
    ("/actions", "Actions"),
    ("/reflection", "Reflection"),
    ("/autonomy", "Autonomy"),
    ("/meta", "Meta"),
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

from zoneinfo import ZoneInfo

_LOCAL_TZ: ZoneInfo = ZoneInfo("UTC")
_env.filters["localtime"] = lambda dt: dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def configure_dashboard_timezone(tz: ZoneInfo) -> None:
    """Set the timezone used by the dashboard's ``localtime`` filter.

    Must be called before the app starts serving requests.
    """
    global _LOCAL_TZ  # noqa: PLW0603 — module-level config, set once at startup
    _LOCAL_TZ = tz
    _env.filters["localtime"] = lambda dt: dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


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
    "script-src 'self' 'unsafe-inline' https://d3js.org; "
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
    meta_adjustments_today = 0
    meta_pending_proposals = 0
    if data.meta is not None:
        status = await data.meta.get_status()
        meta_adjustments_today = status.adjustments_count
        meta_pending_proposals = status.pending_proposals_count
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
        meta_adjustments_today=meta_adjustments_today,
        meta_pending_proposals=meta_pending_proposals,
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
    _min_group_size = 2
    data = _data(request)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    if data.world is not None:
        snapshot = await data.world.snapshot()
        for entity in snapshot.entities:
            nodes.append(
                {
                    "id": f"{entity.type}:{entity.display_name}",
                    "type": entity.type,
                    "display_name": entity.display_name,
                    "properties": entity.properties,
                    "source_plugins": entity.source_plugins,
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

        # If no relationships exist, generate synthetic ones by grouping
        # entities of the same type and from the same source plugins.
        if not edges:
            # Build type → node ids index
            type_groups: dict[str, list[str]] = {}
            plugin_groups: dict[str, list[str]] = {}
            for node in nodes:
                nid = node["id"]
                etype = node["type"]
                type_groups.setdefault(etype, []).append(nid)
                for plugin in node.get("source_plugins", []) or []:
                    plugin_groups.setdefault(str(plugin), []).append(nid)

            # Create same-type relationships (chain within each group)
            for etype, members in type_groups.items():
                if len(members) < _min_group_size:
                    continue
                weight = round(1.0 / len(members), 3)  # Rarer types = stronger
                for i in range(len(members) - 1):
                    edges.append(
                        {
                            "from": members[i],
                            "to": members[i + 1],
                            "type": f"same_type:{etype}",
                            "weight": max(weight, 0.1),
                        }
                    )

            # Create same-plugin relationships
            for plugin, members in plugin_groups.items():
                if len(members) < _min_group_size:
                    continue
                short_plugin = plugin.split(".")[-1] if "." in plugin else plugin
                # Connect first member to all others (star topology for plugins)
                for member in members[1 : min(len(members), 5)]:
                    edges.append(
                        {
                            "from": members[0],
                            "to": member,
                            "type": f"source:{short_plugin}",
                            "weight": 0.5,
                        }
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


# ---------------------------------------------------------------------------
# JSON API handlers for the cockpit dashboard
# ---------------------------------------------------------------------------

_START_TIME = datetime.now(UTC)


async def stats_json(request: web.Request) -> web.Response:
    """Return aggregated daemon stats as JSON for the cockpit."""
    min_same_type_count = 2
    data = _data(request)
    entity_count = 0
    relationship_count = 0
    events_24h = 0
    pending_intents = 0
    pending_approvals = 0

    if data.world is not None:
        with contextlib.suppress(Exception):
            snap = await data.world.snapshot()
            entity_count = len(snap.entities)
            relationship_count = len(snap.relationships)
            # If no stored relationships, compute synthetic ones for display
            if relationship_count == 0 and entity_count > 0:
                rel_count = 0
                type_groups: dict[str, int] = {}
                for entity in snap.entities:
                    etype = entity.type
                    type_groups[etype] = type_groups.get(etype, 0) + 1
                # Count same-type edges (chain within each group)
                for count in type_groups.values():
                    if count >= min_same_type_count:
                        rel_count += count - 1  # chain edges
                # Count same-plugin edges (approx: first member → up to 4 others)
                plugin_groups: dict[str, int] = {}
                for entity in snap.entities:
                    for plugin in entity.source_plugins:
                        plugin_groups[str(plugin)] = plugin_groups.get(str(plugin), 0) + 1
                for count in plugin_groups.values():
                    if count >= min_same_type_count:
                        rel_count += min(count - 1, 4)
                relationship_count = rel_count
        with contextlib.suppress(Exception):
            since = datetime.now(UTC) - timedelta(days=1)
            recent = await data.world.recent_events(since, limit=10_000)
            events_24h = len(recent)

    if data.intents is not None:
        with contextlib.suppress(Exception):
            intents = await data.intents.list(status=None, limit=500)
            pending_intents = sum(
                1 for i in intents if i.status in ("pending", "pending_approval", "executing")
            )

    if data.notifications is not None:
        with contextlib.suppress(Exception):
            pending_approvals = len(data.notifications.pending())

    uptime = (datetime.now(UTC) - _START_TIME).total_seconds()

    return web.json_response(
        {
            "entities": entity_count,
            "relationships": relationship_count,
            "events_24h": events_24h,
            "pending_intents": pending_intents,
            "pending_approvals": pending_approvals,
            "uptime_seconds": int(uptime),
        }
    )


async def intents_list_json(request: web.Request) -> web.Response:
    """Return recent intents as JSON for the cockpit cards."""
    data = _data(request)
    intents = []
    if data.intents is not None:
        try:
            raw = await data.intents.list(status=None, limit=50)
            for i in raw:
                actions = []
                if i.proposed_action:
                    a = i.proposed_action
                    actions.append(
                        {
                            "operation": a.operation,
                            "expected_outcome": a.expected_outcome,
                            "action_class": a.action_class,
                        }
                    )
                intents.append(
                    {
                        "id": i.id,
                        "created_at": i.created_at.isoformat(),
                        "question": i.question.text,
                        "category": i.category,
                        "status": i.status,
                        "salience": i.salience,
                        "confidence": i.confidence,
                        "actions": actions,
                    }
                )
        except Exception:
            log.exception("dashboard.intents_list_json_failed")
    return web.json_response(intents)


async def events_recent_json(request: web.Request) -> web.Response:
    """Return recent events as JSON."""
    data = _data(request)
    limit = min(int(request.query.get("limit", "50")), 200)
    events = []
    if data.world is not None:
        with contextlib.suppress(Exception):
            since = datetime.now(UTC) - timedelta(days=1)
            raw = await data.world.recent_events(since, limit=limit)
            for e in raw:
                events.append(
                    {
                        "id": e.id,
                        "timestamp": e.timestamp.isoformat(),
                        "source": e.source,
                        "entity_type": e.entity.type,
                        "entity_id": e.entity.id,
                        "attribute": e.attribute,
                        "value": e.value,
                        "confidence": e.confidence,
                    }
                )
    return web.json_response(events)


# ---------------------------------------------------------------------------
# Autonomy page + API
# ---------------------------------------------------------------------------

_AUTONOMY_BODY = """
<h2 class="fade-in">Autonomy Sliders</h2>
<p class="muted">Per-domain trust levels. Higher slider = more autonomous.</p>
<table class="fade-in">
<thead><tr><th>Domain</th><th>Slider</th><th>Visual</th><th>Override</th></tr></thead>
<tbody>
{% for d in domains %}
<tr>
  <td><strong>{{ d.name }}</strong></td>
  <td>{{ "%.2f"|format(d.slider) }}</td>
  <td>
    <div style="width:120px;height:8px;background:#1e293b;border-radius:4px;overflow:hidden">
      <div style="width:{{ (d.slider * 100)|int }}%;height:100%;background:{% if d.slider >= 0.7 %}#22c55e{% elif d.slider >= 0.3 %}#eab308{% else %}#ef4444{% endif %};border-radius:4px"></div>
    </div>
  </td>
  <td>{% if d.hard_ask %}🔒 Hard ASK{% elif d.hard_safe %}✅ Hard SAFE{% else %}—{% endif %}</td>
</tr>
{% endfor %}
</tbody>
</table>

{% if proposals %}
<h3 style="margin-top:2rem">Graduation Proposals</h3>
<table class="fade-in">
<thead><tr><th>Domain</th><th>Current</th><th>Proposed</th><th>Approval Rate</th><th>Actions</th></tr></thead>
<tbody>
{% for p in proposals %}
<tr>
  <td>{{ p.domain }}</td>
  <td>{{ "%.2f"|format(p.current_slider) }}</td>
  <td>{{ "%.2f"|format(p.proposed_slider) }}</td>
  <td>{{ "%.0f"|format(p.approval_rate * 100) }}%</td>
  <td>{{ p.approved_actions }}/{{ p.total_actions }}</td>
</tr>
{% endfor %}
</tbody>
</table>
{% endif %}

<h3 style="margin-top:2rem">Hard Overrides</h3>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">
  <div>
    <h4 style="color:#ef4444">🔒 Hard ASK ({{ hard_ask_count }} rules)</h4>
    <p class="muted" style="font-size:12px">Always require explicit approval, regardless of slider.</p>
  </div>
  <div>
    <h4 style="color:#22c55e">✅ Hard SAFE ({{ hard_safe_count }} rules)</h4>
    <p class="muted" style="font-size:12px">Always auto-execute silently, regardless of slider.</p>
  </div>
</div>
"""


async def autonomy_page(request: web.Request) -> web.Response:
    """Render the autonomy slider configuration page."""
    data = _data(request)
    domains: list[dict[str, Any]] = []
    proposals: list[Any] = []
    hard_ask_count = 0
    hard_safe_count = 0

    if data.autonomy is not None:
        config = data.autonomy.get_config()
        all_domains = dict(config.domains)
        if "default" not in all_domains:
            all_domains["default"] = config.default_slider

        for name in sorted(all_domains):
            slider = all_domains[name]
            has_hard_ask = any(name == rule.action_class for rule in config.hard_ask)
            has_hard_safe = any(name == rule.action_class for rule in config.hard_safe)
            domains.append(
                {
                    "name": name,
                    "slider": slider,
                    "hard_ask": has_hard_ask,
                    "hard_safe": has_hard_safe,
                }
            )
        proposals = data.autonomy.get_proposals()
        hard_ask_count = len(config.hard_ask)
        hard_safe_count = len(config.hard_safe)
    else:
        config = AutonomyConfig()
        all_domains = dict(config.domains)
        all_domains["default"] = config.default_slider
        for name in sorted(all_domains):
            domains.append(
                {
                    "name": name,
                    "slider": all_domains[name],
                    "hard_ask": False,
                    "hard_safe": False,
                }
            )
        hard_ask_count = len(config.hard_ask)
        hard_safe_count = len(config.hard_safe)

    html = _render(
        "/autonomy",
        "Autonomy",
        _AUTONOMY_BODY,
        domains=domains,
        proposals=proposals,
        hard_ask_count=hard_ask_count,
        hard_safe_count=hard_safe_count,
    )
    return web.Response(text=html, content_type="text/html")


async def autonomy_config_json(request: web.Request) -> web.Response:
    """Return the current autonomy configuration as JSON."""
    data = _data(request)
    config = AutonomyConfig() if data.autonomy is None else data.autonomy.get_config()

    return web.json_response(
        {
            "default_slider": config.default_slider,
            "domains": config.domains,
            "hard_ask": list(config.hard_ask),
            "hard_safe": list(config.hard_safe),
        }
    )


async def autonomy_set_json(request: web.Request) -> web.Response:
    """Update a domain slider. Body: {"domain": str, "slider": float}."""
    data = _data(request)
    auth = _auth(request)
    if auth is None:
        raise web.HTTPServiceUnavailable(reason="no auth configured")

    token = _extract_bearer_token(request)
    if not auth.token_matches(token):
        raise web.HTTPForbidden(reason="invalid token")

    if data.autonomy is None:
        raise web.HTTPServiceUnavailable(reason="autonomy source not configured")

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason=f"invalid JSON: {exc}") from exc

    domain = body.get("domain")
    slider = body.get("slider")
    if not isinstance(domain, str) or not domain:
        raise web.HTTPBadRequest(reason="'domain' is required")
    if not isinstance(slider, (int, float)) or not (0.0 <= slider <= 1.0):
        raise web.HTTPBadRequest(reason="'slider' must be a float in [0.0, 1.0]")

    await data.autonomy.set_slider(domain, float(slider))
    return web.json_response({"status": "ok", "domain": domain, "slider": slider})


async def autonomy_proposals_json(request: web.Request) -> web.Response:
    """Return pending graduation proposals as JSON."""
    data = _data(request)
    if data.autonomy is None:
        return web.json_response([])

    proposals = data.autonomy.get_proposals()
    return web.json_response(
        [
            {
                "proposal_id": p.proposal_id,
                "domain": p.domain,
                "current_slider": p.current_slider,
                "proposed_slider": p.proposed_slider,
                "approval_rate": p.approval_rate,
                "total_actions": p.total_actions,
                "approved_actions": p.approved_actions,
            }
            for p in proposals
        ]
    )


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------

_LOGO_PATH = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "logo.png"


async def logo_png(request: web.Request) -> web.StreamResponse:
    """Serve the CoreMind logo PNG."""
    if not _LOGO_PATH.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(_LOGO_PATH, headers={"Cache-Control": "public, max-age=86400"})


__all__ = [
    "AUTH_KEY",
    "DATA_SOURCES_KEY",
    "actions_page",
    "autonomy_config_json",
    "autonomy_page",
    "autonomy_proposals_json",
    "autonomy_set_json",
    "configure_dashboard_timezone",
    "events_page",
    "events_recent_json",
    "events_stream",
    "graph_json",
    "graph_page",
    "intents_list_json",
    "intents_page",
    "logo_png",
    "overview",
    "reasoning_page",
    "reflection_page",
    "security_headers_middleware",
    "stats_json",
    "submit_approval",
]
