# Phase 5C — Capability Registry

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_5_UNIFIED_ACTUATOR.md](PHASE_5_UNIFIED_ACTUATOR.md)
**Prerequisites:** Phase 5A (schemas)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Implement the Capability Registry — an in-memory + persisted index of all known device capabilities. It is the single source of truth for "what can be controlled and how."

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/actuator/registry.py` | `CapabilityRegistry` class |
| `tests/actuator/test_registry.py` | Unit tests for indexing, querying, persistence |

---

## 3. CapabilityRegistry

```python
class CapabilityRegistry:
    """In-memory + persisted registry of all known device capabilities."""

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._devices: dict[str, DeviceCapabilities] = {}
        self._by_type: dict[DeviceType, list[str]] = defaultdict(list)
        self._by_action: dict[ActionType, list[str]] = defaultdict(list)
```

---

## 4. API Surface

### 4.1 Registration

```python
def register(self, device: DeviceCapabilities) -> None:
    """Add or update a device in the registry. Rebuilds indexes."""
```

- If a device with the same `device_id` exists, overwrite and re-index.
- Update `_by_type` and `_by_action` indexes.

### 4.2 Queries

```python
def find_by_action(self, action: ActionType, room: str | None = None) -> list[DeviceCapabilities]:
    """Find all devices capable of a given action, optionally filtered by room."""

def find_by_type(self, device_type: DeviceType, room: str | None = None) -> list[DeviceCapabilities]:
    """Find all devices of a given type, optionally filtered by room."""

def find_by_id(self, device_id: str) -> DeviceCapabilities | None:
    """Look up a single device by ID."""

def all_devices(self) -> list[DeviceCapabilities]:
    """Return all registered devices sorted by name."""

def to_compact_summary(self, room: str | None = None) -> str:
    """Return a compact text summary for LLM prompts (device list with capabilities)."""
```

### 4.3 Persistence

```python
async def persist(self) -> None:
    """Write registry to disk as JSON."""

async def load(self) -> None:
    """Load registry from disk. Rebuilds indexes."""
```

Format: JSON file at `registry_path` from `ActuatorConfig`. Schema:

```json
{
  "version": 1,
  "devices": [
    {
      "device_id": "ha:light.bureau",
      "device_name": "Bureau Light",
      "device_type": "light",
      "room": "office",
      "capabilities": [...],
      "source_plugin": "homeassistant",
      "last_seen": "2026-05-28T10:00:00Z",
      "metadata": {}
    }
  ]
}
```

### 4.4 Stats

```python
def stats(self) -> dict[str, int]:
    """Return counts by device type and protocol."""
```

---

## 5. Index Rebuild

When loading from disk or after bulk registration, rebuild all indexes:

```python
def _rebuild_indexes(self) -> None:
    """Clear and rebuild _by_type and _by_action from _devices."""
```

---

## 6. Tests

```python
# tests/actuator/test_registry.py

def test_register_and_find_by_action():
    """Registered device appears in find_by_action results."""

def test_find_by_action_with_room_filter():
    """find_by_action(TURN_ON, room='bedroom') returns only bedroom devices."""

def test_find_by_type():
    """find_by_type(LIGHT) returns all light devices."""

def test_find_by_id_exists():
    """find_by_id returns the correct device."""

def test_find_by_id_missing():
    """find_by_id returns None for unknown device."""

def test_register_overwrites_existing():
    """Re-registering a device updates it in place."""

async def test_persist_and_load_round_trip(tmp_path):
    """Persist → load → all devices and indexes restored."""

def test_to_compact_summary():
    """Summary is a short text listing device names and capabilities."""

def test_stats():
    """Stats return correct counts by type."""
```

---

## 7. Out of Scope

- Discovery logic (Phase 5B).
- Action mapping / goal resolution (Phase 5D).
- CLI or dashboard (Phase 5F).
