# Phase 6F — Finance & Media Collectors (Firefly, Immich)

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_6_SELF_MODEL.md](PHASE_6_SELF_MODEL.md)
**Prerequisites:** Phase 6A complete
**Estimated effort:** 2–3 hours

---

## 1. Goal

Understand financial patterns and social contexts from Firefly III and Immich photo metadata.

---

## 2. Deliverables

| File | Purpose |
| ---- | ------- |
| `src/coremind/self_model/collectors/firefly.py` | Spending pattern collector. |
| `src/coremind/self_model/collectors/immich.py` | Photo metadata collector. |
| `tests/self_model/collectors/test_firefly.py` | Tests. |
| `tests/self_model/collectors/test_immich.py` | Tests. |

---

## 3. Tasks for the Coding Agent

### 6F.1 Firefly Collector

**File:** `src/coremind/self_model/collectors/firefly.py`

```python
class FireflyCollector:
    """Extracts category-level spending patterns from Firefly III events in L2.

    NEVER exposes individual transaction details — only aggregate categories.
    """

    source_id: str = "firefly"
    category: str = "finance"

    async def collect(self, since: datetime) -> Sequence[RawObservation]:
        """Returns observations like:
        - {"category": "food", "monthly_total": 450, "trend": "stable", "currency": "CAD"}
        - {"category": "tech", "monthly_total": 200, "trend": "increasing"}
        - {"savings_rate_pct": 15.0, "vs_last_month": "+2.0"}
        """
```

**Privacy constraint:** Individual transactions (amounts, merchants, dates) NEVER appear in self-model. Only category aggregates and trends.

### 6F.2 Immich Collector

**File:** `src/coremind/self_model/collectors/immich.py`

```python
class ImmichCollector:
    """Extracts social graph and location data from Immich photo metadata.

    Uses face tags, GPS coordinates, and dates — never image content.
    Accesses Immich API directly (not L2 events, as no Immich plugin exists yet).
    """

    source_id: str = "immich"
    category: str = "media"

    def __init__(self, api_url: str, api_key_secret: str) -> None: ...

    async def collect(self, since: datetime) -> Sequence[RawObservation]:
        """Returns observations like:
        - {"people_seen": ["Aurélie", "Jeff"], "location": "Montréal", "date": "2026-05-20"}
        - {"people_seen": ["Aurélie"], "location": "Québec", "date": "2026-05-15"}
        """
```

**Privacy constraint:** Only face tag names + location + date. Never image analysis, never photo content.

---

## 4. Emitted Entity Types (after extraction)

| Entity | Attributes |
| ------ | ---------- |
| `preference:spending` | Top categories, monthly patterns |
| `goal:retirement` | `savings_rate_pct`, progress indicators |
| `person:*` | Reinforced by photo co-occurrence |
| `routine:social` | Physical meeting frequency from photos |

---

## 5. Success Criteria

1. Firefly collector produces category aggregates, never individual transactions.
2. Immich collector extracts face tags and locations from API response.
3. Both handle empty/unavailable data gracefully.
4. Tests validate privacy constraints (no raw amounts, no image data).

---

## 6. Explicitly Out of Scope

- Image analysis or AI-powered photo categorization.
- Budget advice or financial recommendations.
- Creating an Immich plugin for L2 (collector accesses API directly for now).
