# Phase 2E — Meta-Loop CLI & Dashboard

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_2_SELF_IMPROVEMENT.md](PHASE_2_SELF_IMPROVEMENT.md)
**Prerequisites:** Phase 2D (all meta-loop logic is working)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Expose the meta-loop to the user through CLI commands and a dashboard page. This is the user-facing surface — read-only queries, approval flow, rollback commands.

---

## 2. Deliverables

| File | Purpose |
| --- | --- |
| `src/coremind/cli/meta.py` | `coremind meta` CLI subcommands |
| `src/coremind/cli/__init__.py` | **Modified** — register `meta` group |
| `src/coremind/dashboard/views_meta.py` | Dashboard `/meta` page |
| `src/coremind/dashboard/templates/meta.html` | Template for meta page |
| `tests/cli/test_meta_cli.py` | CLI tests |
| `tests/dashboard/test_meta_views.py` | Dashboard view tests |

---

## 3. CLI Commands

### 3.1 `coremind meta status`

```
Meta-loop: ENABLED
Last tick: 2 minutes ago
Observations this hour: 12
Adjustments this hour: 1
Pending proposals: 2 (require user approval)
```

### 3.2 `coremind meta observations [--kind <kind>] [--last <duration>]`

Table output:

```
TIMESTAMP            KIND                          VALUE   THRESHOLD  TRIGGERED
2026-05-27 10:32:00  intent_repeat_rate            0.42    0.30       YES
2026-05-27 10:32:00  notification_engagement_rate  0.65    0.30       NO
2026-05-27 10:32:00  domain_approval_rate (lights) 0.94    0.80       YES
2026-05-27 10:32:00  plugin_error_rate (ha)        0.02    0.50       NO
```

Filter options:
- `--kind <kind>` — show only one observation type
- `--last <duration>` — e.g., `24h`, `7d` (default: `24h`)

### 3.3 `coremind meta adjustments [--last <duration>]`

```
TIMESTAMP            POLICY                       PARAMETER                    OLD → NEW
2026-05-25 10:32:00  raise_salience_when_noisy    intention.min_salience       0.35 → 0.40
2026-05-24 18:11:00  throttle_failing_plugin      plugins.govee.poll_interval  300 → 600
```

### 3.4 `coremind meta policies`

```
NAME                           ENABLED  KIND                     THRESHOLD  PARAMETER
lower_salience_when_quiet      YES      intents_per_hour         <1.0       intention.min_salience
raise_salience_when_noisy      YES      low_quality_intent_rate  >0.5       intention.min_salience
increase_cooldown_on_ignored   YES      notification_ignore_rate >0.7       notifications.cooldown_seconds.*
throttle_failing_plugin        YES      plugin_error_rate        >0.5       plugins.*.poll_interval_seconds
propose_slider_promotion       YES      domain_approval_rate     >0.8       autonomy.domains.*
```

### 3.5 `coremind meta override --policy <name> --disabled|--enabled`

Disable or re-enable a policy at runtime. Persists to config.

### 3.6 `coremind meta rollback <adjustment_id>`

Calls `MetaAdjuster.rollback()`. Prints confirmation.

### 3.7 `coremind meta proposals`

List pending proposals that require user approval:

```
ID          POLICY                    PARAMETER             CHANGE     REASON
abc123      propose_slider_promotion  autonomy.domains.lights  0.8→0.9  94% approval over 50 actions
```

### 3.8 `coremind meta approve <proposal_id>` / `coremind meta deny <proposal_id>`

Approve or deny a pending proposal. On approve, calls `MetaAdjuster.apply()`.

---

## 4. Dashboard Page

### 4.1 Route: `GET /meta`

### 4.2 Layout

```
┌─ Meta-Loop Status ───────────────────────────────────────────┐
│  Status: ENABLED   Last tick: 2 min ago                       │
│  Observations (24h): 288   Adjustments (24h): 3               │
└──────────────────────────────────────────────────────────────┘

┌─ Pending Proposals ──────────────────────────────────────────┐
│  ▸ Promote `lights` slider 0.8 → 0.9                         │
│    Reason: 94% approval rate over 50 actions (30 days)        │
│    [Approve]  [Deny]                                          │
└──────────────────────────────────────────────────────────────┘

┌─ Recent Observations ────────────────────────────────────────┐
│  (Table: kind, value, threshold, triggered, timestamp)        │
└──────────────────────────────────────────────────────────────┘

┌─ Adjustment History (7d) ────────────────────────────────────┐
│  (Table: timestamp, policy, parameter, old→new, rollback btn) │
└──────────────────────────────────────────────────────────────┘
```

### 4.3 API Endpoints (for dashboard AJAX)

```
GET  /api/meta/status          → MetaStatus JSON
GET  /api/meta/observations    → list[MetaObservation] (paginated)
GET  /api/meta/adjustments     → list[AdjustmentRecord] (paginated)
GET  /api/meta/proposals       → list[ProposedAdjustment]
POST /api/meta/proposals/<id>/approve
POST /api/meta/proposals/<id>/deny
POST /api/meta/adjustments/<id>/rollback
```

---

## 5. Cockpit Additions

Add to the main cockpit page (existing dashboard):

- "Meta: N adjustments today" stat card
- "Proposals: N pending" badge (yellow if >0)

---

## 6. Tests

### 6.1 CLI Tests

```python
# tests/cli/test_meta_cli.py

def test_meta_status_output(cli_runner, mock_meta_store):
    """status command prints expected fields."""
    result = cli_runner.invoke(["meta", "status"])
    assert "ENABLED" in result.output
    assert "Last tick" in result.output

def test_meta_observations_filters_by_kind(cli_runner, mock_meta_store):
    """--kind flag filters observations."""
    result = cli_runner.invoke(["meta", "observations", "--kind", "intent_repeat_rate"])
    assert "intent_repeat_rate" in result.output
    assert "plugin_error_rate" not in result.output

def test_meta_rollback_calls_adjuster(cli_runner, mock_adjuster):
    """rollback command calls adjuster.rollback()."""
    result = cli_runner.invoke(["meta", "rollback", "abc-123"])
    mock_adjuster.rollback.assert_called_once_with("abc-123")

def test_meta_approve_applies_proposal(cli_runner, mock_meta_store, mock_adjuster):
    """approve command applies the pending proposal."""
    result = cli_runner.invoke(["meta", "approve", "prop-456"])
    assert result.exit_code == 0

def test_meta_override_disables_policy(cli_runner, mock_config_store):
    """override --disabled persists to config."""
    result = cli_runner.invoke(["meta", "override", "--policy", "my_policy", "--disabled"])
    assert result.exit_code == 0
```

### 6.2 Dashboard Tests

```python
# tests/dashboard/test_meta_views.py

@pytest.mark.asyncio
async def test_meta_status_endpoint(test_client):
    """GET /api/meta/status returns JSON with expected fields."""
    resp = await test_client.get("/api/meta/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "enabled" in data
    assert "last_tick" in data

@pytest.mark.asyncio
async def test_approve_proposal_applies(test_client, mock_adjuster):
    """POST approve endpoint triggers apply."""
    resp = await test_client.post("/api/meta/proposals/abc/approve")
    assert resp.status_code == 200
```

---

## 7. Success Criteria

- [ ] All 8 CLI commands work and produce expected output
- [ ] Dashboard `/meta` page renders with real data
- [ ] Approval flow (propose → approve → apply) works end-to-end
- [ ] Rollback from CLI reverts an adjustment
- [ ] Policy override persists across daemon restarts
- [ ] All tests pass
- [ ] `mypy --strict` passes

---

## 8. Out of Scope

- Advanced dashboard charts or graphs (future iteration)
- Notification integration for proposals (reuse existing notification system)
- Mobile-friendly dashboard (existing design patterns apply)
