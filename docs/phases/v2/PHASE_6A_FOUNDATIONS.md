# Phase 6A — Foundations (Schemas, Config, Store)

**Version:** 0.1
**Status:** ✅ Complete
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_6_SELF_MODEL.md](PHASE_6_SELF_MODEL.md)
**Prerequisites:** Phase 5 complete
**Estimated effort:** 2–3 hours

---

## 1. Goal

Lay typed foundations for the self-model without changing runtime behavior. After this sub-phase:

- The `coremind.self_model` package exists with stable Pydantic schemas.
- `SelfFact` is the atomic unit of personal knowledge (versioned, confidence-scored).
- `SelfModelStore` provides CRUD over SurrealDB.
- `SelfModelConfig` can be loaded from `~/.coremind/config.toml`.
- All new modules pass `mypy --strict` and `ruff check` with zero warnings.

No extraction, no collectors, no integration. Tests cover schemas, store operations, and config loading only.

---

## 2. Deliverables

| File | Purpose |
| ---- | ------- |
| `src/coremind/self_model/__init__.py` | Public re-exports. |
| `src/coremind/self_model/entities.py` | `SelfFact` + entity-specific models. |
| `src/coremind/self_model/config.py` | `SelfModelConfig` + `SelfModelSourcesConfig`. |
| `src/coremind/self_model/store.py` | SurrealDB adapter (CRUD, versioning, dedup). |
| `src/coremind/self_model/errors.py` | Exception hierarchy. |
| `src/coremind/config.py` | Embed `SelfModelConfig` in `DaemonConfig`. |
| `src/coremind/errors.py` | Add `SelfModelError` to root hierarchy. |
| `tests/self_model/__init__.py` | Package marker. |
| `tests/self_model/conftest.py` | Shared fixtures. |
| `tests/self_model/test_entities.py` | Schema validation tests. |
| `tests/self_model/test_config.py` | Config loading tests. |
| `tests/self_model/test_store.py` | Store CRUD tests (mocked DB). |

---

## 3. Key Design Decisions

### 3.1 `SelfFact` — the atomic unit

```python
class SelfFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str                          # ULID
    entity_type: SelfModelEntityType # person | goal | project | routine | identity | preference
    entity_id: str                   # unique within type
    attribute: str                   # what's being described
    value: JsonValue                 # the fact value
    confidence: float                # 0.0–1.0
    method: ConfidenceMethod         # declared | observed | synthesized | questioned
    source: str                      # plugin_id or "user"
    evidence: list[str]              # event IDs or descriptions
    created_at: datetime
    updated_at: datetime
    superseded_by: str | None        # ID of replacing fact
    active: bool                     # participates in reasoning?
```

### 3.2 Versioning via supersession

Facts are immutable. Updating a fact means:

1. Insert new fact with updated value/confidence.
2. Set `superseded_by` on old fact, mark `active=false`.

This preserves full history for audit.

### 3.3 Deduplication in `upsert_fact()`

- Same `(entity_type, entity_id, attribute)` with new confidence ≥ existing → supersede.
- Same key with new confidence < existing → skip (keep higher-confidence version).

---

## 4. Success Criteria

1. `SelfFact` round-trips through `model_dump(mode="json")` / `model_validate()`.
2. Invalid confidence (>1.0, <0.0), invalid entity types, and extra fields are rejected.
3. `SelfModelConfig` loads from a TOML dict matching `config.toml` structure.
4. `SelfModelStore.upsert_fact()` deduplicates correctly (tested with mock DB).
5. All 46 tests pass with `pytest tests/self_model/ -v`.
6. `mypy --strict` and `ruff check` pass on all new files.

---

## 5. Explicitly Out of Scope

- LLM extraction (Phase 6B).
- Any collector implementation (Phases 6C–6F).
- Integration with reasoning/intention (Phase 6H).
- CLI commands (Phase 6I).
- Runtime daemon wiring.
