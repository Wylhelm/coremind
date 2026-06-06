# Phase 6E — Calendar & Health Collectors

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_6_SELF_MODEL.md](PHASE_6_SELF_MODEL.md)
**Prerequisites:** Phase 6A complete
**Estimated effort:** 2–3 hours

---

## 1. Goal

Detect life routines from calendar, health, and presence data. After this sub-phase:

- Calendar collector aggregates GOG calendar events into appointment patterns and time commitments.
- Health collector aggregates health plugin data into sleep/exercise routines.
- Presence collector uses Tapo camera presence data for home/away patterns.
- All read existing L2 WorldEvents — no new external APIs needed.

---

## 2. Deliverables

| File | Purpose |
| ---- | ------- |
| `src/coremind/self_model/collectors/calendar.py` | Calendar pattern collector. |
| `src/coremind/self_model/collectors/health.py` | Health routine collector. |
| `src/coremind/self_model/collectors/presence.py` | Presence pattern collector. |
| `tests/self_model/collectors/test_calendar.py` | Tests. |
| `tests/self_model/collectors/test_health.py` | Tests. |
| `tests/self_model/collectors/test_presence.py` | Tests. |

---

## 3. Tasks for the Coding Agent

### 6E.1 Calendar Collector

**File:** `src/coremind/self_model/collectors/calendar.py`

```python
class CalendarCollector:
    """Detects scheduling patterns from calendar events in L2.

    Reads GOG plugin calendar events and identifies:
    - Recurring events (weekly meetings, regular appointments).
    - Time commitment patterns (busy mornings, free evenings).
    - Social appointments (meetings with specific people).
    """

    source_id: str = "calendar"
    category: str = "activity"

    async def collect(self, since: datetime) -> Sequence[RawObservation]:
        """Returns observations like:
        - {"pattern": "recurring", "event": "standup", "day": "mon-fri", "time": "09:00"}
        - {"pattern": "social", "contact": "Jeff", "frequency": "weekly"}
        - {"pattern": "commitment", "period": "morning", "busy_pct": 0.8}
        """
```

### 6E.2 Health Collector

**File:** `src/coremind/self_model/collectors/health.py`

```python
class HealthCollector:
    """Detects health routines from Apple Health data in L2.

    Aggregates sleep, steps, and heart rate data into patterns.
    Uses rolling averages (7-day window) for stability.
    """

    source_id: str = "health"
    category: str = "health"

    async def collect(self, since: datetime) -> Sequence[RawObservation]:
        """Returns observations like:
        - {"metric": "sleep", "avg_bedtime": "23:30", "avg_wake": "07:00", "avg_hours": 7.5}
        - {"metric": "steps", "avg_daily": 6500, "trend": "stable"}
        - {"metric": "heart_rate", "resting_avg": 62, "trend": "decreasing"}
        """
```

Implementation:
- Query L2 for `person:guillaume` events with health-related attributes.
- Compute 7-day rolling averages for bedtime, wake time, step count.
- Detect trends (increasing/decreasing/stable) over the window.

### 6E.3 Presence Collector

**File:** `src/coremind/self_model/collectors/presence.py`

```python
class PresenceCollector:
    """Detects home/away patterns from Tapo camera presence data.

    Uses motion detection events to infer:
    - Typical departure/arrival times.
    - Days spent at home vs away.
    - Room usage patterns.
    """

    source_id: str = "presence"
    category: str = "activity"

    async def collect(self, since: datetime) -> Sequence[RawObservation]:
        """Returns observations like:
        - {"pattern": "departure", "avg_time": "08:30", "days": ["mon", "tue", "wed", "thu", "fri"]}
        - {"pattern": "arrival", "avg_time": "18:00"}
        - {"pattern": "room_usage", "room": "bureau", "hours_per_day": 6.5}
        """
```

---

## 4. Emitted Entity Types (after extraction)

| Entity | Attributes |
| ------ | ---------- |
| `routine:sleep` | `avg_bedtime`, `avg_wake`, `avg_hours`, `quality_trend` |
| `routine:exercise` | `avg_daily_steps`, `active_days`, `trend` |
| `routine:presence` | `departure_time`, `arrival_time`, `room_preferences` |
| `person:*` | `meeting_frequency` (from calendar with specific contacts) |

---

## 5. Success Criteria

1. Calendar collector identifies recurring events from 2+ weeks of mocked data.
2. Health collector computes 7-day rolling averages correctly.
3. Presence collector detects weekday departure/arrival patterns.
4. All collectors handle empty data gracefully (return empty list).
5. Tests pass with mocked WorldStore queries.

---

## 6. Explicitly Out of Scope

- Direct API access to Apple Health, Google Calendar, or Tapo (uses existing L2 events).
- Health advice or medical recommendations.
- Precise geolocation tracking.
