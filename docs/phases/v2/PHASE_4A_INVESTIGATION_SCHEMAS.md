# Phase 4A — Investigation Schemas & Package Scaffold

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_4_AUTO_INVESTIGATION.md](PHASE_4_AUTO_INVESTIGATION.md)
**Prerequisites:** None
**Estimated effort:** 1–2 hours

---

## 1. Goal

Create the `src/coremind/investigation/` package with all Pydantic models, enums, and configuration types. No runtime logic — just the data layer that subsequent subphases build on.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/investigation/__init__.py` | Package init, re-exports key types |
| `src/coremind/investigation/schemas.py` | All Pydantic models and enums |
| `tests/investigation/__init__.py` | Test package init |
| `tests/investigation/test_schemas.py` | Validation tests for schemas |

---

## 3. Schemas to Define

### 3.1 `InvestigationStatus`

```python
class InvestigationStatus(str, enum.Enum):
    FORMED = "formed"
    DESIGNING_TEST = "designing_test"
    EXECUTING_TEST = "executing_test"
    ANALYZING = "analyzing"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    ESCALATED = "escalated"
```

### 3.2 `AnomalyType`

```python
class AnomalyType(str, enum.Enum):
    STALE_DATE_CLAIM = "stale_date_claim"
    DEVICE_UNAVAILABLE = "device_unavailable"
    DATA_ANOMALY_NUMERIC = "data_anomaly_numeric"
    MISSING_DATA = "missing_data"
    PATTERN_CHANGE = "pattern_change"
    SERVICE_DEGRADED = "service_degraded"
    INCONSISTENT_STATE = "inconsistent_state"
    UNKNOWN = "unknown"
```

### 3.3 `AnomalyContext`

```python
class AnomalyContext(BaseModel):
    """Context passed to test designers and the investigation engine."""
    description: str
    anomaly_type: AnomalyType
    metadata: dict[str, Any] = Field(default_factory=dict)
    related_entities: list[str] = Field(default_factory=list)
```

### 3.4 `InvestigationTest`

```python
class InvestigationTest(BaseModel):
    """A concrete test to execute."""
    test_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    test_type: str                  # "ha_query", "plugin_call", "embedding_compare", etc.
    description: str                # Human-readable: "Query HA for last clean time"
    parameters: dict[str, Any]      # Test-specific parameters
    timeout_seconds: float = 30.0
    plugin_id: str | None = None    # Which plugin to invoke (if any)
```

### 3.5 `InvestigationResult`

```python
class InvestigationResult(BaseModel):
    """The result of executing a single test."""
    test_id: str
    success: bool
    raw_output: dict[str, Any]
    error: str | None = None
    duration_seconds: float
    executed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
```

### 3.6 `InvestigationConclusion`

```python
class InvestigationConclusion(BaseModel):
    """Final determination after analyzing test results."""
    verdict: Literal["resolved", "unresolved", "escalated"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    user_message: str | None = None
    suggested_action: str | None = None
```

### 3.7 `InvestigationRun`

```python
class InvestigationRun(BaseModel):
    """Top-level container for an investigation."""
    investigation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # Anomaly being investigated
    anomaly_description: str
    anomaly_type: AnomalyType
    anomaly_metadata: dict[str, Any] = Field(default_factory=dict)

    # Hypothesis under test
    hypothesis: str

    # Lifecycle
    status: InvestigationStatus = InvestigationStatus.FORMED

    # Tests & results
    tests: list[InvestigationTest] = Field(default_factory=list)
    results: list[InvestigationResult] = Field(default_factory=list)

    # Conclusion
    conclusion: InvestigationConclusion | None = None

    # Timing
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    # Retry tracking
    retry_count: int = 0
    max_retries: int = 2

    # Audit
    related_intent_ids: list[str] = Field(default_factory=list)
```

### 3.8 `InvestigationConfig`

```python
class InvestigationConfig(BaseModel):
    enabled: bool = True
    max_concurrent: int = 3
    default_timeout_seconds: float = 30.0
    max_retries: int = 2
    retention_days: int = 30
```

---

## 4. Tests

```python
# tests/investigation/test_schemas.py

def test_investigation_run_defaults():
    run = InvestigationRun(
        anomaly_description="Test anomaly",
        anomaly_type=AnomalyType.STALE_DATE_CLAIM,
        hypothesis="The claim is stale",
    )
    assert run.status == InvestigationStatus.FORMED
    assert run.investigation_id  # UUID generated
    assert run.retry_count == 0
    assert run.max_retries == 2
    assert run.tests == []
    assert run.results == []
    assert run.conclusion is None


def test_investigation_conclusion_validates_confidence():
    with pytest.raises(ValidationError):
        InvestigationConclusion(verdict="resolved", confidence=1.5, reasoning="x")
    with pytest.raises(ValidationError):
        InvestigationConclusion(verdict="resolved", confidence=-0.1, reasoning="x")


def test_anomaly_type_values():
    assert AnomalyType.STALE_DATE_CLAIM.value == "stale_date_claim"
    assert AnomalyType.DEVICE_UNAVAILABLE.value == "device_unavailable"


def test_investigation_test_default_timeout():
    test = InvestigationTest(
        test_type="ha_query_entity",
        description="Query HA",
        parameters={"entity_id": "light.bureau"},
    )
    assert test.timeout_seconds == 30.0
    assert test.test_id  # UUID generated


def test_investigation_config_defaults():
    config = InvestigationConfig()
    assert config.max_concurrent == 3
    assert config.max_retries == 2
    assert config.retention_days == 30
```

---

## 5. Success Criteria

- [ ] All models instantiate with valid defaults
- [ ] Confidence field rejects values outside [0.0, 1.0]
- [ ] UUID fields auto-generate
- [ ] `InvestigationStatus` and `AnomalyType` enums are complete
- [ ] `ruff check` and `mypy --strict` pass
- [ ] All tests pass
