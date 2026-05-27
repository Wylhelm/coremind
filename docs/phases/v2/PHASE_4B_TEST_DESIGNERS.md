# Phase 4B — Test Designers

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_4_AUTO_INVESTIGATION.md](PHASE_4_AUTO_INVESTIGATION.md)
**Prerequisites:** Phase 4A (schemas)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Implement `TestDesigner` — the abstract base class and all concrete implementations. Each designer knows how to produce one or more `InvestigationTest` objects for a given anomaly type.

Test designers are **pure logic**. They have no I/O dependencies and are fully unit-testable.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/investigation/designers.py` | ABC + all concrete designers |
| `tests/investigation/test_designers.py` | Unit tests for each designer |

---

## 3. Abstract Base

```python
class TestDesigner(ABC):
    """Base class for investigation test designers."""

    @abstractmethod
    def applies_to(self, anomaly_type: AnomalyType) -> bool:
        """Does this designer handle this anomaly type?"""
        ...

    @abstractmethod
    async def design(self, anomaly: AnomalyContext) -> list[InvestigationTest]:
        """Design one or more tests to verify the anomaly."""
        ...
```

---

## 4. Concrete Designers

### 4.1 `StaleDateTestDesigner`

Handles `AnomalyType.STALE_DATE_CLAIM`.

Strategy: Query the authoritative source (HA entity) for the actual timestamp of the attribute in question. Compare to the claimed date.

```python
class StaleDateTestDesigner(TestDesigner):
    def applies_to(self, anomaly_type: AnomalyType) -> bool:
        return anomaly_type == AnomalyType.STALE_DATE_CLAIM

    async def design(self, anomaly: AnomalyContext) -> list[InvestigationTest]:
        entity_id = anomaly.metadata.get("entity_id")
        attribute = anomaly.metadata.get("attribute", "last_changed")
        claimed_date = anomaly.metadata.get("claimed_date")

        if not entity_id:
            return []

        return [
            InvestigationTest(
                test_type="ha_query_entity",
                description=f"Query HA for current state of {entity_id}",
                parameters={
                    "entity_id": entity_id,
                    "attribute": attribute,
                    "claimed_date": claimed_date,
                },
                timeout_seconds=10.0,
                plugin_id="homeassistant",
            ),
        ]
```

### 4.2 `DeviceUnavailableTestDesigner`

Handles `AnomalyType.DEVICE_UNAVAILABLE`.

Strategy: Two-part — (1) check current availability, (2) look up history to find the last valid state.

```python
class DeviceUnavailableTestDesigner(TestDesigner):
    def applies_to(self, anomaly_type: AnomalyType) -> bool:
        return anomaly_type == AnomalyType.DEVICE_UNAVAILABLE

    async def design(self, anomaly: AnomalyContext) -> list[InvestigationTest]:
        entity_id = anomaly.metadata.get("entity_id")
        if not entity_id:
            return []
        return [
            InvestigationTest(
                test_type="ha_check_availability",
                description=f"Check current availability of {entity_id}",
                parameters={"entity_id": entity_id},
                timeout_seconds=10.0,
                plugin_id="homeassistant",
            ),
            InvestigationTest(
                test_type="ha_check_last_seen",
                description=f"Find last successful state change for {entity_id}",
                parameters={"entity_id": entity_id, "lookback_hours": 168},
                timeout_seconds=15.0,
                plugin_id="homeassistant",
            ),
        ]
```

### 4.3 `NumericAnomalyTestDesigner`

Handles `AnomalyType.DATA_ANOMALY_NUMERIC`.

Strategy: Query historical baseline (30 days) and re-query current value to compute z-score.

```python
class NumericAnomalyTestDesigner(TestDesigner):
    def applies_to(self, anomaly_type: AnomalyType) -> bool:
        return anomaly_type == AnomalyType.DATA_ANOMALY_NUMERIC

    async def design(self, anomaly: AnomalyContext) -> list[InvestigationTest]:
        entity_id = anomaly.metadata.get("entity_id")
        attribute = anomaly.metadata.get("attribute")
        observed_value = anomaly.metadata.get("observed_value")

        if not (entity_id and attribute):
            return []

        return [
            InvestigationTest(
                test_type="influx_baseline_query",
                description=f"Compute 30-day baseline for {entity_id}.{attribute}",
                parameters={
                    "entity_id": entity_id,
                    "attribute": attribute,
                    "lookback_days": 30,
                    "observed_value": observed_value,
                },
                timeout_seconds=20.0,
            ),
            InvestigationTest(
                test_type="ha_query_entity",
                description=f"Re-query current value of {entity_id}",
                parameters={"entity_id": entity_id, "attribute": attribute},
                timeout_seconds=10.0,
                plugin_id="homeassistant",
            ),
        ]
```

### 4.4 `MissingDataTestDesigner`

Handles `AnomalyType.MISSING_DATA`.

Strategy: Check plugin health, then attempt a force poll.

```python
class MissingDataTestDesigner(TestDesigner):
    def applies_to(self, anomaly_type: AnomalyType) -> bool:
        return anomaly_type == AnomalyType.MISSING_DATA

    async def design(self, anomaly: AnomalyContext) -> list[InvestigationTest]:
        plugin_id = anomaly.metadata.get("plugin_id")
        if not plugin_id:
            return []
        return [
            InvestigationTest(
                test_type="plugin_health_check",
                description=f"Check {plugin_id} plugin health",
                parameters={"plugin_id": plugin_id},
                timeout_seconds=10.0,
            ),
            InvestigationTest(
                test_type="plugin_force_poll",
                description=f"Force poll on {plugin_id}",
                parameters={"plugin_id": plugin_id},
                timeout_seconds=30.0,
            ),
        ]
```

### 4.5 `PatternChangeTestDesigner`

Handles `AnomalyType.PATTERN_CHANGE`.

Strategy: Compare current snapshot embedding to historical embeddings via similarity lookup.

```python
class PatternChangeTestDesigner(TestDesigner):
    def applies_to(self, anomaly_type: AnomalyType) -> bool:
        return anomaly_type == AnomalyType.PATTERN_CHANGE

    async def design(self, anomaly: AnomalyContext) -> list[InvestigationTest]:
        return [
            InvestigationTest(
                test_type="embedding_similarity_lookup",
                description="Compare current state to last 30 days",
                parameters={
                    "current_snapshot_id": anomaly.metadata.get("snapshot_id"),
                    "lookback_days": 30,
                    "top_k": 5,
                },
                timeout_seconds=15.0,
            ),
        ]
```

---

## 5. Designer Registry Helper

```python
def get_all_designers() -> list[TestDesigner]:
    """Return all built-in test designers."""
    return [
        StaleDateTestDesigner(),
        DeviceUnavailableTestDesigner(),
        NumericAnomalyTestDesigner(),
        MissingDataTestDesigner(),
        PatternChangeTestDesigner(),
    ]


def find_designer(designers: list[TestDesigner], anomaly_type: AnomalyType) -> TestDesigner | None:
    """Find the first designer that handles a given anomaly type."""
    for designer in designers:
        if designer.applies_to(anomaly_type):
            return designer
    return None
```

---

## 6. Tests

```python
# tests/investigation/test_designers.py

@pytest.mark.asyncio
async def test_stale_date_designer_creates_ha_query():
    designer = StaleDateTestDesigner()
    anomaly = AnomalyContext(
        description="Roborock hasn't cleaned since May 17",
        anomaly_type=AnomalyType.STALE_DATE_CLAIM,
        metadata={"entity_id": "vacuum.s7_max_ultra", "claimed_date": "2026-05-17"},
    )
    tests = await designer.design(anomaly)
    assert len(tests) == 1
    assert tests[0].test_type == "ha_query_entity"
    assert tests[0].parameters["entity_id"] == "vacuum.s7_max_ultra"
    assert tests[0].plugin_id == "homeassistant"


@pytest.mark.asyncio
async def test_stale_date_designer_returns_empty_without_entity():
    designer = StaleDateTestDesigner()
    anomaly = AnomalyContext(
        description="Something stale",
        anomaly_type=AnomalyType.STALE_DATE_CLAIM,
        metadata={},  # no entity_id
    )
    tests = await designer.design(anomaly)
    assert tests == []


@pytest.mark.asyncio
async def test_device_unavailable_designer_creates_two_tests():
    designer = DeviceUnavailableTestDesigner()
    anomaly = AnomalyContext(
        description="Light bureau unavailable",
        anomaly_type=AnomalyType.DEVICE_UNAVAILABLE,
        metadata={"entity_id": "light.bureau"},
    )
    tests = await designer.design(anomaly)
    assert len(tests) == 2
    assert tests[0].test_type == "ha_check_availability"
    assert tests[1].test_type == "ha_check_last_seen"


@pytest.mark.asyncio
async def test_numeric_designer_creates_baseline_and_requery():
    designer = NumericAnomalyTestDesigner()
    anomaly = AnomalyContext(
        description="Steps only 36 today",
        anomaly_type=AnomalyType.DATA_ANOMALY_NUMERIC,
        metadata={"entity_id": "sensor.steps", "attribute": "steps", "observed_value": 36},
    )
    tests = await designer.design(anomaly)
    assert len(tests) == 2
    assert tests[0].test_type == "influx_baseline_query"
    assert tests[1].test_type == "ha_query_entity"


@pytest.mark.asyncio
async def test_missing_data_designer_checks_health_and_polls():
    designer = MissingDataTestDesigner()
    anomaly = AnomalyContext(
        description="No data from weather",
        anomaly_type=AnomalyType.MISSING_DATA,
        metadata={"plugin_id": "weather"},
    )
    tests = await designer.design(anomaly)
    assert len(tests) == 2
    assert tests[0].test_type == "plugin_health_check"
    assert tests[1].test_type == "plugin_force_poll"


@pytest.mark.asyncio
async def test_pattern_change_designer_uses_embeddings():
    designer = PatternChangeTestDesigner()
    anomaly = AnomalyContext(
        description="Unusual pattern",
        anomaly_type=AnomalyType.PATTERN_CHANGE,
        metadata={"snapshot_id": "snap-123"},
    )
    tests = await designer.design(anomaly)
    assert len(tests) == 1
    assert tests[0].test_type == "embedding_similarity_lookup"


def test_find_designer_returns_match():
    designers = get_all_designers()
    d = find_designer(designers, AnomalyType.STALE_DATE_CLAIM)
    assert isinstance(d, StaleDateTestDesigner)


def test_find_designer_returns_none_for_unknown():
    designers = get_all_designers()
    d = find_designer(designers, AnomalyType.UNKNOWN)
    assert d is None
```

---

## 7. Success Criteria

- [ ] All 5 concrete designers implemented
- [ ] Each designer returns `[]` when required metadata is missing (graceful degradation)
- [ ] `get_all_designers()` returns all designers
- [ ] `find_designer()` correctly dispatches
- [ ] `ruff check` and `mypy --strict` pass
- [ ] All tests pass
