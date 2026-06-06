# Phase 5A — Actuator Schemas & Package Scaffold

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_5_UNIFIED_ACTUATOR.md](PHASE_5_UNIFIED_ACTUATOR.md)
**Prerequisites:** None
**Estimated effort:** 1–2 hours

---

## 1. Goal

Create the `src/coremind/actuator/` package with all Pydantic models, enums, and configuration types. No runtime logic — just the data layer that subsequent subphases build on.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/actuator/__init__.py` | Package init, re-exports key types |
| `src/coremind/actuator/schemas.py` | All Pydantic models and enums |
| `src/coremind/core/config.py` | Add `ActuatorConfig` section |
| `tests/actuator/__init__.py` | Test package init |
| `tests/actuator/test_schemas.py` | Validation tests for schemas |

---

## 3. Enums

### 3.1 `ActionType`

```python
class ActionType(str, enum.Enum):
    """Standardized action types across all device protocols."""
    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"
    SET_BRIGHTNESS = "set_brightness"
    SET_COLOR = "set_color"
    SET_TEMPERATURE = "set_temperature"
    SET_HUMIDITY = "set_humidity"
    SET_VOLUME = "set_volume"
    PLAY = "play"
    PAUSE = "pause"
    START_CLEANING = "start_cleaning"
    STOP_CLEANING = "stop_cleaning"
    RETURN_TO_BASE = "return_to_base"
    LOCK = "lock"
    UNLOCK = "unlock"
    OPEN = "open"
    CLOSE = "close"
    SNAPSHOT = "snapshot"
    READ_VALUE = "read_value"
    SEND_NOTIFICATION = "send_notification"
    EXECUTE_COMMAND = "execute_command"
```

### 3.2 `DeviceType`

```python
class DeviceType(str, enum.Enum):
    LIGHT = "light"
    SWITCH = "switch"
    THERMOSTAT = "thermostat"
    VACUUM = "vacuum"
    LOCK = "lock"
    MEDIA = "media"
    COVER = "cover"
    CAMERA = "camera"
    SENSOR = "sensor"
    HUMIDIFIER = "humidifier"
    SPEAKER = "speaker"
    UNKNOWN = "unknown"
```

### 3.3 `DiscoveryMethod`

```python
class DiscoveryMethod(str, enum.Enum):
    MDNS = "mdns"
    HOME_ASSISTANT = "ha"
    PLUGIN_MANIFEST = "plugin"
    UPNP = "upnp"
    STATIC_CONFIG = "static"
```

---

## 4. Pydantic Models

### 4.1 `DiscoveredDevice`

```python
class DiscoveredDevice(BaseModel):
    """Raw result from a discovery scan."""
    id: str
    name: str
    type: DeviceType
    capabilities: list[ActionType]
    source: DiscoveryMethod
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime
```

### 4.2 `Capability`

```python
class Capability(BaseModel):
    """A single thing a device can do, with protocol-specific mapping."""
    action: ActionType
    protocol: str  # "homeassistant", "hue", "sonos", "tapo"
    operation: str  # protocol-specific operation name
    parameter_mapping: dict[str, str] = Field(default_factory=dict)
```

### 4.3 `DeviceCapabilities`

```python
class DeviceCapabilities(BaseModel):
    """All capabilities of a discovered device."""
    device_id: str
    device_name: str
    device_type: DeviceType
    room: str | None = None
    capabilities: list[Capability]
    source_plugin: str
    last_seen: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 4.4 `ResolvedAction`

```python
class ResolvedAction(BaseModel):
    """A concrete action ready for execution."""
    device_id: str
    device_name: str
    action: ActionType
    protocol: str
    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
```

### 4.5 `ActionResult`

```python
class ActionResult(BaseModel):
    """Result of executing a resolved action."""
    device_id: str
    device_name: str
    action: ActionType
    success: bool
    error: str | None = None
    executed_at: datetime
```

### 4.6 `CapabilityInfo` (helper for HA domain mapping)

```python
class CapabilityInfo(BaseModel):
    """Maps an HA domain to a device type + list of supported actions."""
    device_type: DeviceType
    actions: list[ActionType]
```

---

## 5. Configuration

### 5.1 `ActuatorConfig`

Add to `src/coremind/core/config.py`:

```python
class ActuatorConfig(BaseModel):
    """Configuration for the Unified Actuator subsystem."""
    discovery_enabled: bool = True
    discovery_interval_seconds: int = 21600  # 6 hours
    mdns_timeout_seconds: float = 10.0
    ha_discovery_enabled: bool = True
    plugin_manifest_discovery_enabled: bool = True
    mdns_discovery_enabled: bool = True
    mdns_service_types: list[str] = Field(default_factory=lambda: [
        "_hap._tcp.local.",
        "_sonos._tcp.local.",
        "_googlecast._tcp.local.",
    ])
    disambiguation_model: str = "ollama/deepseek-v4-flash:cloud"
    disambiguation_max_tokens: int = 200
    registry_path: str = "~/.coremind/capability_registry.json"
```

---

## 6. Tests

```python
# tests/actuator/test_schemas.py

def test_action_type_values():
    """All ActionType members are lowercase snake_case strings."""

def test_device_type_values():
    """All DeviceType members are valid."""

def test_discovered_device_validation():
    """DiscoveredDevice rejects missing required fields."""

def test_capability_round_trip():
    """Capability serializes and deserializes correctly."""

def test_device_capabilities_with_room():
    """DeviceCapabilities.room is optional and nullable."""

def test_resolved_action_defaults():
    """ResolvedAction.parameters defaults to empty dict."""

def test_actuator_config_defaults():
    """ActuatorConfig has sane defaults for all fields."""
```

---

## 7. Out of Scope

- No runtime logic (discovery, registry queries, mapping).
- No I/O, no network calls.
- No CLI commands or dashboard views.
