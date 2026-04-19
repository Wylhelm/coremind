# Phase 2 — Memory + Reasoning (L3 + L4)

**Duration:** ~1.5 weeks
**Prerequisite:** Phase 1 complete
**Deliverable:** The daemon remembers what it sees across time and reasons over world snapshots on a schedule, producing structured interpretations.

---

## Goals

- Three-kind memory system (episodic, semantic, procedural) operational.
- Qdrant integrated for semantic memory via multilingual embeddings.
- Reasoning loop runs on a configurable cadence, produces structured output.
- LiteLLM integration routes calls to any configured model.
- Reasoning outputs persist and are queryable.
- Two real plugins: `homeassistant` and `gmail-imap`.

---

## Deliverables Checklist

- [ ] `src/coremind/memory/episodic.py` — episodic memory over the event store
- [ ] `src/coremind/memory/semantic.py` — Qdrant-backed semantic store
- [ ] `src/coremind/memory/procedural.py` — versioned rule store
- [ ] `src/coremind/memory/embeddings.py` — embedding provider (multilingual-e5)
- [ ] `src/coremind/reasoning/llm.py` — LiteLLM wrapper with structured outputs
- [ ] `src/coremind/reasoning/loop.py` — scheduled reasoning cycle
- [ ] `src/coremind/reasoning/prompts.py` — prompt templates
- [ ] `src/coremind/reasoning/schemas.py` — Pydantic output schemas
- [ ] `plugins/homeassistant/` — real HA integration
- [ ] `plugins/gmail-imap/` — IMAP-based Gmail (not Google API, to stay agnostic)
- [ ] `tests/memory/*`, `tests/reasoning/*`
- [ ] CLI additions: `coremind memory search`, `coremind reason now`, `coremind reason list`

---

## Tasks for the Coding Agent

### 2.1 Episodic Memory

**File:** `src/coremind/memory/episodic.py`

Episodic memory is a **view** over the event store (no extra persistence), plus a compaction step.

```python
class EpisodicMemory:
    def __init__(self, store: WorldStore): ...

    async def recent(self, window: timedelta, entity: EntityRef | None = None) -> list[Episode]:
        """Return time-bucketed summaries of recent activity."""

    async def compact_older_than(self, age: timedelta) -> None:
        """Roll old raw events into Episode summaries (LLM-assisted)."""
```

Compaction runs once a day (nightly). It:
1. Pulls raw events older than the threshold
2. Groups by entity + day
3. Asks the reasoning LLM for a 1-paragraph summary per group
4. Stores the summary as a new entity of type `episode`
5. Optionally deletes the raw events (configurable: keep or delete)

### 2.2 Semantic Memory (Qdrant)

**File:** `src/coremind/memory/semantic.py`

```python
class SemanticMemory:
    def __init__(self, qdrant: QdrantClient, embedder: Embedder): ...

    async def remember(self, text: str, tags: list[str], metadata: dict) -> str:
        """Embed and store. Returns memory_id."""

    async def recall(self, query: str, k: int = 10, tags: list[str] | None = None) -> list[Memory]: ...

    async def forget(self, memory_id: str, reason: str) -> None:
        """Signed forgetting — logs to audit."""
```

Collections:
- `semantic_facts` — stable facts about entities
- `semantic_preferences` — user preferences learned over time
- `semantic_documents` — ingested text chunks from plugins (emails, notes, …)

### 2.3 Procedural Memory

**File:** `src/coremind/memory/procedural.py`

Versioned rule store as JSONL with hash-chaining (reuses audit log primitives).

```python
class Rule(BaseModel):
    id: str
    created_at: datetime
    description: str
    trigger: dict           # structured condition (DSL to be designed here)
    action: dict            # action proposal
    confidence: float       # tracks whether this rule has proven itself
    applied_count: int
    success_rate: float
    source: Literal["human", "reflection"]

class ProceduralMemory:
    async def add(self, rule: Rule) -> None: ...
    async def match(self, context: dict) -> list[Rule]: ...
    async def reinforce(self, rule_id: str, success: bool) -> None: ...
    async def deprecate(self, rule_id: str, reason: str) -> None: ...
```

### 2.4 Embeddings

**File:** `src/coremind/memory/embeddings.py`

- Use `sentence-transformers` with `intfloat/multilingual-e5-small` as default.
- Runnable locally, no cloud dependency.
- Configurable: users can switch to Ollama's embedding API or OpenAI's `text-embedding-3`.

### 2.5 LLM Router

**File:** `src/coremind/reasoning/llm.py`

Wrapper around LiteLLM with:
- Structured output enforcement via Pydantic models
- Retry-on-malformed (up to 2 retries)
- Token budget enforcement per call
- Per-layer model configuration

```python
class LLM:
    def __init__(self, config: LLMConfig): ...

    async def complete_structured[T: BaseModel](
        self,
        layer: Literal["reasoning_heavy", "reasoning_fast", "intention", "reflection"],
        system: str,
        user: str,
        response_model: type[T],
        max_tokens: int | None = None,
    ) -> T: ...
```

Config example (`~/.coremind/config.toml`):
```toml
[llm.models]
reasoning_heavy = "openai/gpt-4o"           # or "ollama/glm-5.1" or "anthropic/claude-opus-4-7"
reasoning_fast  = "ollama/llama3.3:8b"
intention       = "anthropic/claude-opus-4-7"
reflection      = "anthropic/claude-opus-4-7"
```

### 2.6 Reasoning Loop

**File:** `src/coremind/reasoning/loop.py`

Scheduled task running every N minutes (configurable, default 15). Triggered also by "significant event" heuristics (configurable delta thresholds).

Cycle:
1. Collect the snapshot from L2.
2. Pull relevant memory excerpts from L3 (entities present in snapshot → semantic facts about them).
3. Build the prompt using a template from `prompts.py`.
4. Call the LLM with the structured response model.
5. Persist the result as a `reasoning_cycle` entity in L2 + write an audit entry.

### 2.7 Reasoning Output Schema

**File:** `src/coremind/reasoning/schemas.py`

```python
class Pattern(BaseModel):
    id: str
    description: str
    entities_involved: list[EntityRef]
    confidence: float
    evidence: list[str]

class Anomaly(BaseModel):
    id: str
    description: str
    entity: EntityRef
    severity: Literal["low", "medium", "high"]
    baseline_description: str

class Prediction(BaseModel):
    id: str
    hypothesis: str
    horizon_hours: int
    confidence: float
    falsifiable_by: str  # how we'll know if it was right

class ReasoningOutput(BaseModel):
    cycle_id: str
    timestamp: datetime
    model_used: str
    patterns: list[Pattern]
    anomalies: list[Anomaly]
    predictions: list[Prediction]
    token_usage: TokenUsage
```

### 2.8 Prompt Templates

**File:** `src/coremind/reasoning/prompts.py`

Every prompt is versioned and stored in a template. At minimum:
- `reasoning.heavy.system.v1.md` — the persona/framing for the heavy cycle
- `reasoning.heavy.user.v1.md` — the user message template with placeholders
- Same for `reasoning.fast.*`

Templates are Jinja2. They include the snapshot, memory excerpt, and an explicit instruction to produce valid JSON matching the response schema.

### 2.9 Plugin: Home Assistant

**Directory:** `plugins/homeassistant/`

- Python plugin.
- Connects to HA via its WebSocket API.
- Subscribes to state change events.
- Maps HA entities → CoreMind entities.
- Emits WorldEvents for motion, temperature, humidity, light states, etc.

Config via `plugins/homeassistant/config.toml`:
```toml
[homeassistant]
base_url = "http://localhost:8123"
access_token_ref = "secrets:ha_token"
entity_prefixes = ["sensor.", "light.", "binary_sensor."]
```

### 2.10 Plugin: Gmail (IMAP)

**Directory:** `plugins/gmail-imap/`

- Python plugin using `imap_tools` or `aioimaplib`.
- Pulls new messages (no polling burn — uses IMAP IDLE).
- Emits a `WorldEvent` per new message with entity type `email` and attributes: `subject`, `sender`, `has_attachment`, `importance_hint`.
- Does not store full bodies in L2. Bodies go into L3 semantic memory, indexed by `email_id`.

**Why IMAP not Google API:** to stay provider-agnostic. Works with ProtonMail, Fastmail, iCloud, anything.

### 2.11 CLI additions

```
coremind memory search "..."
coremind memory tags list
coremind memory forget <id>
coremind reason now             # trigger a cycle immediately
coremind reason list --last 24h
coremind reason show <cycle_id> # full cycle with patterns, anomalies, predictions
```

### 2.12 Tests

- Semantic memory: remember/recall round trip, tag filtering, forgetting logs to audit
- Procedural memory: rule match evaluation, reinforcement updates success_rate correctly
- LLM wrapper: structured output validation, retry-on-malformed, budget enforcement
- Reasoning loop: with a mock LLM, a cycle produces a valid `ReasoningOutput` and persists
- End-to-end: start daemon + systemstats + a mocked HA plugin → trigger a cycle → assert patterns.length ≥ 0 without error

---

## Success Criteria

1. Daemon runs a reasoning cycle every 15 minutes automatically.
2. Memory is populated: at least 50 semantic memories after a day of running with HA + Gmail.
3. `coremind reason show <id>` returns a well-formed cycle for any executed cycle.
4. Switching the `reasoning_heavy` model in config and restarting works without code change.
5. All LLM calls respect token budgets and log their usage.
6. Compaction runs nightly and produces `episode` entities without deleting raw events (configurable).

---

## Explicitly Out of Scope

- Intention (L5) — no self-prompting yet
- Action (L6) — reasoning produces no side effects yet
- Reflection (L7)

---

## Handoff to Phase 3

Phase 3 begins with:
- A system that sees, remembers, and interprets — but is still passive.
- A stable reasoning pipeline that produces structured hypotheses.
- Two real plugins with proven integration.

**Next:** [`PHASE_3_INTENTION_ACTION.md`](PHASE_3_INTENTION_ACTION.md)
