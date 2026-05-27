# Phase 4F — CLI, Dashboard & Stale-Pruner Integration

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_4_AUTO_INVESTIGATION.md](PHASE_4_AUTO_INVESTIGATION.md)
**Prerequisites:** Phase 4E (engine & store)
**Estimated effort:** 3–4 hours

---

## 1. Goal

Wire the investigation engine into the running system:

1. CLI commands for listing, inspecting, cancelling, and manually triggering investigations
2. Dashboard page showing active + recent investigations with stats
3. Integration with the existing `stale_investigation_pruner.py` — verify before pruning
4. Daemon initialization — add `InvestigationEngine` to the process topology
5. Configuration loading from `~/.coremind/config.toml`

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/cli/investigations.py` | CLI commands (click group) |
| `src/coremind/dashboard/pages/investigations.py` | Dashboard page |
| `src/coremind/core/daemon.py` | Modified — initialize engine |
| `src/coremind/intention/stale_investigation_pruner.py` | Modified — hook into engine |
| `src/coremind/intention/loop.py` | Modified — trigger investigations on anomaly |
| `tests/cli/test_investigations_cli.py` | CLI tests |
| `tests/investigation/test_integration.py` | Integration test: pruner → engine flow |

---

## 3. CLI Commands

### 3.1 Command Group

```python
@click.group("investigations")
def investigations_group():
    """Manage auto-investigations."""
    pass
```

### 3.2 `list`

```bash
coremind investigations list [--status <status>] [--last <duration>]
```

Output format:

```
ID         Status     Anomaly                              Duration  Verdict
─────────  ─────────  ───────────────────────────────────  ────────  ────────
a1b2c3d4   RESOLVED   Roborock hasn't cleaned since May 17   4.2s    resolved
e5f6g7h8   ESCALATED  Light bureau unavailable               8.1s    escalated
i9j0k1l2   ACTIVE     Health steps anomaly (36)              —       —
```

### 3.3 `show`

```bash
coremind investigations show <id>
```

Displays full investigation details: hypothesis, tests run, results, conclusion, timing.

### 3.4 `cancel`

```bash
coremind investigations cancel <id>
```

### 3.5 `trigger`

```bash
coremind investigations trigger \
    --anomaly-type stale_date_claim \
    --entity-id "vacuum.s7_max_ultra" \
    --claimed-date "2026-05-17" \
    --description "Vacuum hasn't cleaned since 2026-05-17"
```

### 3.6 `stats`

```bash
coremind investigations stats [--last <duration>]
```

Output:

```
Investigations last 7d: 47
  Resolved:   31 (66%)
  Escalated:  12 (26%)
  Unresolved:  4 (8%)
Avg duration: 8.2s
Top anomaly type: stale_date_claim (22)
```

---

## 4. Dashboard Page

### 4.1 Route

`/investigations` — accessible from the main nav.

### 4.2 Layout

```
┌─ Investigation Stats (last 7 days) ─────────────────────────────┐
│  Total: 47    Resolved: 31 (66%)    Escalated: 12 (26%)          │
│               Unresolved: 4 (8%)    Avg duration: 8.2s           │
└──────────────────────────────────────────────────────────────────┘

┌─ Active Investigations ──────────────────────────────────────────┐
│  ▸ light.bureau_unavailable                                       │
│    Status: EXECUTING_TEST    Started: 12s ago    Tests: 2/2       │
│    [Cancel]                                                       │
└──────────────────────────────────────────────────────────────────┘

┌─ Recent Investigations ──────────────────────────────────────────┐
│  ✓ Roborock 'hasn't cleaned since May 17'                          │
│    RESOLVED · 2 min ago · Claim was stale (actual: May 24)        │
│                                                                  │
│  🚨 Light bureau unavailable                                        │
│    ESCALATED · 1h ago · Offline for 73h                           │
│                                                                  │
│  ? Health steps 36 today                                          │
│    UNRESOLVED · 3h ago · Insufficient baseline data               │
└──────────────────────────────────────────────────────────────────┘
```

### 4.3 Cockpit Widget

Add to existing cockpit page:
- "Investigations: N active / M resolved this week"

---

## 5. Stale-Pruner Integration

### 5.1 Current Behavior

The existing `stale_investigation_pruner.py` uses date-pattern regex to identify stale claims and removes them after a time threshold.

### 5.2 New Behavior

Before removing a stale candidate, the pruner triggers an auto-investigation to verify the claim. Only prune if the investigation resolves (claim confirmed stale), otherwise wait for the investigation to complete.

```python
async def prune_with_verification(
    pruner: StaleInvestigationPruner,
    engine: InvestigationEngine,
    snapshot: WorldSnapshot,
) -> list[str]:
    """Verify stale claims by running investigations before pruning."""
    candidates = pruner.find_stale_candidates(snapshot)
    triggered_ids = []

    for candidate in candidates:
        anomaly = AnomalyContext(
            description=candidate.text,
            anomaly_type=AnomalyType.STALE_DATE_CLAIM,
            metadata={
                "entity_id": candidate.entity_id,
                "attribute": "last_changed",
                "claimed_date": candidate.claimed_date,
            },
        )
        inv_id = await engine.investigate(anomaly)
        triggered_ids.append(inv_id)

    return triggered_ids
```

---

## 6. Daemon Initialization

In `src/coremind/core/daemon.py`, add engine construction during startup:

```python
# During daemon initialization
investigation_store = InvestigationStore(db=surreal_client)
test_executor = TestExecutor(
    plugin_manager=plugin_manager,
    ha_client=ha_client,
    embedding_memory=embedding_memory,  # None if Phase 3 not yet deployed
)
result_analyzer = ResultAnalyzer(llm_client=llm_client)
investigation_engine = InvestigationEngine(
    designers=get_all_designers(),
    executor=test_executor,
    analyzer=result_analyzer,
    store=investigation_store,
    narrative_store=narrative_store,
    event_bus=event_bus,
    config=InvestigationConfig.from_toml(config),
)
```

---

## 7. Intention Loop Hook

When L4/L5 detects an anomaly, trigger an investigation:

```python
# In intention/loop.py, when an anomaly is detected
if anomaly_detected:
    anomaly_ctx = AnomalyContext(
        description=anomaly.description,
        anomaly_type=classify_anomaly(anomaly),
        metadata=extract_anomaly_metadata(anomaly),
    )
    await investigation_engine.investigate(anomaly_ctx)
```

---

## 8. Configuration

Add to `~/.coremind/config.toml`:

```toml
[investigation]
enabled = true
max_concurrent = 3
default_timeout_seconds = 30.0
max_retries = 2
retention_days = 30

[investigation.anomaly_types.stale_date_claim]
enabled = true
auto_trigger = true
auto_resolve = true
escalation_cooldown_seconds = 86400

[investigation.anomaly_types.device_unavailable]
enabled = true
auto_trigger = true
auto_resolve = false
escalation_cooldown_seconds = 21600
```

---

## 9. Tests

```python
# tests/cli/test_investigations_cli.py

def test_investigations_list(cli_runner, mock_store):
    mock_store.list_recent.return_value = [make_investigation_run()]
    result = cli_runner.invoke(["investigations", "list"])
    assert result.exit_code == 0
    assert "RESOLVED" in result.output or "FORMED" in result.output


def test_investigations_stats(cli_runner, mock_store):
    result = cli_runner.invoke(["investigations", "stats"])
    assert result.exit_code == 0
    assert "Resolved" in result.output


def test_investigations_trigger(cli_runner, mock_engine):
    result = cli_runner.invoke([
        "investigations", "trigger",
        "--anomaly-type", "stale_date_claim",
        "--entity-id", "vacuum.s7_max_ultra",
        "--description", "Test",
    ])
    assert result.exit_code == 0
    mock_engine.investigate.assert_called_once()


# tests/investigation/test_integration.py

@pytest.mark.asyncio
async def test_pruner_triggers_investigation():
    """Stale pruner triggers investigation instead of blindly removing."""
    pruner = StaleInvestigationPruner(...)
    engine = make_engine(...)

    # Set up a stale-looking candidate
    snapshot = make_snapshot_with_stale_claim("vacuum.s7", "2026-05-17")

    triggered = await prune_with_verification(pruner, engine, snapshot)
    assert len(triggered) >= 1


@pytest.mark.asyncio
async def test_daemon_initializes_engine(mock_config):
    """Daemon correctly wires up the investigation engine."""
    daemon = Daemon(config=mock_config)
    await daemon.start()
    assert daemon.investigation_engine is not None
    assert daemon.investigation_engine._config.max_concurrent == 3
    await daemon.stop()
```

---

## 10. Success Criteria

- [ ] All CLI commands functional (`list`, `show`, `cancel`, `trigger`, `stats`)
- [ ] Dashboard page renders active + recent investigations
- [ ] Cockpit widget shows investigation count
- [ ] Stale-pruner uses engine for verification instead of blind removal
- [ ] Daemon initializes engine on startup
- [ ] Intention loop triggers investigations on anomaly detection
- [ ] Config loaded from TOML
- [ ] `ruff check` and `mypy --strict` pass
- [ ] All tests pass
