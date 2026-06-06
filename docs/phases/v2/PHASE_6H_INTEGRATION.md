# Phase 6H — Integration Layer (Reasoning, Intention, Conversation)

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_6_SELF_MODEL.md](PHASE_6_SELF_MODEL.md)
**Prerequisites:** Phase 6A + 6B + at least one collector (6C–6F) complete
**Estimated effort:** 4–5 hours

---

## 1. Goal

Wire the Self-Model into the core cognitive loop so it influences reasoning, intention generation, and conversation quality. After this sub-phase:

- `SelfModelProvider` exposes a clean read API for other layers.
- Reasoning prompts include relevant self-model context (replaces `{{ about_user }}`).
- Intention generation is goal-aware and project-aware.
- Conversation prompts provide rich personal context.
- `NarrativeMemory` is migrated and deprecated.

---

## 2. Deliverables

| File | Purpose |
| ---- | ------- |
| `src/coremind/self_model/provider.py` | `SelfModelProvider` — read API for other layers. |
| `src/coremind/reasoning/prompts.py` | **Modify:** inject self-model context. |
| `src/coremind/intention/prompts.py` | **Modify:** goal/project awareness. |
| `src/coremind/conversation/prompts.py` | **Modify:** replace `{{ about_user }}`. |
| `src/coremind/reasoning/loop.py` | **Modify:** pass provider to prompt builder. |
| `src/coremind/memory/narrative.py` | **Modify:** deprecate, add migration method. |
| `tests/self_model/test_provider.py` | Provider tests. |
| `tests/self_model/test_integration.py` | End-to-end integration tests. |

---

## 3. Tasks for the Coding Agent

### 6H.1 Self-Model Provider

**File:** `src/coremind/self_model/provider.py`

```python
class SelfModelProvider:
    """Read API exposing the self-model to other cognitive layers.

    Generates truncated context strings suitable for prompt injection,
    respecting the configured max_context_tokens budget.
    """

    def __init__(self, store: SelfModelStore, config: SelfModelConfig) -> None: ...

    async def get_context_for_reasoning(self) -> str:
        """Generate self-model context for the reasoning loop prompt.

        Returns a structured summary including:
        - Active projects (name, phase, status, intensity).
        - Current routines (time windows, frequencies).
        - Active goals (description, progress, deadline proximity).

        Priority: declared > recent observed > stale observed > synthesized.
        Truncated to fit max_context_tokens.
        """

    async def get_context_for_conversation(
        self, topic_entities: Sequence[EntityRef] | None = None
    ) -> str:
        """Generate self-model context relevant to a conversation topic.

        If topic_entities are provided, prioritize facts about those entities.
        Otherwise, provide a general personal context summary.
        """

    async def get_context_for_intention(self) -> str:
        """Generate self-model context for intention generation.

        Focuses on:
        - Goals with upcoming deadlines or stalled progress.
        - Projects with high intensity or recent activity.
        - Routines that are being violated (e.g. late coding session).
        """

    async def get_relationships(self) -> Sequence[PersonEntity]:
        """Return all known person entities."""

    async def get_active_goals(self) -> Sequence[GoalEntity]:
        """Return active goals sorted by deadline proximity."""

    async def get_active_projects(self) -> Sequence[ProjectEntity]:
        """Return active projects sorted by last activity."""

    async def get_routines(self) -> Sequence[RoutineEntity]:
        """Return all detected routines."""

    async def get_pending_questions(self, limit: int = 3) -> Sequence[SelfFact]:
        """Return L4 (questioned) facts suitable for conversational asking."""
```

### 6H.2 Reasoning Prompt Integration

**File:** `src/coremind/reasoning/prompts.py` (modify)

Replace the static `{{ about_user }}` placeholder with dynamic self-model context:

```python
# Before:
# user_context = personalization.about_user or ""

# After:
user_context = await self_model_provider.get_context_for_reasoning()
```

The self-model context block in the reasoning prompt should look like:

```text
## About the user
- Active projects: CoreMind (phase 6, high intensity), G-Bot Immo (paused, 45 days inactive)
- Goals: Retirement by 2043 (12% progress), Health improvement (ongoing)
- Routines: Coding 20:00-00:00 weekdays, Sleep avg 23:30-07:00
- Key relationships: Aurélie (fille, last contact 5 days ago), Jeff (ami, weekly)
```

### 6H.3 Intention Prompt Integration

**File:** `src/coremind/intention/prompts.py` (modify)

Add self-model context to intention generation:

```text
## User goals and projects (for goal-aware self-prompting)
{{ self_model_intention_context }}
```

This enables the intention layer to generate questions like:
- "Phase 6 is at 60% and you said 'done this week' — on track?"
- "No contact with Aurélie in 5 days — her birthday is in 2 weeks."

### 6H.4 Conversation Prompt Integration

**File:** `src/coremind/conversation/prompts.py` (modify)

Replace `{{ about_user }}` with rich context:

```python
# Before:
about_user = personalization.greeting_name or personalization.user_name

# After:
about_user = await self_model_provider.get_context_for_conversation(topic_entities)
```

### 6H.5 NarrativeMemory Migration

**File:** `src/coremind/memory/narrative.py` (modify)

Add a migration method and deprecation notice:

```python
class NarrativeMemory:
    """DEPRECATED: Use SelfModelProvider instead.

    This class will be removed in v1.1.0. Migration path:
    - recent_patterns → routine:* entities in self-model
    - active_concerns → goal:*/project:* entities
    - relationship_notes → person:* entities
    - user_mood_trend → dropped (too vague for structured model)
    """

    async def migrate_to_self_model(self, store: SelfModelStore) -> int:
        """Migrate NarrativeState data to self-model facts.

        Returns number of facts created.
        Idempotent: skips facts that already exist in the store.
        """
```

Migration mapping:
- `recent_patterns` → parse each into `routine:*` facts (method="observed", confidence=0.7)
- `active_concerns` → parse into `goal:*` or `project:*` facts (method="observed", confidence=0.7)
- `relationship_notes` → parse into `person:*` facts (method="declared", confidence=0.9)
- `user_mood_trend` → drop (no equivalent in structured model)

---

## 4. Context Generation Strategy

### Priority ordering (highest first)

1. Declared facts (confidence ≥ 0.95)
2. Recently observed facts (updated < 7 days ago)
3. Older observed facts (updated ≥ 7 days ago)
4. Synthesized facts (confidence 0.5–0.7)

### Token budget enforcement

- `max_context_tokens` from config (default 2000).
- Use `tiktoken` or character-based approximation (4 chars ≈ 1 token).
- Start with highest-priority facts, add until budget exhausted.
- Each fact renders as one line: `- {entity_type}:{entity_id}.{attribute} = {value}`.

### Caching

- Context is regenerated at most once per reasoning cycle.
- Cache TTL: 5 minutes (configurable).
- Cache key: provider method name + argument hash.

---

## 5. Success Criteria

1. `SelfModelProvider.get_context_for_reasoning()` returns non-empty string when facts exist.
2. Context respects `max_context_tokens` budget.
3. Priority ordering: declared facts appear before synthesized in output.
4. Reasoning prompt includes self-model context (verified in integration test with mocked LLM).
5. `NarrativeMemory.migrate_to_self_model()` creates correct facts from sample NarrativeState.
6. After migration, NarrativeMemory methods log deprecation warnings.
7. End-to-end: daemon with self-model → reasoning cycle → output references user goals/projects.

---

## 6. Explicitly Out of Scope

- CLI commands for inspecting integration (6I).
- Dashboard visualization (6I).
- Real-time prompt streaming (context is pre-generated).
