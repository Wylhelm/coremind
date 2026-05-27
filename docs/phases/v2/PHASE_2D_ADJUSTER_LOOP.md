# Phase 2D — MetaAdjuster & MetaLoop Orchestration

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_2_SELF_IMPROVEMENT.md](PHASE_2_SELF_IMPROVEMENT.md)
**Prerequisites:** Phase 2A (schemas), Phase 2B (observer), Phase 2C (evaluator + validator)
**Estimated effort:** 3–4 hours

---

## 1. Goal

Implement the side-effecting components and wire everything together:

1. **`MetaAdjuster`** — applies validated adjustments to config, persists records, publishes events.
2. **`MetaLoop`** — async orchestrator that ticks periodically: observe → evaluate → validate → adjust.
3. **Daemon integration** — start/stop `MetaLoop` from the main daemon lifecycle.

---

## 2. Deliverables

| File | Purpose |
| --- | --- |
| `src/coremind/meta/adjuster.py` | `MetaAdjuster` class |
| `src/coremind/meta/loop.py` | `MetaLoop` orchestrator |
| `tests/meta/test_adjuster.py` | Adjuster unit tests |
| `tests/meta/test_loop.py` | Loop integration tests |
| `src/coremind/core/daemon.py` | **Modified** — add MetaLoop init and start/stop |
| `src/coremind/core/config.py` | **Modified** — add `MetaConfig` section |

---

## 3. MetaAdjuster

### 3.1 Interface

```python
class MetaAdjuster:
    """Applies adjustments and propagates them to the running system."""

    def __init__(
        self,
        config_store: ConfigStoreProtocol,
        meta_store: MetaStoreProtocol,
        event_bus: EventBusProtocol,
    ) -> None: ...

    async def apply(self, proposal: ProposedAdjustment) -> AdjustmentRecord:
        """Apply an adjustment: persist record, update config, publish event."""
        ...

    async def rollback(self, adjustment_id: str) -> None:
        """Revert an adjustment by restoring its old_value."""
        ...
```

### 3.2 `apply()` Steps

1. Build `AdjustmentRecord` from `ProposedAdjustment`.
2. Persist record to `meta_store.save_adjustment(record)`.
3. Update config: `config_store.set(parameter_path, new_value)`.
4. Publish event: `event_bus.publish("meta.adjustment.applied", record.model_dump())`.
5. Log with structlog: `log.info("meta.adjustment_applied", ...)`.
6. Return record.

### 3.3 `rollback()` Steps

1. Load record from `meta_store.get_adjustment(adjustment_id)`.
2. Restore: `config_store.set(record.parameter_path, record.old_value)`.
3. Mark: `record.rollback_at = datetime.now(UTC)`.
4. Persist update: `meta_store.update_adjustment(record)`.
5. Publish: `event_bus.publish("meta.adjustment.rolled_back", ...)`.

### 3.4 Protocols

```python
class ConfigStoreProtocol(Protocol):
    async def get(self, dotted_path: str) -> Any: ...
    async def set(self, dotted_path: str, value: Any) -> None: ...

class MetaStoreProtocol(Protocol):
    async def save_adjustment(self, record: AdjustmentRecord) -> None: ...
    async def get_adjustment(self, adjustment_id: str) -> AdjustmentRecord | None: ...
    async def update_adjustment(self, record: AdjustmentRecord) -> None: ...
    async def save_observations(self, observations: list[MetaObservation]) -> None: ...

class EventBusProtocol(Protocol):
    async def publish(self, topic: str, payload: dict[str, Any]) -> None: ...
```

---

## 4. MetaLoop

### 4.1 Interface

```python
class MetaLoop:
    """Orchestrates the meta-loop. Runs periodically as an asyncio task."""

    def __init__(
        self,
        observer: MetaObserver,
        evaluator: PolicyEvaluator,
        validator: MetaSafetyValidator,
        adjuster: MetaAdjuster,
        meta_store: MetaStoreProtocol,
        approval_queue: ApprovalQueueProtocol,
        config: MetaConfig,
    ) -> None: ...

    async def start(self) -> None:
        """Start the periodic loop. No-op if config.enabled is False."""
        ...

    async def stop(self) -> None:
        """Cancel the running task."""
        ...

    async def tick(self) -> None:
        """Run one loop iteration. Exposed for testing."""
        ...
```

### 4.2 `tick()` Steps

1. `observations = await self._observer.observe_all()`
2. `await self._meta_store.save_observations(observations)` (if `config.log_observations`)
3. `proposals = self._evaluator.evaluate(observations)`
4. For each proposal:
   - `result = self._validator.validate(proposal)`
   - If invalid: log warning, continue.
   - If `policy.requires_user_approval`: `await self._approval_queue.add(proposal)`, continue.
   - `await self._adjuster.apply(proposal)`
5. Log tick summary.

### 4.3 `_run_forever()` Pattern

```python
async def _run_forever(self) -> None:
    while True:
        try:
            await self.tick()
        except Exception as e:
            log.exception("meta.tick_failed", error=str(e))
        await asyncio.sleep(self._config.observation_interval_seconds)
```

Use `asyncio.create_task` in `start()`, `task.cancel()` in `stop()`.

---

## 5. Daemon Integration

### 5.1 Config Addition

Add `MetaConfig` to the daemon's config loading:

```toml
[meta]
enabled = true
observation_interval_seconds = 300
max_adjustments_per_hour = 4
```

### 5.2 Daemon Lifecycle

In `daemon.py`:

```python
# During initialization
self._meta_loop = MetaLoop(
    observer=MetaObserver(...),
    evaluator=PolicyEvaluator(DEFAULT_POLICIES, ...),
    validator=MetaSafetyValidator(FORBIDDEN_PARAMETER_PATHS, HARD_BOUNDS),
    adjuster=MetaAdjuster(...),
    meta_store=...,
    approval_queue=...,
    config=self._config.meta,
)

# During start
await self._meta_loop.start()

# During stop
await self._meta_loop.stop()
```

---

## 6. Tests

### 6.1 Adjuster Tests

```python
# tests/meta/test_adjuster.py

@pytest.mark.asyncio
async def test_apply_persists_record():
    """Applied adjustment is saved to meta_store."""

@pytest.mark.asyncio
async def test_apply_updates_config():
    """Applied adjustment updates the config_store."""

@pytest.mark.asyncio
async def test_apply_publishes_event():
    """Applied adjustment publishes to event bus."""

@pytest.mark.asyncio
async def test_rollback_restores_old_value():
    """Rollback sets parameter back to old_value."""

@pytest.mark.asyncio
async def test_rollback_marks_timestamp():
    """Rolled back record has rollback_at set."""

@pytest.mark.asyncio
async def test_rollback_unknown_id_raises():
    """Rollback with invalid adjustment_id raises ValueError."""
```

### 6.2 Loop Tests

```python
# tests/meta/test_loop.py

@pytest.mark.asyncio
async def test_tick_applies_valid_proposal():
    """One tick with a valid triggered policy applies the adjustment."""

@pytest.mark.asyncio
async def test_tick_rejects_invalid_proposal():
    """Proposals rejected by safety validator are logged, not applied."""

@pytest.mark.asyncio
async def test_tick_routes_approval_required():
    """Proposals with requires_user_approval go to the approval queue."""

@pytest.mark.asyncio
async def test_tick_with_no_observations():
    """Empty observations produce no proposals."""

@pytest.mark.asyncio
async def test_tick_exception_does_not_crash_loop():
    """An exception in one tick is caught, loop continues."""

@pytest.mark.asyncio
async def test_start_disabled_is_noop():
    """MetaLoop with enabled=False does nothing on start."""

@pytest.mark.asyncio
async def test_meta_loop_cannot_modify_hard_ask():
    """Full integration: a policy targeting hard_ask is blocked."""
```

---

## 7. Success Criteria

- [ ] `MetaAdjuster.apply()` persists, updates config, and publishes event
- [ ] `MetaAdjuster.rollback()` correctly reverts an adjustment
- [ ] `MetaLoop.tick()` orchestrates the full observe→evaluate→validate→apply pipeline
- [ ] Approval-required proposals are routed to queue, not applied
- [ ] Invalid proposals are rejected with clear log messages
- [ ] Daemon starts and stops the meta-loop cleanly
- [ ] Exception in one tick does not crash the loop
- [ ] `mypy --strict` passes
- [ ] All tests pass

---

## 8. Out of Scope

- User-facing CLI commands (Phase 2E)
- Dashboard page (Phase 2E)
- Graduation proposal UI flow (Phase 2E)
- SurrealDB persistence implementation (use in-memory or file-backed mock — concrete store adapter can be added separately)
