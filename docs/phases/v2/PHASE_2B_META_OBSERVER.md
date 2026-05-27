# Phase 2B — MetaObserver

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_2_SELF_IMPROVEMENT.md](PHASE_2_SELF_IMPROVEMENT.md)
**Prerequisites:** Phase 2A (schemas and constants exist)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Implement `MetaObserver` — the component that collects system performance metrics from L1–L7. It produces `MetaObservation` objects. It has no side effects beyond reading.

---

## 2. Deliverables

| File | Purpose |
| --- | --- |
| `src/coremind/meta/observer.py` | `MetaObserver` class |
| `tests/meta/test_observer.py` | Unit tests with mocked stores |

---

## 3. Interface

```python
class MetaObserver:
    """Collects observations about system performance from all layers."""

    def __init__(
        self,
        intention_store: IntentionStoreProtocol,
        action_store: ActionStoreProtocol,
        plugin_registry: PluginRegistryProtocol,
        narrative_store: NarrativeStoreProtocol,
    ) -> None: ...

    async def observe_all(self) -> list[MetaObservation]:
        """Collect all observation kinds. Returns one or more per kind."""
        ...
```

---

## 4. Observations to Implement

Implement each as a private `async def _observe_<kind>()` method:

### 4.1 `intent_repeat_rate`

- **Source:** `intention_store.list_recent(timedelta(hours=6))`
- **Computation:** Group intents by `md5(topic + sorted(key_entities))`. Count duplicates. `value = repeats / total`.
- **Threshold:** 0.30

### 4.2 `notification_engagement_rate`

- **Source:** `action_store.list_notifications(timedelta(days=7))`
- **Computation:** `value = engaged / total`. Return 0.5 if no data.
- **Threshold:** 0.30

### 4.3 `domain_approval_rate` (per domain)

- **Source:** `action_store.list_recent(timedelta(days=30))`
- **Computation:** Filter to category=`ask`. Group by domain (use `classify_action()`). Skip domains with <10 actions. `value = approved / total`.
- **Threshold:** 0.80
- **Returns:** One `MetaObservation` per qualifying domain. Store `domain` in `metadata`.

### 4.4 `plugin_error_rate` (per plugin)

- **Source:** `plugin_registry.list_active()` + `plugin_registry.get_stats(plugin_id, timedelta(hours=1))`
- **Computation:** `value = errors / total_calls`. Skip if `total_calls == 0`.
- **Threshold:** 0.50
- **Returns:** One per plugin with errors. Store `plugin_id` in `metadata`.

### 4.5 `token_per_useful_intent`

- **Source:** `narrative_store.total_tokens(timedelta(hours=24))` + `intention_store.count_useful(timedelta(hours=24))`
- **Computation:** `value = tokens / max(intents, 1)`
- **Threshold:** 5000.0

### 4.6 `investigation_success_rate`

- **Source:** `narrative_store.list_investigations(timedelta(days=7))`
- **Computation:** `value = resolved / total`. Return 1.0 if no data.
- **Threshold:** 0.60

### 4.7 `low_quality_intent_rate`

- **Source:** `intention_store.list_recent(timedelta(hours=24))`
- **Computation:** `value = (intents with salience<0.4 or confidence<0.5) / total`. Return 0.0 if no data.
- **Threshold:** 0.50

---

## 5. Protocol Definitions

Define minimal `Protocol` types for the stores so `MetaObserver` doesn't import concrete implementations:

```python
# In src/coremind/meta/protocols.py (or at top of observer.py)

class IntentionStoreProtocol(Protocol):
    async def list_recent(self, window: timedelta) -> list[Any]: ...
    async def count_useful(self, window: timedelta) -> int: ...

class ActionStoreProtocol(Protocol):
    async def list_recent(self, window: timedelta) -> list[Any]: ...
    async def list_notifications(self, window: timedelta) -> list[Any]: ...

class PluginRegistryProtocol(Protocol):
    async def list_active(self) -> list[Any]: ...
    async def get_stats(self, plugin_id: str, window: timedelta) -> Any: ...

class NarrativeStoreProtocol(Protocol):
    async def total_tokens(self, window: timedelta) -> int: ...
    async def list_investigations(self, window: timedelta) -> list[Any]: ...
```

If these protocols already exist in the codebase, use them. Otherwise create `src/coremind/meta/protocols.py`.

---

## 6. Tests

```python
# tests/meta/test_observer.py

@pytest.mark.asyncio
async def test_intent_repeat_rate_no_repeats():
    """All unique intents → rate = 0.0."""
    store = MockIntentionStore([
        make_intent(topic="a", entities=["x"]),
        make_intent(topic="b", entities=["y"]),
    ])
    observer = MetaObserver(store, ...)
    obs = await observer._observe_intent_repeat_rate()
    assert obs.value == 0.0

@pytest.mark.asyncio
async def test_intent_repeat_rate_all_repeats():
    """All identical intents → rate = (n-1)/n."""
    store = MockIntentionStore([
        make_intent(topic="a", entities=["x"]),
        make_intent(topic="a", entities=["x"]),
        make_intent(topic="a", entities=["x"]),
    ])
    observer = MetaObserver(store, ...)
    obs = await observer._observe_intent_repeat_rate()
    assert abs(obs.value - 2/3) < 0.01

@pytest.mark.asyncio
async def test_plugin_error_rate_skips_zero_calls():
    """Plugins with zero calls produce no observation."""
    registry = MockPluginRegistry(plugins=[
        MockPlugin(id="p1", stats=PluginStats(total_calls=0, errors=0)),
    ])
    observer = MetaObserver(..., plugin_registry=registry, ...)
    obs_list = await observer._observe_plugin_error_rates()
    assert obs_list == []

@pytest.mark.asyncio
async def test_domain_approval_rate_skips_small_domains():
    """Domains with <10 ASK actions are skipped."""
    store = MockActionStore(actions=[
        make_action(action_class="light.turn_on", category="ask", approved=True)
        for _ in range(5)
    ])
    observer = MetaObserver(..., action_store=store, ...)
    obs_list = await observer._observe_domain_approval_rates()
    assert obs_list == []

@pytest.mark.asyncio
async def test_observe_all_returns_all_kinds():
    """observe_all() covers all expected observation kinds."""
    observer = make_full_observer()
    results = await observer.observe_all()
    kinds = {o.kind for o in results}
    assert "intent_repeat_rate" in kinds
    assert "notification_engagement_rate" in kinds
    assert "token_per_useful_intent" in kinds
    assert "low_quality_intent_rate" in kinds
    assert "investigation_success_rate" in kinds
```

---

## 7. Success Criteria

- [ ] `MetaObserver.observe_all()` returns `list[MetaObservation]` with all 7+ observation kinds
- [ ] Each observation method is independently testable with mocked stores
- [ ] No side effects (read-only)
- [ ] Handles empty data gracefully (returns neutral values, never divides by zero)
- [ ] `mypy --strict` passes
- [ ] All tests pass

---

## 8. Out of Scope

- Acting on observations (Phase 2C)
- Persisting observations to SurrealDB (Phase 2D)
- CLI/dashboard display (Phase 2E)
