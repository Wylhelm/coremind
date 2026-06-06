# Phase 5D — Action Mapper

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_5_UNIFIED_ACTUATOR.md](PHASE_5_UNIFIED_ACTUATOR.md)
**Prerequisites:** Phase 5A (schemas), Phase 5C (registry)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Implement the Action Mapper — the component that translates high-level goals (natural language or structured) into protocol-specific `ResolvedAction` objects by querying the Capability Registry and optionally using a lightweight LLM call for disambiguation.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/actuator/mapper.py` | `ActionMapper` class |
| `tests/actuator/test_mapper.py` | Unit tests with mocked registry and LLM |

---

## 3. ActionMapper

```python
class ActionMapper:
    """Maps high-level goals to protocol-specific operations."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        llm: LLM | None = None,
        config: ActuatorConfig | None = None,
    ) -> None: ...
```

---

## 4. Resolution Logic

### 4.1 `resolve()` method

```python
def resolve(
    self,
    goal: str,
    *,
    action_type: ActionType | None = None,
    room: str | None = None,
    device_type: DeviceType | None = None,
    parameters: dict[str, Any] | None = None,
) -> list[ResolvedAction]:
    """Resolve a goal into concrete actions."""
```

Resolution strategy (in order):
1. If `action_type` is provided → `registry.find_by_action(action_type, room)`.
2. Else if `device_type` is provided → `registry.find_by_type(device_type, room)`.
3. Else → call `_llm_resolve(goal, room)` for disambiguation.

For each matched device, build a `ResolvedAction` with protocol-specific parameters mapped via `Capability.parameter_mapping`.

### 4.2 Parameter Mapping

```python
def _map_parameters(
    self,
    user_params: dict[str, Any],
    mapping: dict[str, str],
) -> dict[str, Any]:
    """Translate user-facing parameter names to protocol-specific names.

    Example: {"brightness": 80} with mapping {"brightness": "brightness_pct"}
    → {"brightness_pct": 80}
    """
```

### 4.3 LLM Disambiguation

Used only when no explicit `action_type` or `device_type` is given:

```python
async def _llm_resolve(
    self,
    goal: str,
    room: str | None = None,
) -> list[DeviceCapabilities]:
    """Use a lightweight LLM call to resolve ambiguous goals.

    This is a SMALL call (~500 tokens) for disambiguation only.
    Uses structured output with a Pydantic response model.
    """
```

Response model:

```python
class DisambiguationResponse(BaseModel):
    device_ids: list[str]
    action_type: ActionType | None = None
```

The LLM receives a compact summary of available devices (from `registry.to_compact_summary()`) and the user's goal. It returns which device IDs to target.

---

## 5. Edge Cases

- **No matching devices:** Return empty list, log a warning.
- **LLM returns unknown device IDs:** Filter them out silently.
- **LLM call fails:** Fall back to empty list with a warning, do not crash.
- **Multiple capabilities per device match:** Prefer the most specific (e.g., `SET_BRIGHTNESS` over `TURN_ON` if goal mentions brightness).

---

## 6. Tests

```python
# tests/actuator/test_mapper.py

def test_resolve_with_explicit_action_type():
    """action_type=TURN_OFF → resolves to all devices with TURN_OFF capability."""

def test_resolve_with_device_type():
    """device_type=LIGHT → resolves to all lights."""

def test_resolve_with_room_filter():
    """room='bedroom' → only bedroom devices returned."""

def test_parameter_mapping():
    """User params are translated via capability.parameter_mapping."""

async def test_llm_disambiguation():
    """When no action_type given, LLM is called and returns device_ids."""

async def test_llm_failure_returns_empty():
    """LLM error → empty list, no crash."""

def test_no_matching_devices():
    """Goal with no matching devices → empty list."""

def test_llm_returns_unknown_ids_filtered():
    """Unknown device IDs from LLM are silently excluded."""
```

---

## 7. Out of Scope

- Discovery logic (Phase 5B).
- Execution of resolved actions (Phase 5E).
- CLI or dashboard (Phase 5F).
