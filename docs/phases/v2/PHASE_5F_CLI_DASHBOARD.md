# Phase 5F — CLI & Dashboard Integration

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_5_UNIFIED_ACTUATOR.md](PHASE_5_UNIFIED_ACTUATOR.md)
**Prerequisites:** Phase 5E (unified actuator integration)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Expose the Unified Actuator through CLI commands and the Textual dashboard. The user can discover devices, list them, execute actions, and see device inventory in the dashboard.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/cli/actuator.py` | CLI commands for actuator |
| `src/coremind/dashboard/views/devices.py` | Device inventory dashboard page |
| `src/coremind/dashboard/views.py` | Add device stat card to cockpit |
| `tests/cli/test_actuator_cli.py` | CLI command tests |
| `tests/dashboard/test_devices_view.py` | Dashboard view tests |

---

## 3. CLI Commands

### 3.1 Discovery

```bash
coremind actuator discover           # Force re-discovery now
coremind actuator discover --status  # Show last discovery results (time, count, errors)
```

### 3.2 Device Listing

```bash
coremind actuator list                        # All devices (table format)
coremind actuator list --room "living room"   # Filter by room
coremind actuator list --type light           # Filter by type
coremind actuator list --json                 # JSON output
```

Table columns: Name, Type, Room, Protocol, Capabilities, Last Seen.

### 3.3 Action Execution

```bash
coremind actuator act "turn off all lights"
coremind actuator act "set living room temperature to 21" --confidence 0.9
coremind actuator act --action turn_off --type light --room bedroom
```

Output: table of results (device, action, success/error).

### 3.4 Registry Management

```bash
coremind actuator registry export > backup.json
coremind actuator registry stats   # Count by type, protocol
```

---

## 4. Dashboard: Device Inventory Page

### 4.1 Route: `/devices`

A full-page Textual view showing:

- **Header:** "Device Inventory" + last discovery timestamp
- **Filters:** Type dropdown, Room text filter
- **Table:** Name | Type | Room | Protocol | Capabilities | Last Seen
- **Footer:** Total device count, "Refresh" button triggering discovery

### 4.2 Cockpit Integration

Add a stat card to the existing cockpit view:

- Label: "Devices"
- Value: total device count from registry
- Subtitle: last discovery time

---

## 5. Implementation Notes

- CLI uses the existing Click group structure (add `actuator` subgroup).
- Dashboard uses existing Textual DataTable widget pattern.
- Both delegate to `UnifiedActuator` — no direct registry/discovery access.
- Discovery command runs async and shows a progress indicator.

---

## 6. Tests

```python
# tests/cli/test_actuator_cli.py

def test_actuator_list_shows_devices(cli_runner):
    """'actuator list' displays registered devices in table format."""

def test_actuator_list_room_filter(cli_runner):
    """'actuator list --room office' shows only office devices."""

def test_actuator_list_json_output(cli_runner):
    """'actuator list --json' outputs valid JSON."""

def test_actuator_discover_forces_scan(cli_runner):
    """'actuator discover' triggers discovery and shows results."""

def test_actuator_act_executes(cli_runner):
    """'actuator act "turn off lights"' calls unified.act()."""

def test_actuator_registry_stats(cli_runner):
    """'actuator registry stats' shows type/protocol counts."""

# tests/dashboard/test_devices_view.py

async def test_devices_view_renders_table():
    """Device inventory page shows all registered devices."""

async def test_devices_view_filters_by_type():
    """Type filter narrows displayed devices."""
```

---

## 7. Out of Scope

- New discovery methods or protocols.
- Actuator runtime logic changes.
- Mobile or web dashboard (Textual TUI only).
