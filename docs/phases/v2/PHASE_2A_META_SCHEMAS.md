# Phase 2A — Meta-Loop Schemas & Package Scaffold

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_2_SELF_IMPROVEMENT.md](PHASE_2_SELF_IMPROVEMENT.md)
**Prerequisites:** None
**Estimated effort:** 1–2 hours

---

## 1. Goal

Create the `src/coremind/meta/` package with all Pydantic models, constants, and configuration types. No runtime logic — just the data layer that subsequent subphases build on.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/meta/__init__.py` | Package init, re-exports key types |
| `src/coremind/meta/schemas.py` | All Pydantic models |
| `src/coremind/meta/constants.py` | `FORBIDDEN_PARAMETER_PATHS`, `HARD_BOUNDS`, `DEFAULT_POLICIES` |
| `tests/meta/__init__.py` | Test package init |
| `tests/meta/test_schemas.py` | Validation tests for schemas |

---

## 3. Schemas to Define

### 3.1 `MetaObservation`

```python
class MetaObservation(BaseModel):
    """A single measured metric about system performance."""
    observation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: str
    value: float
    threshold: float
    window_seconds: float
    triggers_policy: bool = False
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 3.2 `AdjustmentPolicy`

```python
class AdjustmentPolicy(BaseModel):
    """A rule that maps observations to parameter changes."""
    name: str
    description: str
    observation_kind: str
    trigger_condition: Literal["above", "below", "between"]
    threshold: float
    threshold_upper: float | None = None
    parameter_path: str
    direction: Literal["increase", "decrease"]
    delta: float
    min_value: float
    max_value: float
    cooldown_seconds: float
    requires_user_approval: bool = False
    enabled: bool = True
```

### 3.3 `AdjustmentRecord`

```python
class AdjustmentRecord(BaseModel):
    """Record of an applied adjustment."""
    adjustment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    policy_name: str
    parameter_path: str
    old_value: Any
    new_value: Any
    reason: str
    triggered_by_observation_id: str
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rollback_at: datetime | None = None
    user_approved: bool = False
    user_approved_at: datetime | None = None
```

### 3.4 `ProposedAdjustment`

```python
class ProposedAdjustment(BaseModel):
    """An adjustment proposed by the evaluator, not yet validated."""
    policy: AdjustmentPolicy
    observation: MetaObservation
    parameter_path: str
    old_value: float
    new_value: float
```

### 3.5 `ValidationResult`

```python
class ValidationResult(BaseModel):
    """Result of safety validation."""
    valid: bool
    reason: str = ""
```

### 3.6 `MetaConfig`

```python
class MetaConfig(BaseModel):
    """Configuration for the meta-loop."""
    enabled: bool = True
    observation_interval_seconds: float = 300.0
    max_adjustments_per_hour: int = 4
    require_observation_window_days: int = 1
    log_observations: bool = True
    log_observations_retention_days: int = 30
```

---

## 4. Constants

### 4.1 `FORBIDDEN_PARAMETER_PATHS`

```python
FORBIDDEN_PARAMETER_PATHS: list[str] = [
    "autonomy.hard_ask",
    "autonomy.hard_safe",
    "intention.quiet_hours",
    "notifications.quiet_hours",
    "secrets.*",
    "plugins.*.permissions",
    "plugins.*.action_classes",
    "audit.*",
    "logging.*",
    "meta.forbidden_parameter_paths",
    "meta.safety_bounds",
    "meta.enabled",
]
```

### 4.2 `HARD_BOUNDS`

```python
HARD_BOUNDS: dict[str, tuple[float, float]] = {
    "intention.min_salience": (0.20, 0.70),
    "intention.min_confidence": (0.20, 0.80),
    "reasoning.interval_seconds": (60.0, 7200.0),
    "intention.interval_seconds": (60.0, 3600.0),
    "reflection.interval_seconds": (1800.0, 86400.0),
    "plugins.*.poll_interval_seconds": (30.0, 86400.0),
    "notifications.cooldown_seconds.*": (60.0, 86400.0),
    "autonomy.domains.*": (0.0, 1.0),
}
```

### 4.3 `DEFAULT_POLICIES`

See parent doc §2.3.2 for full list. Define all 7 built-in policies.

---

## 5. Tests

```python
# tests/meta/test_schemas.py

def test_meta_observation_defaults():
    """Observation ID and timestamp are auto-generated."""
    obs = MetaObservation(kind="test", value=0.5, threshold=0.3, window_seconds=60)
    assert obs.observation_id
    assert obs.observed_at

def test_adjustment_policy_validation():
    """Policies reject invalid min/max bounds."""
    # min_value > max_value should be rejected (add validator)

def test_hard_bounds_are_all_valid_tuples():
    """Every entry in HARD_BOUNDS has min < max."""
    for path, (min_v, max_v) in HARD_BOUNDS.items():
        assert min_v < max_v, f"Bad bounds for {path}"

def test_forbidden_paths_no_duplicates():
    """No duplicate entries in FORBIDDEN_PARAMETER_PATHS."""
    assert len(FORBIDDEN_PARAMETER_PATHS) == len(set(FORBIDDEN_PARAMETER_PATHS))

def test_default_policies_have_unique_names():
    """All default policies have distinct names."""
    names = [p.name for p in DEFAULT_POLICIES]
    assert len(names) == len(set(names))
```

---

## 6. Success Criteria

- [ ] `from coremind.meta.schemas import MetaObservation, AdjustmentPolicy, ...` works
- [ ] `from coremind.meta.constants import FORBIDDEN_PARAMETER_PATHS, HARD_BOUNDS, DEFAULT_POLICIES` works
- [ ] All tests pass
- [ ] `mypy --strict` passes on the new package
- [ ] No runtime logic (no async, no I/O, no imports from other coremind modules beyond `errors`)

---

## 7. Out of Scope

- Observer logic (Phase 2B)
- Policy evaluation (Phase 2C)
- Safety validator (Phase 2C)
- Adjuster and loop orchestration (Phase 2D)
- CLI and dashboard (Phase 2E)
