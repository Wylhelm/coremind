# Phase 6I — CLI + Dashboard + NarrativeMemory Migration

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_6_SELF_MODEL.md](PHASE_6_SELF_MODEL.md)
**Prerequisites:** Phase 6H complete
**Estimated effort:** 3–4 hours

---

## 1. Goal

User can inspect, manage, and monitor the self-model via CLI and dashboard. After this sub-phase:

- CLI commands list, show, set, forget, export, and import self-model facts.
- Dashboard panel shows entity counts, confidence distribution, and recent learnings.
- NarrativeMemory data is fully migrated (one-time) and old class deprecated.

---

## 2. Deliverables

| File | Purpose |
| ---- | ------- |
| `src/coremind/self_model/cli.py` | CLI command handlers. |
| `src/coremind/dashboard/panels/self_model.py` | Dashboard data provider. |
| `src/coremind/cli/self_model.py` | CLI entry point (registers subcommands). |
| `tests/self_model/test_cli.py` | CLI tests. |
| `tests/dashboard/test_self_model_panel.py` | Dashboard panel tests. |

---

## 3. Tasks for the Coding Agent

### 6I.1 CLI Commands

**File:** `src/coremind/self_model/cli.py`

Register under `coremind self-model` subcommand group:

```text
coremind self-model list [--type TYPE] [--active-only] [--min-confidence FLOAT]
    List all active self-model facts, optionally filtered.
    Output: table with entity, attribute, value, confidence, method, updated_at.

coremind self-model show <entity_type>:<entity_id>
    Show all facts for a specific entity, including superseded history.

coremind self-model set <entity_type>:<entity_id> <attribute> <value>
    Declare a fact explicitly (confidence=1.0, method=declared).

coremind self-model forget <entity_type>:<entity_id> [--reason REASON]
    Deactivate all facts for an entity. Logged to audit journal.

coremind self-model sources
    Show which collectors are active, their last run time, and fact count.

coremind self-model export [--format json|toml]
    Export all active facts for backup.

coremind self-model import <file>
    Import facts from a backup or seed file.

coremind self-model stats
    Show: total facts, counts by type, confidence distribution,
    stale facts (>7 days without refresh), method distribution.
```

### 6I.2 Dashboard Panel

**File:** `src/coremind/dashboard/panels/self_model.py`

Add a `/api/self-model` endpoint group:

```python
async def get_self_model_overview() -> dict:
    """Return self-model dashboard data.

    Response:
    {
        "entity_counts": {"person": 5, "project": 3, "routine": 4, ...},
        "total_facts": 42,
        "confidence_distribution": {"0.9-1.0": 12, "0.7-0.9": 18, "0.5-0.7": 8, "0.3-0.5": 4},
        "method_distribution": {"declared": 10, "observed": 22, "synthesized": 8, "questioned": 2},
        "recent_learnings": [...],  // Last 10 facts created/updated in 24h
        "stale_facts": [...],       // Facts not refreshed in >7 days
        "sources": [                // Collector health
            {"name": "github", "last_run": "...", "facts_produced": 5, "status": "healthy"},
            ...
        ]
    }
    """
```

### 6I.3 NarrativeMemory Final Migration

Run `NarrativeMemory.migrate_to_self_model()` once during daemon startup if:
- Self-model is enabled.
- NarrativeState file exists.
- Migration hasn't been marked as done (store a `_migration_complete` flag).

After migration:
- Log: "NarrativeMemory migrated to Self-Model: N facts created."
- Add deprecation warning to all NarrativeMemory method calls.
- Do NOT delete the NarrativeState file (keep for rollback if needed).

---

## 4. Success Criteria

1. `coremind self-model list` shows all active entities with correct formatting.
2. `coremind self-model set person:test name "Test"` creates a declared fact.
3. `coremind self-model forget person:test` deactivates all facts for that entity.
4. `coremind self-model export --format json | coremind self-model import /dev/stdin` round-trips.
5. Dashboard `/api/self-model` endpoint returns valid JSON with expected structure.
6. NarrativeMemory migration runs once and is idempotent.
7. All tests pass.

---

## 5. Explicitly Out of Scope

- Advanced dashboard visualization (charts, graphs) — data API only for now.
- Real-time WebSocket updates for self-model changes.
- Multi-user CLI authentication.
