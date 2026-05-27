# Phase 4C — Test Executor

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_4_AUTO_INVESTIGATION.md](PHASE_4_AUTO_INVESTIGATION.md)
**Prerequisites:** Phase 4A (schemas)
**Estimated effort:** 3–4 hours

---

## 1. Goal

Implement `TestExecutor` — the component that takes `InvestigationTest` objects and executes them against real infrastructure (Home Assistant, plugins, embedding pipeline). Each test type has a dedicated handler method. All executions are timeout-protected.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/investigation/executor.py` | `TestExecutor` class |
| `tests/investigation/test_executor.py` | Unit tests with mocked backends |

---

## 3. Architecture

```python
class TestExecutor:
    """Executes investigation tests via the plugin infrastructure."""

    def __init__(
        self,
        plugin_manager: PluginManagerProtocol,
        ha_client: HAClientProtocol,
        embedding_memory: EmbeddingMemoryProtocol | None = None,
    ):
        self._plugins = plugin_manager
        self._ha = ha_client
        self._embeddings = embedding_memory

        self._handlers: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
            "ha_query_entity": self._exec_ha_query_entity,
            "ha_check_availability": self._exec_ha_check_availability,
            "ha_check_last_seen": self._exec_ha_check_last_seen,
            "influx_baseline_query": self._exec_influx_baseline,
            "plugin_health_check": self._exec_plugin_health,
            "plugin_force_poll": self._exec_plugin_force_poll,
            "embedding_similarity_lookup": self._exec_embedding_similarity,
        }

    async def execute(self, test: InvestigationTest) -> InvestigationResult:
        """Execute a single test with timeout protection."""
        ...
```

---

## 4. Handler Methods

### 4.1 `ha_query_entity`

Query HA REST API for a single entity's current state and attributes.

```python
async def _exec_ha_query_entity(self, params: dict[str, Any]) -> dict[str, Any]:
    entity_id = params["entity_id"]
    state = await self._ha.get_state(entity_id)
    return {
        "entity_id": entity_id,
        "state": state.state,
        "attributes": state.attributes,
        "last_changed": state.last_changed.isoformat() if state.last_changed else None,
        "last_updated": state.last_updated.isoformat() if state.last_updated else None,
    }
```

### 4.2 `ha_check_availability`

Check if an entity's state is `unavailable` or `unknown`.

```python
async def _exec_ha_check_availability(self, params: dict[str, Any]) -> dict[str, Any]:
    entity_id = params["entity_id"]
    state = await self._ha.get_state(entity_id)
    return {
        "entity_id": entity_id,
        "available": state.state not in ("unavailable", "unknown"),
        "current_state": state.state,
    }
```

### 4.3 `ha_check_last_seen`

Look through history to find the last valid (non-unavailable) state.

```python
async def _exec_ha_check_last_seen(self, params: dict[str, Any]) -> dict[str, Any]:
    entity_id = params["entity_id"]
    lookback_hours = params.get("lookback_hours", 168)
    history = await self._ha.get_history(
        entity_id,
        start=datetime.now(UTC) - timedelta(hours=lookback_hours),
    )
    valid_states = [s for s in history if s.state not in ("unavailable", "unknown")]
    last_valid = valid_states[-1] if valid_states else None
    return {
        "entity_id": entity_id,
        "last_valid_state": last_valid.state if last_valid else None,
        "last_valid_at": last_valid.last_changed.isoformat() if last_valid else None,
        "lookback_hours": lookback_hours,
    }
```

### 4.4 `influx_baseline_query`

Compute mean/stdev/z-score from historical data. Uses the HA long-term statistics API or plugin data.

```python
async def _exec_influx_baseline(self, params: dict[str, Any]) -> dict[str, Any]:
    entity_id = params["entity_id"]
    attribute = params["attribute"]
    lookback_days = params.get("lookback_days", 30)
    observed_value = params.get("observed_value")

    values = await self._ha.get_statistics(
        entity_id=entity_id,
        attribute=attribute,
        lookback=timedelta(days=lookback_days),
    )

    if not values:
        return {"error": "no historical data", "entity_id": entity_id}

    import statistics
    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0
    z_score = (observed_value - mean) / stdev if (stdev > 0 and observed_value is not None) else None

    return {
        "entity_id": entity_id,
        "attribute": attribute,
        "sample_count": len(values),
        "mean": mean,
        "stdev": stdev,
        "min": min(values),
        "max": max(values),
        "observed_value": observed_value,
        "z_score": z_score,
        "is_anomalous": z_score is not None and abs(z_score) > 3,
    }
```

### 4.5 `plugin_health_check`

Query the plugin manager for a plugin's health metrics.

```python
async def _exec_plugin_health(self, params: dict[str, Any]) -> dict[str, Any]:
    plugin_id = params["plugin_id"]
    info = await self._plugins.get_health(plugin_id)
    return {
        "plugin_id": plugin_id,
        "alive": info.alive,
        "last_successful_poll": info.last_success.isoformat() if info.last_success else None,
        "error_rate_1h": info.error_rate_1h,
        "latency_p95_ms": info.latency_p95_ms,
    }
```

### 4.6 `plugin_force_poll`

Force a single poll cycle on the specified plugin.

```python
async def _exec_plugin_force_poll(self, params: dict[str, Any]) -> dict[str, Any]:
    plugin_id = params["plugin_id"]
    result = await self._plugins.force_poll(plugin_id)
    return {
        "plugin_id": plugin_id,
        "poll_success": result.success,
        "entities_returned": result.entity_count,
        "error": result.error,
    }
```

### 4.7 `embedding_similarity_lookup`

Query the embedding memory for similar past states (requires Phase 3).

```python
async def _exec_embedding_similarity(self, params: dict[str, Any]) -> dict[str, Any]:
    snapshot_id = params["current_snapshot_id"]
    top_k = params.get("top_k", 5)

    if self._embeddings is None:
        return {"error": "embedding pipeline not available", "snapshot_id": snapshot_id}

    current_embedding = await self._embeddings.get_embedding(snapshot_id)
    if current_embedding is None:
        return {"error": "no embedding available", "snapshot_id": snapshot_id}

    similar = await self._embeddings.find_similar(current_embedding, k=top_k)
    return {
        "snapshot_id": snapshot_id,
        "similar_snapshots": [
            {"id": s.snapshot_id, "score": s.score, "summary": s.summary, "timestamp": s.timestamp.isoformat()}
            for s in similar
        ],
        "highest_similarity": similar[0].score if similar else 0.0,
    }
```

---

## 5. Timeout & Error Handling

```python
async def execute(self, test: InvestigationTest) -> InvestigationResult:
    start = time.perf_counter()

    handler = self._handlers.get(test.test_type)
    if not handler:
        return InvestigationResult(
            test_id=test.test_id,
            success=False,
            raw_output={},
            error=f"No handler for test type: {test.test_type}",
            duration_seconds=0.0,
        )

    try:
        output = await asyncio.wait_for(
            handler(test.parameters),
            timeout=test.timeout_seconds,
        )
        return InvestigationResult(
            test_id=test.test_id,
            success=True,
            raw_output=output,
            duration_seconds=time.perf_counter() - start,
        )
    except asyncio.TimeoutError:
        return InvestigationResult(
            test_id=test.test_id,
            success=False,
            raw_output={},
            error=f"Test timed out after {test.timeout_seconds}s",
            duration_seconds=test.timeout_seconds,
        )
    except Exception as e:
        return InvestigationResult(
            test_id=test.test_id,
            success=False,
            raw_output={},
            error=str(e),
            duration_seconds=time.perf_counter() - start,
        )
```

---

## 6. Protocol Interfaces

Define the protocols expected by `TestExecutor` so it can be tested with mocks:

```python
class HAClientProtocol(Protocol):
    async def get_state(self, entity_id: str) -> HAState: ...
    async def get_history(self, entity_id: str, start: datetime) -> list[HAState]: ...
    async def get_statistics(self, entity_id: str, attribute: str, lookback: timedelta) -> list[float]: ...


class PluginManagerProtocol(Protocol):
    async def get_health(self, plugin_id: str) -> PluginHealth: ...
    async def force_poll(self, plugin_id: str) -> PollResult: ...


class EmbeddingMemoryProtocol(Protocol):
    async def get_embedding(self, snapshot_id: str) -> list[float] | None: ...
    async def find_similar(self, embedding: list[float], k: int) -> list[SimilarSnapshot]: ...
```

---

## 7. Tests

```python
# tests/investigation/test_executor.py

@pytest.mark.asyncio
async def test_execute_ha_query_entity(mock_ha):
    mock_ha.set_state("light.bureau", state="on", attributes={"brightness": 200})
    executor = TestExecutor(plugin_manager=Mock(), ha_client=mock_ha)

    test = InvestigationTest(
        test_type="ha_query_entity",
        description="Query light",
        parameters={"entity_id": "light.bureau"},
    )
    result = await executor.execute(test)
    assert result.success
    assert result.raw_output["state"] == "on"
    assert result.raw_output["entity_id"] == "light.bureau"


@pytest.mark.asyncio
async def test_execute_unknown_test_type():
    executor = TestExecutor(plugin_manager=Mock(), ha_client=Mock())
    test = InvestigationTest(
        test_type="nonexistent_type",
        description="Bad test",
        parameters={},
    )
    result = await executor.execute(test)
    assert not result.success
    assert "No handler" in result.error


@pytest.mark.asyncio
async def test_execute_timeout(mock_ha_slow):
    """Handler takes longer than timeout → TimeoutError → graceful result."""
    executor = TestExecutor(plugin_manager=Mock(), ha_client=mock_ha_slow)
    test = InvestigationTest(
        test_type="ha_query_entity",
        description="Slow query",
        parameters={"entity_id": "light.x"},
        timeout_seconds=0.01,
    )
    result = await executor.execute(test)
    assert not result.success
    assert "timed out" in result.error


@pytest.mark.asyncio
async def test_execute_handler_exception(mock_ha_failing):
    """Handler raises → graceful error result."""
    executor = TestExecutor(plugin_manager=Mock(), ha_client=mock_ha_failing)
    test = InvestigationTest(
        test_type="ha_query_entity",
        description="Failing query",
        parameters={"entity_id": "light.x"},
    )
    result = await executor.execute(test)
    assert not result.success
    assert result.error is not None


@pytest.mark.asyncio
async def test_execute_embedding_without_pipeline():
    """Embedding test gracefully degrades when pipeline is None."""
    executor = TestExecutor(plugin_manager=Mock(), ha_client=Mock(), embedding_memory=None)
    test = InvestigationTest(
        test_type="embedding_similarity_lookup",
        description="Embedding test",
        parameters={"current_snapshot_id": "snap-1", "top_k": 3},
    )
    result = await executor.execute(test)
    assert result.success
    assert "not available" in result.raw_output["error"]
```

---

## 8. Success Criteria

- [ ] All 7 handler methods implemented
- [ ] Timeout protection works (proven by test)
- [ ] Exception handling produces graceful `InvestigationResult` (never raises)
- [ ] Unknown test types handled gracefully
- [ ] Embedding handler gracefully degrades when Phase 3 is absent
- [ ] Protocol interfaces defined for testability
- [ ] `ruff check` and `mypy --strict` pass
- [ ] All tests pass
