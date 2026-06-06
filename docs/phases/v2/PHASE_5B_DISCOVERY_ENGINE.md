# Phase 5B — Discovery Engine

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_5_UNIFIED_ACTUATOR.md](PHASE_5_UNIFIED_ACTUATOR.md)
**Prerequisites:** Phase 5A (schemas)
**Estimated effort:** 3–4 hours

---

## 1. Goal

Implement the Discovery Engine — the subsystem that probes the local network and connected services for controllable devices. It produces `DiscoveredDevice` objects from multiple sources (Home Assistant API, mDNS, plugin manifests).

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/actuator/discovery.py` | `DiscoveryEngine`, `MDNSScanner`, HA discovery logic |
| `tests/actuator/test_discovery.py` | Unit tests with mocked network/HA responses |
| `pyproject.toml` | Add `zeroconf` dependency |

---

## 3. Discovery Engine

```python
class DiscoveryEngine:
    """Runs on daemon startup and periodically (every 6h by default)."""

    def __init__(
        self,
        ha_client: HomeAssistantClient | None = None,
        mdns_scanner: MDNSScanner | None = None,
        plugin_registry: PluginRegistry | None = None,
        config: ActuatorConfig | None = None,
    ) -> None: ...

    async def discover(self) -> list[DiscoveredDevice]:
        """Run all enabled discovery methods, deduplicate, return devices."""
        ...

    def _deduplicate(self, devices: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
        """Merge devices that represent the same physical entity."""
        ...
```

---

## 4. Home Assistant Discovery

Scan all HA entity states, filter to controllable domains, map each to a `DiscoveredDevice`:

```python
HA_DOMAIN_CAPABILITY_MAP = {
    "light": CapabilityInfo(DeviceType.LIGHT, [ActionType.TURN_ON, ActionType.TURN_OFF, ActionType.SET_BRIGHTNESS, ActionType.SET_COLOR]),
    "switch": CapabilityInfo(DeviceType.SWITCH, [ActionType.TURN_ON, ActionType.TURN_OFF]),
    "climate": CapabilityInfo(DeviceType.THERMOSTAT, [ActionType.SET_TEMPERATURE, ActionType.TURN_ON, ActionType.TURN_OFF]),
    "vacuum": CapabilityInfo(DeviceType.VACUUM, [ActionType.START_CLEANING, ActionType.STOP_CLEANING, ActionType.RETURN_TO_BASE]),
    "lock": CapabilityInfo(DeviceType.LOCK, [ActionType.LOCK, ActionType.UNLOCK]),
    "media_player": CapabilityInfo(DeviceType.MEDIA, [ActionType.PLAY, ActionType.PAUSE, ActionType.SET_VOLUME]),
    "cover": CapabilityInfo(DeviceType.COVER, [ActionType.OPEN, ActionType.CLOSE]),
    "camera": CapabilityInfo(DeviceType.CAMERA, [ActionType.SNAPSHOT]),
    "sensor": CapabilityInfo(DeviceType.SENSOR, [ActionType.READ_VALUE]),
    "humidifier": CapabilityInfo(DeviceType.HUMIDIFIER, [ActionType.TURN_ON, ActionType.TURN_OFF, ActionType.SET_HUMIDITY]),
}
```

Key behaviors:
- Extract `friendly_name` from attributes.
- Generate deterministic ID: `ha:{entity_id}`.
- Skip entities in unavailable/unknown state.
- Log skipped domains at debug level.

---

## 5. mDNS Scanner

```python
class MDNSScanner:
    """Scans local network for smart home devices via mDNS/Zeroconf."""

    SERVICE_TYPES = [
        "_hap._tcp.local.",        # HomeKit (Hue, etc.)
        "_sonos._tcp.local.",      # Sonos
        "_googlecast._tcp.local.", # Chromecast
        "_spotify-connect._tcp.local.",
    ]

    async def scan(self, timeout: float = 10.0) -> list[DiscoveredDevice]:
        """Scan mDNS for known service types."""
        ...
```

Implementation notes:
- Use the `zeroconf` library's async API (`AsyncZeroconf`).
- Map service types to `DeviceType` and default capabilities.
- Generate deterministic ID: `mdns:{service_type}:{name}`.
- Gracefully handle timeout and network errors.

---

## 6. Deduplication

Two devices are the same if:
1. Same `device_id` from different sources, OR
2. Same normalized name + same IP address (from raw_metadata), OR
3. HA entity links to a device whose mDNS name matches.

When deduplicating, prefer the source with richer metadata (HA > mDNS > static).

---

## 7. Error Handling

- Individual source failures do not abort the entire discovery cycle.
- Failed sources log a warning and return an empty list.
- A custom `DiscoveryError` exception for source-specific failures.

---

## 8. Tests

```python
# tests/actuator/test_discovery.py

async def test_ha_discovery_parses_lights():
    """HA light entities map to DeviceType.LIGHT with correct capabilities."""

async def test_ha_discovery_skips_unavailable():
    """Entities with state 'unavailable' are excluded."""

async def test_mdns_parses_sonos():
    """_sonos._tcp service maps to DeviceType.SPEAKER."""

async def test_mdns_timeout_returns_empty():
    """mDNS scan that times out returns empty list, no exception."""

async def test_deduplication_same_device_two_sources():
    """Same device via HA and mDNS → single DiscoveredDevice."""

async def test_one_source_failure_doesnt_abort():
    """If HA fails, mDNS results still returned."""

async def test_discover_with_no_sources():
    """Engine with no configured sources returns empty list."""
```

---

## 9. Out of Scope

- Capability Registry persistence (Phase 5C).
- Action mapping / LLM disambiguation (Phase 5D).
- CLI commands (Phase 5F).
