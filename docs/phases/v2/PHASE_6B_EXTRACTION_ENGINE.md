# Phase 6B — Extraction Engine (LLM Fact Pipeline)

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_6_SELF_MODEL.md](PHASE_6_SELF_MODEL.md)
**Prerequisites:** Phase 6A complete
**Estimated effort:** 3–4 hours

---

## 1. Goal

Build the LLM-powered extraction pipeline that transforms raw data (WorldEvents, metadata) into structured `SelfFact` proposals at appropriate confidence levels. After this sub-phase:

- `SelfModelExtractor` takes batched source data and produces fact proposals via `LLM.complete_structured()`.
- Confidence scoring and weekly decay logic is implemented.
- Jinja2 prompt templates guide extraction per source category.
- Deduplication prevents redundant facts from flooding the store.
- Rate limiting caps facts per cycle.
- Golden tests validate extraction output schemas.

---

## 2. Deliverables

| File | Purpose |
| ---- | ------- |
| `src/coremind/self_model/extractor.py` | Main extraction engine. |
| `src/coremind/self_model/confidence.py` | Confidence scoring, decay logic, threshold enforcement. |
| `src/coremind/self_model/prompts/extract_from_events.jinja2` | General event → fact prompt. |
| `src/coremind/self_model/prompts/extract_from_communication.jinja2` | Message metadata → person/social facts. |
| `src/coremind/self_model/prompts/extract_from_activity.jinja2` | Coding/health activity → routine/project facts. |
| `src/coremind/self_model/prompts/synthesize_cross_source.jinja2` | Multi-signal → higher-level inference. |
| `tests/self_model/test_extractor.py` | Extraction tests with mocked LLM + golden fixtures. |
| `tests/self_model/test_confidence.py` | Scoring rules, decay, threshold tests. |

---

## 3. Tasks for the Coding Agent

### 6B.1 Confidence Module

**File:** `src/coremind/self_model/confidence.py`

```python
from coremind.self_model.config import SelfModelConfig
from coremind.self_model.entities import ConfidenceMethod

class ConfidenceScorer:
    """Validates and adjusts confidence values based on method and config."""

    def __init__(self, config: SelfModelConfig) -> None: ...

    def validate(self, confidence: float, method: ConfidenceMethod) -> float:
        """Ensure confidence meets minimum threshold for its method.

        Returns the clamped value or raises ConfidenceError.
        """

    def apply_decay(self, current: float, days_stale: int) -> float:
        """Calculate decayed confidence for stale facts.

        Returns new confidence. Caller decides to deactivate if below 0.3.
        """

    def should_supersede(self, existing: float, proposed: float) -> bool:
        """Return True if proposed confidence justifies superseding existing."""
```

### 6B.2 Extraction Response Schema

**File:** `src/coremind/self_model/extractor.py` (partial — the Pydantic response model)

```python
class ExtractedFact(BaseModel):
    """Single fact extracted by the LLM from source data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_type: SelfModelEntityType
    entity_id: str
    attribute: str
    value: JsonValue
    confidence: float = Field(ge=0.0, le=1.0)
    method: ConfidenceMethod
    reasoning: str  # Why the LLM inferred this
    evidence_refs: list[str]  # Event IDs used as evidence


class ExtractionResult(BaseModel):
    """Structured response from the extraction LLM call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: list[ExtractedFact] = Field(max_length=20)
    skipped_reason: str | None = None  # If extraction found nothing notable
```

### 6B.3 Extractor Engine

**File:** `src/coremind/self_model/extractor.py`

```python
class SelfModelExtractor:
    """Transforms raw observations into structured self-model facts.

    Uses LLM.complete_structured() with the 'reasoning_fast' layer
    to produce ExtractionResult from batched source data.
    """

    def __init__(
        self,
        llm: LLM,
        store: SelfModelStore,
        scorer: ConfidenceScorer,
        config: SelfModelConfig,
    ) -> None: ...

    async def extract_from_events(
        self,
        events: Sequence[WorldEventRecord],
        source_category: str,
    ) -> list[SelfFact]:
        """Run extraction on a batch of events.

        1. Select appropriate prompt template based on source_category.
        2. Build prompt with event data + existing self-model context.
        3. Call LLM.complete_structured(response_model=ExtractionResult).
        4. Validate confidence via scorer.
        5. Deduplicate against store (skip if existing fact has higher confidence).
        6. Rate-limit to max_facts_per_cycle.
        7. Persist accepted facts via store.upsert_fact().
        8. Return the list of persisted facts.
        """

    async def synthesize(self, entity_type: SelfModelEntityType | None = None) -> list[SelfFact]:
        """Cross-source synthesis — infer higher-level facts from existing facts.

        Uses synthesize_cross_source template.
        Produces method='synthesized' facts at lower confidence.
        """
```

### 6B.4 Prompt Templates

Templates use Jinja2 and are stored under `src/coremind/self_model/prompts/`.

**`extract_from_events.jinja2`:**

```jinja2
You are a personal knowledge extractor. Given recent observations from {{ source_category }},
identify facts about the user that should be remembered.

## Existing knowledge (for context, do not repeat unchanged facts):
{% for fact in existing_facts %}
- {{ fact.entity_type }}:{{ fact.entity_id }}.{{ fact.attribute }} = {{ fact.value }} (confidence={{ fact.confidence }})
{% endfor %}

## Recent observations:
{% for event in events %}
- [{{ event.timestamp }}] {{ event.entity.type }}:{{ event.entity.id }}.{{ event.attribute }} = {{ event.value }}
{% endfor %}

## Instructions:
- Extract ONLY new or updated facts about the user (person, goals, projects, routines, identity, preferences).
- Each fact must have concrete evidence from the observations above.
- Set confidence appropriately: 0.7-0.9 for clear patterns, 0.5-0.7 for inferences.
- Do NOT extract facts about the environment (temperature, lights, etc.) — only about the user.
- If nothing notable is found, set facts to empty array and explain in skipped_reason.
```

**`extract_from_communication.jinja2`:**

```jinja2
You are a personal knowledge extractor. Given message metadata (NOT content),
identify facts about the user's relationships and social patterns.

## Existing relationships:
{% for person in known_people %}
- person:{{ person.entity_id }} ({{ person.relationship }}, last_contact={{ person.last_contact }})
{% endfor %}

## Recent communication metadata:
{% for msg in messages %}
- [{{ msg.timestamp }}] {{ msg.direction }} {{ msg.contact }} via {{ msg.channel }}
{% endfor %}

## Instructions:
- Update last_contact for known people.
- Detect new relationships (unknown contacts with repeated interaction).
- Detect changes in contact frequency.
- Do NOT infer emotional content — you only see metadata.
- Set confidence 0.8-0.9 for frequency patterns, 0.7-0.8 for new relationship detection.
```

**`extract_from_activity.jinja2`:**

```jinja2
You are a personal knowledge extractor. Given development/health activity data,
identify the user's routines, project status, and behavioral patterns.

## Known routines:
{% for routine in known_routines %}
- routine:{{ routine.entity_id }} (window={{ routine.time_window }}, frequency={{ routine.frequency }})
{% endfor %}

## Known projects:
{% for project in known_projects %}
- project:{{ project.entity_id }} (status={{ project.status }}, phase={{ project.current_phase }})
{% endfor %}

## Recent activity:
{% for event in events %}
- [{{ event.timestamp }}] {{ event.attribute }} = {{ event.value }}
{% endfor %}

## Instructions:
- Detect time-of-day patterns (routine windows).
- Track project activity levels (commits, active hours).
- Identify intensity changes (more/less active than usual).
- Confidence: 0.7-0.85 for patterns with 5+ data points, 0.5-0.7 for emerging patterns.
```

**`synthesize_cross_source.jinja2`:**

```jinja2
You are a personal knowledge synthesizer. Given the user's current self-model,
identify higher-level insights by combining facts across sources.

## Current self-model:
{% for fact in all_facts %}
- {{ fact.entity_type }}:{{ fact.entity_id }}.{{ fact.attribute }} = {{ fact.value }} (method={{ fact.method }}, conf={{ fact.confidence }})
{% endfor %}

## Instructions:
- Look for contradictions between goals and behavior (intent-vs-action gaps).
- Identify correlations between routines (e.g. late coding → poor sleep).
- Detect stalled projects or abandoned goals.
- Produce ONLY method='synthesized' facts with confidence 0.5-0.7.
- Each insight must cite ≥2 existing facts as evidence.
- Maximum 3 synthesized facts per run.
```

---

## 4. Success Criteria

1. `SelfModelExtractor.extract_from_events()` produces valid `SelfFact` objects from a golden fixture of 10 WorldEvents (mocked LLM).
2. Deduplication correctly skips facts that already exist at higher confidence.
3. Rate limiting caps output at `max_facts_per_cycle` even when LLM proposes more.
4. Confidence decay reduces stale facts by `decay_per_week` and deactivates below 0.3.
5. `ConfidenceScorer.validate()` rejects facts below the minimum threshold for their method.
6. All prompt templates render without errors given sample data.
7. Tests pass with `pytest tests/self_model/test_extractor.py tests/self_model/test_confidence.py -v`.

---

## 5. Explicitly Out of Scope

- Collector implementations (6C–6F provide the raw data).
- User-declared facts (6G).
- Integration with reasoning prompts (6H).
- Real LLM calls in tests — use mocked responses and schema validation.
