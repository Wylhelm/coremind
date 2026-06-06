# Phase 5E — Unified Actuator & Daemon Integration

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_5_UNIFIED_ACTUATOR.md](PHASE_5_UNIFIED_ACTUATOR.md)
**Prerequisites:** Phase 5B (discovery), Phase 5C (registry), Phase 5D (mapper)
**Estimated effort:** 3–4 hours

---

## 1. Goal

Implement the `UnifiedActuator` facade that composes Discovery, Registry, and Mapper into a single entry point. Integrate it into the daemon lifecycle and wire it to the existing L6 executor and intention loop.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/actuator/unified.py` | `UnifiedActuator` class |
| `src/coremind/core/daemon.py` | Initialize actuator on startup, schedule periodic discovery |
| `src/coremind/action/executor.py` | Add `execute_resolved()` method for `ResolvedAction` |
| `src/coremind/intention/loop.py` | Use `unified.act()` API for new intents |
| `tests/actuator/test_unified.py` | Unit tests for the facade |

---

## 3. UnifiedActuator

```python
class UnifiedActuator:
    """Single entry point for all device actions in CoreMind."""

    def __init__(
        self,
        discovery: DiscoveryEngine,
        registry: CapabilityRegistry,
        mapper: ActionMapper,
        executor: Executor,
    ) -> None: ...

    async def refresh_devices(self) -> None:
        """Run discovery and update registry."""

    async def act(
        self,
        goal: str,
        *,
        action_type: ActionType | None = None,
        room: str | None = None,
        device_type: DeviceType | None = None,
        parameters: dict[str, Any] | None = None,
        confidence: float = 0.0,
    ) -> list[ActionResult]:
        """Execute a high-level goal. Resolves → executes → returns results."""

    def list_devices(self, room: str | None = None) -> list[DeviceCapabilities]:
        """List all known devices, optionally filtered by room."""
```

---

## 4. Daemon Integration

### 4.1 Startup

In `daemon.py`, after existing subsystems are initialized:

1. Load `ActuatorConfig` from config.
2. Construct `DiscoveryEngine`, `CapabilityRegistry`, `ActionMapper`.
3. Construct `UnifiedActuator`.
4. Load persisted registry from disk.
5. If `discovery_enabled`, schedule initial discovery as a background task.

### 4.2 Periodic Discovery

Schedule a recurring task at `discovery_interval_seconds`:

```python
async def _periodic_discovery(self) -> None:
    """Re-discover devices on the configured interval."""
    while True:
        await asyncio.sleep(self._config.actuator.discovery_interval_seconds)
        await self._actuator.refresh_devices()
```

### 4.3 Graceful Shutdown

On daemon stop, persist registry to disk.

---

## 5. Executor Extension

Add to the existing `Executor` class:

```python
async def execute_resolved(
    self,
    action: ResolvedAction,
    confidence: float,
) -> ActionResult:
    """Execute a ResolvedAction via the appropriate plugin.

    Delegates to the plugin matching action.protocol.
    Respects autonomy slider: if confidence < threshold, queue for approval.
    Signs and journals the action.
    """
```

This method:
1. Looks up the plugin by `action.protocol`.
2. Checks autonomy slider threshold.
3. If approved, calls the plugin with `action.operation` and `action.parameters`.
4. Signs and journals the action (existing L6 guarantees).
5. Returns an `ActionResult`.

---

## 6. Intention Loop Changes

The intention loop should prefer the unified API for new intents:

```python
# Before (plugin-specific):
await self._executor.execute("homeassistant", "light.turn_off", {"entity_id": "light.bureau"})

# After (unified):
results = await self._actuator.act(
    "turn off bureau light",
    action_type=ActionType.TURN_OFF,
    room="office",
    confidence=intent.confidence,
)
```

Existing plugin-specific calls remain as fallback for devices not yet in the registry.

---

## 7. Migration Path

1. Both paths coexist: unified for discovered devices, direct for uncovered cases.
2. Log when falling back to direct calls → track migration progress.
3. No breaking changes to existing intent handling.

---

## 8. Tests

```python
# tests/actuator/test_unified.py

async def test_refresh_devices_populates_registry():
    """refresh_devices() calls discovery and registers results."""

async def test_act_resolves_and_executes():
    """act('turn off lights') → mapper.resolve → executor.execute_resolved."""

async def test_act_returns_results_per_device():
    """Multiple devices → one ActionResult per device."""

async def test_act_empty_when_no_match():
    """No matching devices → empty list, no error."""

async def test_list_devices_filters_by_room():
    """list_devices(room='office') returns only office devices."""

async def test_execute_resolved_respects_autonomy():
    """Low confidence + high threshold → action queued, not executed."""

async def test_execute_resolved_journals_action():
    """Executed action produces an audit journal entry."""
```

---

## 9. Out of Scope

- CLI commands and dashboard views (Phase 5F).
- New discovery methods beyond HA/mDNS/plugin (future work).
