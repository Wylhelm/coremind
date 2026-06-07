# CoreMind v2 — Technical Architecture

**Status:** Design document for v2 implementation
**Audience:** Coding agents (Opus in VS Code), system architects, contributors
**Reference v1:** `~/.openclaw/workspace/coremind/docs/ARCHITECTURE.md`

---

## Table of Contents

1. [Overview & Core Principles](#1-overview--core-principles)
2. [The 9-Layer Architecture (L0–L8)](#2-the-9-layer-architecture-l0l8)
3. [Autonomy Slider System](#3-autonomy-slider-system)
4. [JEPA-Inspired Embedding World](#4-jepa-inspired-embedding-world)
5. [Auto-Investigation Loop](#5-auto-investigation-loop)
6. [Unified Actuator Surface](#6-unified-actuator-surface)
7. [Self-Improving Meta-Loop (L8)](#7-self-improving-meta-loop-l8)
8. [Data Model — Complete Schema Reference](#8-data-model--complete-schema-reference)
9. [Storage Architecture](#9-storage-architecture)
10. [API & CLI](#10-api--cli)
11. [Security & Safety](#11-security--safety)
12. [Deployment & Migration](#12-deployment--migration)

---

## 1. Overview & Core Principles

CoreMind v2 evolves the v1 7-layer cognitive architecture by adding:

- **L0 (Discovery):** Automatic detection of controllable devices and services
- **L8 (Meta):** A meta-cognition layer that observes and improves the system itself
- **JEPA-inspired embeddings:** Replace verbose JSON snapshots with compressed embedding-based context
- **Per-domain autonomy sliders:** Replace binary safe/suggest/ask with graduated float thresholds (0.0–1.0)
- **Auto-investigation loops:** Test hypotheses rather than re-asking the same questions
- **Unified actuator surface:** A single `act(goal, confidence)` API over all plugins

### Non-Negotiable Principles

1. **Sovereignty.** All data stays local. No external LLM calls reveal personal data unless the user explicitly enables a cloud model.
2. **Reversibility.** Every action can be undone or reverted. The meta-loop can never make irreversible changes.
3. **Safety.** Hardcoded boundaries prevent the meta-loop from disabling safeguards. Approval gates remain for finance, security, and external communication.
4. **Transparency.** The user can always see why the system made a decision. Every adjustment is logged with a reason.
5. **Backward compatibility.** v1 features remain functional during and after migration. No breaking schema changes without migration scripts.

---

## 2. The 9-Layer Architecture (L0–L8)

### Architecture Diagram

```
┌────────────────────────────────────────────────────────────────────┐
│  L8 — META (NEW)                                                    │
│       Observes L2–L7. Adjusts parameters. Proposes promotions.      │
│       Cannot violate hard safety boundaries.                        │
└──────────────────────────────▲─────────────────────────────────────┘
                                │ feedback loop
┌──────────────────────────────┴─────────────────────────────────────┐
│  L7 — REFLECTION                                                    │
│       Periodic narrative synthesis. "What did I do? What worked?"   │
│       Writes reflection reports for the user.                       │
└──────────────────────────────▲─────────────────────────────────────┘
                                │
┌──────────────────────────────┴─────────────────────────────────────┐
│  L6 — ACTION                                                        │
│       Plans → Executes → Audits actions through the executor.       │
│       NEW: Uses autonomy slider instead of binary category.         │
└──────────────────────────────▲─────────────────────────────────────┘
                                │
┌──────────────────────────────┴─────────────────────────────────────┐
│  L5 — INTENTION                                                     │
│       Generates intents from world state. Routes to L6 or skip.     │
│       NEW: Receives compressed embedding-based prompts.             │
└──────────────────────────────▲─────────────────────────────────────┘
                                │
┌──────────────────────────────┴─────────────────────────────────────┐
│  L4 — REASONING                                                     │
│       Anomaly detection, hypothesis formation, summarization.       │
│       NEW: Embedding-based context. Triggers auto-investigations.   │
└──────────────────────────────▲─────────────────────────────────────┘
                                │
┌──────────────────────────────┴─────────────────────────────────────┐
│  L3 — MEMORY                                                        │
│       Persistent narrative state, working memory, history.          │
│       NEW: Stores snapshot embeddings for similarity queries.       │
└──────────────────────────────▲─────────────────────────────────────┘
                                │
┌──────────────────────────────┴─────────────────────────────────────┐
│  L2 — WORLD MODEL                                                   │
│       Aggregates entity states from all plugins into snapshots.     │
│       NEW: Computes embeddings + diffs for compressed context.      │
└──────────────────────────────▲─────────────────────────────────────┘
                                │
┌──────────────────────────────┴─────────────────────────────────────┐
│  L1 — PERCEPTION                                                    │
│       Pulls data from plugins on schedule. Routes events.           │
└──────────────────────────────▲─────────────────────────────────────┘
                                │
┌──────────────────────────────┴─────────────────────────────────────┐
│  L0 — DISCOVERY (NEW)                                               │
│       mDNS, HA scan, plugin manifest. Builds capability registry.   │
└────────────────────────────────────────────────────────────────────┘
```

### 2.1 L0 — Discovery (NEW)

**Responsibility:** Automatically detect controllable devices and services on the local network and through registered plugins.

**Inputs:**
- mDNS service announcements (`_hap._tcp`, `_sonos._tcp`, `_googlecast._tcp`, etc.)
- Home Assistant entity registry (`GET /api/states`)
- Plugin gRPC `Identify()` calls returning their action capabilities

**Outputs:**
- `DeviceCapabilities` records stored in the capability registry
- Discovery events published to the event bus

**Key Abstractions:**
- `DiscoveryEngine` — orchestrates all discovery methods
- `MDNSScanner` — listens for mDNS announcements via `zeroconf` library
- `HADiscoverer` — calls HA REST API to enumerate entities
- `PluginManifestDiscoverer` — calls plugin `Identify()` to enumerate capabilities
- `CapabilityRegistry` — stores and indexes discovered devices

**Schedule:** Runs on daemon startup + every 6 hours.

**See:** `PHASE_5_UNIFIED_ACTUATOR.md` for full implementation details.

### 2.2 L1 — Perception (Unchanged)

**Responsibility:** Pull data from plugins on schedule and emit perception events.

**v2 changes:** None to the layer itself; plugins benefit from discovery (L0) but the perception loop is unchanged.

### 2.3 L2 — World Model (Enhanced)

**v1 behavior:** Aggregates all entity states into a `WorldSnapshot` JSON object. Sends this snapshot to L4/L5 as prompt context (typically 10K–30K tokens).

**v2 enhancements:**
1. **Embedding computation.** Each entity gets an embedding via `nomic-embed-text` (Ollama @ OLLAMA_HOST:11434).
2. **Snapshot diffing.** Compares current snapshot to previous, identifies added/removed/changed entities.
3. **Compressed prompts.** Generates a `CompressedPrompt` with only the diff + similarity context.

**New key abstractions:**
- `EmbeddingEncoder` — wraps Ollama, computes entity and snapshot embeddings
- `SnapshotDiffer` — computes diffs between snapshots
- `CompressedPromptBuilder` — builds compact prompts for L4/L5

**Token budget:** Target <3000 tokens per reasoning cycle (vs 15K–30K in v1).

**See:** `PHASE_3_EMBEDDING_WORLD.md`.

### 2.4 L3 — Memory (Enhanced)

**v1 behavior:** Stores narrative state in SurrealDB. Working memory for L4–L7.

**v2 enhancements:**
1. **Snapshot embedding storage.** New Qdrant collection `snapshot_embeddings` stores the last 1000 snapshot embeddings.
2. **Similarity queries.** "Find the 5 most similar past states to current." Enables "this pattern looks like last Tuesday at 6pm" reasoning.
3. **Investigation results storage.** New SurrealDB table `investigation_run` stores investigation lifecycles.

### 2.5 L4 — Reasoning (Enhanced)

**v1 behavior:** Periodically analyzes world state. Detects anomalies, summarizes patterns, generates hypotheses for L5.

**v2 enhancements:**
1. **Embedding-based context.** Receives compressed prompts from L2 instead of full snapshots.
2. **Auto-investigation triggers.** When L4 detects an anomaly, it can trigger a `InvestigationRun` via the investigation engine.
3. **Similarity-aware reasoning.** L4 receives top-K similar past states from L3 to leverage temporal patterns.

**See:** `PHASE_4_AUTO_INVESTIGATION.md`.

### 2.6 L5 — Intention (Enhanced)

**v1 behavior:** Converts L4 hypotheses into concrete intents. Routes intents to L6 (action) or discards low-salience ones.

**v2 enhancements:**
1. **Compressed input.** Receives compact context from L2/L4.
2. **Unified action routing.** Generates `Intent` objects that may target the unified actuator surface instead of specific plugins.

### 2.7 L6 — Action (Enhanced)

**v1 behavior:** Plans, executes, and audits actions. Uses hardcoded category logic (`get_forced_category()`) to determine SAFE/SUGGEST/ASK/CONVERSATION.

**v2 enhancements:**
1. **Autonomy slider.** Replaces hardcoded category logic with `resolve_agency(action_class, confidence, slider_config)`.
2. **Unified executor.** New `execute_resolved(ResolvedAction)` method for the unified actuator path.
3. **Domain-aware approval flow.** Approvals are tagged with their domain for L7/L8 to track approval rates.

**See:** `PHASE_1_AUTONOMY_SLIDER.md`.

### 2.8 L7 — Reflection (Unchanged in core, enhanced data)

**v1 behavior:** Periodic narrative synthesis. Writes reflection reports.

**v2 enhancements:**
1. **Richer telemetry.** L7 now has access to investigation outcomes, embedding similarity matches, and meta-loop adjustment history.
2. **Graduation proposals.** L7 can propose autonomy slider increases for domains with consistent approval rates.

### 2.9 L8 — Meta (NEW)

**Responsibility:** Observe L2–L7 performance and adjust system parameters within safety bounds.

**What it tracks:**
- Intent repetition rate
- Notification engagement rate
- Approval rate per domain
- Plugin health (errors, latency)
- Token efficiency (tokens per useful intent)
- Investigation success rate

**What it can adjust (within bounds):**
- Polling intervals (30s ≤ x ≤ 24h)
- Salience thresholds (0.2 ≤ x ≤ 0.7)
- Notification cooldowns (1min ≤ x ≤ 24h)
- Investigation priorities

**What it CANNOT adjust:**
- Approval class assignments (finance, security, messaging remain ASK)
- Quiet hours
- Secrets / credentials
- Plugin permissions
- Audit logging
- The meta-loop's own safety bounds

**See:** `PHASE_2_SELF_IMPROVEMENT.md`.

---

## 3. Autonomy Slider System

### 3.1 Conceptual Model

In v1, an action is categorized as one of: `safe` | `suggest` | `ask` | `conversation`. The category is a property of the action's class — independent of confidence or user trust.

In v2, the user assigns each domain a slider value in [0.0, 1.0] representing their trust in CoreMind for that domain. Combined with the action's confidence score, this produces the agency decision:

```python
def resolve_agency(action_class: str, confidence: float, config: AutonomyConfig) -> ActionCategory:
    # 1. Hard overrides
    if config.is_hard_ask(action_class):
        return "ask"
    if config.is_hard_safe(action_class):
        return "safe"
    
    # 2. Slider-based decision
    domain = classify_action(action_class)
    slider = config.get_slider(domain)  # 0.0 to 1.0
    
    if confidence >= slider:
        return "safe"          # auto-execute
    elif confidence >= slider * 0.6:
        return "suggest"       # notify + grace period
    else:
        return "ask"           # block and require approval
```

### 3.2 Default Per-Domain Sliders

| Domain | Default | Rationale |
|---|---|---|
| `weather` | 1.0 | Purely informational, always safe |
| `lights` | 0.8 | Low risk, easily reversible |
| `calendar` | 0.8 | Mostly read-only |
| `hvac` | 0.7 | Comfort, moderate energy cost |
| `media` | 0.7 | Entertainment, low risk |
| `presence` | 0.6 | User notifications |
| `notifications` | 0.6 | System notifications |
| `health` | 0.5 | Personal data, sensitive |
| `default` | 0.4 | Conservative fallback |
| `vacuum` | 0.3 | Noise/disturbance |
| `messaging` | 0.2 | External communication |
| `locks` | 0.1 | Security (also hard-ASK) |
| `finance` | 0.1 | Money (also hard-ASK) |

### 3.3 Hard Boundaries

Certain action classes are **always ASK** regardless of slider:
- `finance.transfer`, `finance.payment`
- `lock.unlock`, `garage_door.open`
- `messaging.send_external`, `email.send`
- `security.disable`, `plugin.install`, `config.modify`

These cannot be promoted by the meta-loop or by user action — they require explicit code changes to the `hard_ask` registry.

### 3.4 Graduation (Automatic Slider Promotion)

L7/L8 can propose slider increases when:
- The domain has ≥10 approvals in the observation window
- The approval rate is ≥80%
- It's been ≥7 days since the last promotion in this domain
- The proposed increase is ≤0.1

The user must approve the proposal before it takes effect. See `PHASE_1_AUTONOMY_SLIDER.md` for details.

---

## 4. JEPA-Inspired Embedding World

### 4.1 Background

Yann LeCun's **JEPA (Joint Embedding Predictive Architecture)** trains models to predict abstract representations of the world rather than generating raw outputs. The key insight: most cognitive work happens in embedding space, not in token space.

We adapt this insight pragmatically. We're not building JEPA; we're using its core idea — that **rich intermediary embeddings beat verbose text representations** — to dramatically reduce token usage and improve reasoning quality.

### 4.2 Why This Matters for CoreMind

CoreMind v1 sends full `WorldSnapshot` JSON to the LLM at every reasoning cycle. A typical snapshot in production:

```
Entities: 48
JSON size: 24,000 tokens
Truly changed since last cycle: 3
Useful changes for current reasoning: 1-2
```

This is 90%+ noise. The LLM has to skim through irrelevant data, and worse, it can hallucinate stale values back into responses.

### 4.3 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  L2 — World Model                                            │
│                                                              │
│  Current Snapshot ─┐                                         │
│                    │                                         │
│  Previous Snapshot─┼──▶  SnapshotDiffer                      │
│                    │      → SnapshotDiff (added/removed/etc) │
│                    │                                         │
│                    ▼                                         │
│           EmbeddingEncoder (Ollama nomic-embed-text)         │
│                    │                                         │
│                    ▼                                         │
│           CompressedPromptBuilder                            │
│                    │                                         │
│                    ▼                                         │
│           ┌────────────────────────────────────┐             │
│           │  CompressedPrompt:                  │             │
│           │   - 48 entities (3 changed)         │             │
│           │   - Changes: [...]                  │             │
│           │   - Similar past states: [top-3]    │             │
│           │   - Summary stats: {...}            │             │
│           │   Total: ~2500 tokens              │             │
│           └────────────────────────────────────┘             │
│                    │                                         │
└────────────────────┼─────────────────────────────────────────┘
                     ▼
            L4 / L5 (Reasoning, Intention)
```

### 4.4 Key Components

**EmbeddingEncoder** wraps the Ollama API:

```python
class EmbeddingEncoder:
    """Encodes entities and snapshots to embedding vectors."""
    
    def __init__(self, ollama_url: str, model: str = "nomic-embed-text"):
        self._url = ollama_url
        self._model = model
        self._cache: dict[str, list[float]] = {}  # content_hash → embedding
    
    async def encode_entity(self, entity: Entity) -> list[float]:
        """Encode a single entity to a vector."""
        text = self._entity_to_text(entity)
        return await self._embed(text)
    
    async def encode_snapshot(self, snapshot: WorldSnapshot) -> list[float]:
        """Encode an entire snapshot as a weighted average of entity embeddings."""
        entity_embeddings = await asyncio.gather(*[
            self.encode_entity(e) for e in snapshot.entities
        ])
        return self._weighted_average(entity_embeddings, snapshot.entities)
    
    def _entity_to_text(self, entity: Entity) -> str:
        """Convert entity to a string suitable for embedding."""
        parts = [f"{entity.entity_type}:{entity.entity_id}"]
        for attr, value in sorted(entity.attributes.items()):
            parts.append(f"{attr}={value}")
        return " | ".join(parts)
```

**SnapshotDiffer** computes diffs:

```python
class SnapshotDiff(BaseModel):
    added: list[Entity]
    removed: list[Entity]
    changed: list[tuple[Entity, Entity]]  # (old, new)
    unchanged_count: int
    total_count: int


class SnapshotDiffer:
    def diff(self, current: WorldSnapshot, previous: WorldSnapshot | None) -> SnapshotDiff:
        if previous is None:
            return SnapshotDiff(
                added=list(current.entities),
                removed=[],
                changed=[],
                unchanged_count=0,
                total_count=len(current.entities),
            )
        
        prev_by_key = {self._key(e): e for e in previous.entities}
        curr_by_key = {self._key(e): e for e in current.entities}
        
        prev_keys = set(prev_by_key.keys())
        curr_keys = set(curr_by_key.keys())
        
        added = [curr_by_key[k] for k in curr_keys - prev_keys]
        removed = [prev_by_key[k] for k in prev_keys - curr_keys]
        
        changed = []
        for k in prev_keys & curr_keys:
            old, new = prev_by_key[k], curr_by_key[k]
            if old.attributes != new.attributes:
                changed.append((old, new))
        
        unchanged = len(prev_keys & curr_keys) - len(changed)
        
        return SnapshotDiff(
            added=added, removed=removed, changed=changed,
            unchanged_count=unchanged, total_count=len(current.entities),
        )
```

**CompressedPromptBuilder** generates compact context:

```python
class CompressedPrompt(BaseModel):
    summary: str                    # "48 entities, 3 changed"
    changes_text: str               # Human-readable changes
    similar_states: list[dict]      # Top-K similar past snapshots
    key_metrics: dict[str, Any]     # Summary stats
    fallback_text: str | None       # Full text fallback if needed


class CompressedPromptBuilder:
    def __init__(self, memory: MemoryStore, encoder: EmbeddingEncoder):
        self._memory = memory
        self._encoder = encoder
    
    async def build(
        self,
        snapshot: WorldSnapshot,
        diff: SnapshotDiff,
        embedding: list[float],
    ) -> CompressedPrompt:
        # Find top-K similar past states
        similar = await self._memory.find_similar_snapshots(embedding, k=3)
        
        return CompressedPrompt(
            summary=f"{diff.total_count} entities ({len(diff.changed)} changed)",
            changes_text=self._format_changes(diff),
            similar_states=[s.to_summary_dict() for s in similar],
            key_metrics=self._compute_metrics(snapshot),
            fallback_text=None,
        )
```

### 4.5 Storage

A new Qdrant collection `snapshot_embeddings`:
- Vector dimension: 768 (nomic-embed-text default)
- Distance: Cosine
- Payload: snapshot summary, timestamp, entity count
- Pruning: keep last 1000, older summarized into rolling buckets

### 4.6 Fallback Strategy

If the embedding service is unreachable:
1. Log warning
2. Fall back to v1-style full JSON snapshots
3. L8 meta-loop detects the degradation and lowers reasoning frequency
4. Health check alerts the user

**See:** `PHASE_3_EMBEDDING_WORLD.md`.

---

## 5. Auto-Investigation Loop

### 5.1 Problem

In production, CoreMind v1 displays repeated anomalies without resolution:
- "Roborock hasn't cleaned since May 17" — even though it cleaned on May 24
- "Light bureau is unavailable" — never investigated
- "Health data anomaly: 36 steps today" — never queried for real data

This erodes user trust. The system must **actively investigate** anomalies, not just flag them.

### 5.2 Investigation Lifecycle

```
        anomaly detected
              │
              ▼
     ┌──────────────────┐
     │ FORMED            │ ← L4 creates InvestigationRun
     └────────┬─────────┘
              ▼
     ┌──────────────────┐
     │ DESIGNING_TEST    │ ← Test designer selects strategy
     └────────┬─────────┘
              ▼
     ┌──────────────────┐
     │ EXECUTING_TEST    │ ← Engine runs test via plugins
     └────────┬─────────┘
              ▼
     ┌──────────────────┐
     │ ANALYZING         │ ← Compare result to hypothesis
     └────────┬─────────┘
              ▼
   ┌──────────┼──────────┐
   │          │          │
   ▼          ▼          ▼
RESOLVED  UNRESOLVED  ESCALATED
(silent)  (re-queue)  (notify user)
```

### 5.3 Test Designers

Per anomaly type, a `TestDesigner` knows how to design a verification test:

| Anomaly Type | Test Strategy |
|---|---|
| Device unavailable | Ping HA API, check `last_seen`, check power state |
| Stale data | Query raw plugin data, compare to last update timestamp |
| Pattern change | Compare current embedding to similar past states |
| Missing data | Check plugin health endpoint, verify connectivity |
| Numeric anomaly | Query primary data source, compute baseline from last 30 days |

### 5.4 Resolution Examples

**Example 1: Stale Roborock claim**
- L4 detects: "Last cleaning was 2026-05-17"
- Investigation: Query HA `vacuum.s7_max_ultra_etat`, get `last_clean_at`
- Result: `last_clean_at = 2026-05-24T15:32:00`
- Conclusion: RESOLVED. Update narrative_state: "Roborock cleaned on 2026-05-24."
- Notification: Silent (user already knows the truth)

**Example 2: Light unavailable**
- L4 detects: "light.bureau is unavailable"
- Investigation: Query HA state, check connection status
- Result: Still unavailable. Last successful state change: 3 days ago.
- Conclusion: ESCALATED. Notify user with evidence: "Light bureau unreachable for 3 days. Suggest power cycle?"

**See:** `PHASE_4_AUTO_INVESTIGATION.md`.

---

## 6. Unified Actuator Surface

### 6.1 The Karpathy Vision

> "An assistant should scan your network, find your Sonos speakers and Hue lights, and let you control them with one command." — Andrej Karpathy

In v1, the LLM must know exact plugin operation names:
- `coremind.plugin.homeassistant.light.turn_on` with `{"entity_id": "light.bureau"}`
- `coremind.plugin.homeassistant.vacuum.send_command` with arcane params

In v2, the LLM uses:
- `coremind.act("turn off lights in the living room", confidence=0.85)`

The unified actuator handles discovery, mapping, and execution.

### 6.2 Components

- **DiscoveryEngine (L0)** — auto-detects devices via mDNS, HA, plugin manifests
- **CapabilityRegistry** — stores all known device capabilities with standardized action types
- **ActionMapper** — translates goals into protocol-specific `ResolvedAction` objects
- **UnifiedActuator** — single `act()` entry point delegating to L6 executor

### 6.3 Standardized Action Types

```python
class ActionType(str, enum.Enum):
    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"
    SET_BRIGHTNESS = "set_brightness"
    SET_COLOR = "set_color"
    SET_TEMPERATURE = "set_temperature"
    SET_VOLUME = "set_volume"
    PLAY = "play"
    PAUSE = "pause"
    START_CLEANING = "start_cleaning"
    LOCK = "lock"
    UNLOCK = "unlock"
    # ... etc
```

**See:** `PHASE_5_UNIFIED_ACTUATOR.md`.

---

## 7. Self-Improving Meta-Loop (L8)

### 7.1 Concept

L8 is the "system thinking about itself" layer. It observes the lower layers and makes adjustments to improve over time — within strict safety bounds.

### 7.2 Observation Categories

| Observation | Source | Threshold |
|---|---|---|
| `intent_repeat_rate` | L5 intent history | >3 same intent in 1h |
| `notification_ignore_rate` | L6 audit + user interactions | >70% over 7 days |
| `domain_approval_rate` | L6 approvals | <50% suggests slider too high |
| `plugin_error_rate` | L1 perception errors | >5% per hour |
| `token_per_useful_intent` | L4 token usage / L5 intent count | >5000 tokens/intent |
| `investigation_success_rate` | L4 investigations | <60% suggests test designer needs work |

### 7.3 Adjustment Policies

```python
class AdjustmentPolicy(BaseModel):
    name: str
    observation: str               # what triggers it
    parameter: str                 # what gets adjusted
    direction: Literal["increase", "decrease"]
    delta: float                   # how much per adjustment
    min_value: float
    max_value: float
    cooldown_hours: float          # min time between adjustments
    requires_user_approval: bool
```

**Built-in policies:**

| Policy | Trigger | Action |
|---|---|---|
| `lower_salience_when_quiet` | `intents_per_hour < 1` for 6h | Lower `min_salience` by 0.05 |
| `raise_salience_when_noisy` | `low_quality_intents > 5/h` | Raise `min_salience` by 0.05 |
| `increase_cooldown_on_ignored` | `notification_ignore_rate > 0.7` for a topic | Double cooldown for that topic |
| `propose_slider_promotion` | `domain_approval_rate > 0.8` for 10+ actions | Propose +0.1 to slider (needs approval) |
| `throttle_failing_plugin` | `plugin_error_rate > 0.5` | Double its poll interval |

### 7.4 Hard Safety Boundaries

L8 **cannot**:
- Modify `hard_ask` rules (finance, locks, messaging always stay ASK)
- Modify quiet hours
- Access or modify secrets
- Modify plugin permissions
- Disable audit logging
- Disable itself or other safety mechanisms
- Adjust any parameter outside its declared `min_value` / `max_value` bounds

Any attempt to violate these is rejected by the `MetaSafetyValidator` and logged as a security event.

**See:** `PHASE_2_SELF_IMPROVEMENT.md`.

---

## 8. Data Model — Complete Schema Reference

### 8.1 Autonomy

```python
class DomainConfig(BaseModel):
    slider: float = Field(ge=0.0, le=1.0)
    action_classes: list[str] = Field(default_factory=list)

class HardAskRule(BaseModel):
    action_class: str
    reason: str

class GraduationConfig(BaseModel):
    enabled: bool = True
    min_approvals_before_promotion: int = 10
    min_approval_rate_for_promotion: float = 0.8
    max_promotion_per_proposal: float = 0.1
    min_observation_days: int = 30
    promotion_cooldown_days: int = 7

class AutonomyConfig(BaseModel):
    default_slider: float = 0.4
    domains: dict[str, float] = Field(default_factory=dict)
    hard_ask: list[HardAskRule] = Field(default_factory=list)
    hard_safe: list[HardAskRule] = Field(default_factory=list)
    graduation: GraduationConfig = Field(default_factory=GraduationConfig)
```

### 8.2 Embeddings

```python
class EntityEmbedding(BaseModel):
    entity_id: str
    entity_type: str
    content_hash: str       # SHA256 of source text
    vector: list[float]
    computed_at: datetime

class SnapshotEmbedding(BaseModel):
    snapshot_id: str
    vector: list[float]
    entity_count: int
    timestamp: datetime
    summary: str            # human-readable summary

class SnapshotDiff(BaseModel):
    added: list[Entity]
    removed: list[Entity]
    changed: list[tuple[Entity, Entity]]
    unchanged_count: int
    total_count: int

class CompressedPrompt(BaseModel):
    summary: str
    changes_text: str
    similar_states: list[dict]
    key_metrics: dict[str, Any]
    fallback_text: str | None
```

### 8.3 Investigations

```python
class InvestigationStatus(str, enum.Enum):
    FORMED = "formed"
    DESIGNING_TEST = "designing_test"
    EXECUTING_TEST = "executing_test"
    ANALYZING = "analyzing"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    ESCALATED = "escalated"

class InvestigationTest(BaseModel):
    test_id: str
    test_type: str          # "ha_query", "plugin_call", "embedding_compare"
    parameters: dict[str, Any]
    timeout_seconds: float = 30.0

class InvestigationResult(BaseModel):
    test_id: str
    success: bool
    raw_output: dict[str, Any]
    error: str | None = None
    duration_seconds: float

class InvestigationRun(BaseModel):
    investigation_id: str
    anomaly_description: str
    anomaly_type: str
    hypothesis: str
    status: InvestigationStatus
    tests: list[InvestigationTest]
    results: list[InvestigationResult]
    conclusion: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    confidence: float = 0.0
```

### 8.4 Actuator

```python
class DeviceType(str, enum.Enum):
    LIGHT = "light"
    SWITCH = "switch"
    THERMOSTAT = "thermostat"
    VACUUM = "vacuum"
    LOCK = "lock"
    MEDIA = "media"
    SPEAKER = "speaker"
    CAMERA = "camera"
    SENSOR = "sensor"
    HUMIDIFIER = "humidifier"
    UNKNOWN = "unknown"

class Capability(BaseModel):
    action: ActionType
    protocol: str
    operation: str
    parameter_mapping: dict[str, str] = Field(default_factory=dict)

class DeviceCapabilities(BaseModel):
    device_id: str
    device_name: str
    device_type: DeviceType
    room: str | None = None
    capabilities: list[Capability]
    source_plugin: str
    last_seen: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

class ResolvedAction(BaseModel):
    device_id: str
    device_name: str
    action: ActionType
    protocol: str
    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
```

### 8.5 Meta (L8)

```python
class MetaObservation(BaseModel):
    observation_id: str
    kind: str               # e.g., "intent_repeat_rate"
    value: float
    threshold: float
    window_seconds: float
    observed_at: datetime
    metadata: dict[str, Any]

class AdjustmentRecord(BaseModel):
    adjustment_id: str
    policy_name: str
    parameter_path: str     # e.g., "intention.min_salience"
    old_value: Any
    new_value: Any
    reason: str
    triggered_by_observation: str
    applied_at: datetime
    rollback_at: datetime | None = None

class MetaConfig(BaseModel):
    enabled: bool = True
    observation_interval_seconds: float = 300.0
    policies: list[AdjustmentPolicy] = Field(default_factory=list)
    safety_bounds: dict[str, tuple[float, float]] = Field(default_factory=dict)
    forbidden_paths: list[str] = Field(default_factory=list)
```

---

## 9. Storage Architecture

### 9.1 SurrealDB Tables (New)

```sql
-- Autonomy change log
DEFINE TABLE autonomy_change SCHEMAFULL;
DEFINE FIELD domain ON autonomy_change TYPE string;
DEFINE FIELD old_slider ON autonomy_change TYPE float;
DEFINE FIELD new_slider ON autonomy_change TYPE float;
DEFINE FIELD reason ON autonomy_change TYPE string;
DEFINE FIELD changed_by ON autonomy_change TYPE string;
DEFINE FIELD changed_at ON autonomy_change TYPE datetime;

-- Investigation runs
DEFINE TABLE investigation_run SCHEMAFULL;
DEFINE FIELD investigation_id ON investigation_run TYPE string;
DEFINE FIELD anomaly_description ON investigation_run TYPE string;
DEFINE FIELD hypothesis ON investigation_run TYPE string;
DEFINE FIELD status ON investigation_run TYPE string;
DEFINE FIELD conclusion ON investigation_run OPTION TYPE string;
DEFINE FIELD started_at ON investigation_run TYPE datetime;
DEFINE FIELD completed_at ON investigation_run OPTION TYPE datetime;

-- Device capability registry
DEFINE TABLE device_capability SCHEMAFULL;
DEFINE FIELD device_id ON device_capability TYPE string;
DEFINE FIELD device_name ON device_capability TYPE string;
DEFINE FIELD device_type ON device_capability TYPE string;
DEFINE FIELD room ON device_capability OPTION TYPE string;
DEFINE FIELD source_plugin ON device_capability TYPE string;
DEFINE FIELD last_seen ON device_capability TYPE datetime;
DEFINE FIELD raw ON device_capability TYPE object;

-- Meta-loop observations
DEFINE TABLE meta_observation SCHEMAFULL;
DEFINE FIELD observation_id ON meta_observation TYPE string;
DEFINE FIELD kind ON meta_observation TYPE string;
DEFINE FIELD value ON meta_observation TYPE float;
DEFINE FIELD threshold ON meta_observation TYPE float;
DEFINE FIELD observed_at ON meta_observation TYPE datetime;

-- Meta-loop adjustments
DEFINE TABLE meta_adjustment SCHEMAFULL;
DEFINE FIELD adjustment_id ON meta_adjustment TYPE string;
DEFINE FIELD policy_name ON meta_adjustment TYPE string;
DEFINE FIELD parameter_path ON meta_adjustment TYPE string;
DEFINE FIELD old_value ON meta_adjustment TYPE any;
DEFINE FIELD new_value ON meta_adjustment TYPE any;
DEFINE FIELD reason ON meta_adjustment TYPE string;
DEFINE FIELD applied_at ON meta_adjustment TYPE datetime;
```

### 9.2 Qdrant Collections (New)

**`snapshot_embeddings`:**
- Vector size: 768 (nomic-embed-text)
- Distance: Cosine
- Payload schema:
  ```json
  {
    "snapshot_id": "string",
    "timestamp": "iso8601",
    "entity_count": "int",
    "summary": "string"
  }
  ```
- Index on `timestamp` for time-range queries
- Max points: 1000 (older are summarized and pruned)

### 9.3 JSON Files (Runtime State)

| File | Purpose |
|---|---|
| `~/.coremind/capability_registry.json` | Discovered device capabilities |
| `~/.coremind/run/notify_queue.jsonl` | Existing — unchanged |
| `~/.coremind/run/active_investigations.json` | Existing — refined by Phase 4 |

---

## 10. API & CLI

### 10.1 New CLI Commands

```bash
# Autonomy (Phase 1)
coremind autonomy show
coremind autonomy set <domain> <value>
coremind autonomy proposals
coremind autonomy approve <proposal_id>

# Meta-loop (Phase 2)
coremind meta status
coremind meta observations [--kind <kind>] [--last 24h]
coremind meta adjustments [--last 7d]
coremind meta policies                    # List active policies
coremind meta override --policy <name> --disabled

# Embeddings (Phase 3)
coremind world embed-stats                # Cache hits, token reduction stats
coremind world similar --limit 5          # Show top-K similar past states

# Investigations (Phase 4)
coremind investigations list [--status <status>]
coremind investigations show <id>
coremind investigations cancel <id>

# Actuator (Phase 5)
coremind actuator discover                # Force re-discovery
coremind actuator list [--room <room>] [--type <type>]
coremind actuator act "<goal>" [--confidence <float>]
coremind actuator registry stats
```

### 10.2 Dashboard Changes

New pages:
- `/autonomy` — slider controls + graduation proposals
- `/meta` — observations panel + adjustment history
- `/investigations` — active + completed investigation log
- `/devices` — device inventory + discovery status

Cockpit additions:
- "Devices: N discovered" stat
- "Meta-loop: N adjustments today" stat
- "Investigations: N active / M resolved this week" stat

### 10.3 gRPC Endpoints

Most v2 functionality is internal. New external gRPC endpoints (used by CLI/dashboard):

```protobuf
service Coremind {
    // Existing endpoints unchanged
    
    // Phase 1
    rpc GetAutonomyConfig (Empty) returns (AutonomyConfig);
    rpc SetSlider (SetSliderRequest) returns (Empty);
    rpc ListProposals (Empty) returns (ProposalList);
    rpc ApproveProposal (ApproveProposalRequest) returns (Empty);
    
    // Phase 2
    rpc GetMetaStatus (Empty) returns (MetaStatus);
    rpc ListObservations (ObservationsRequest) returns (ObservationList);
    rpc ListAdjustments (AdjustmentsRequest) returns (AdjustmentList);
    
    // Phase 4
    rpc ListInvestigations (InvestigationsRequest) returns (InvestigationList);
    rpc GetInvestigation (GetInvestigationRequest) returns (InvestigationRun);
    
    // Phase 5
    rpc DiscoverDevices (Empty) returns (DiscoveryResult);
    rpc ListDevices (ListDevicesRequest) returns (DeviceList);
    rpc Act (ActRequest) returns (ActResult);
}
```

---

## 11. Security & Safety

### 11.1 The Meta-Loop is the Largest Risk

A self-improving system that can modify its own parameters is genuinely dangerous if not bounded. CoreMind v2 mitigates this through:

1. **Hardcoded forbidden paths.** L8 cannot touch:
   - `autonomy.hard_ask` (immutable approval requirements)
   - `secrets.*` (credentials)
   - `audit.*` (logging)
   - `meta.safety_bounds` (the bounds themselves)
   - `meta.forbidden_paths` (this list itself)

2. **Min/max bounds per parameter.** Every adjustable parameter has declared bounds. Adjustments outside bounds are rejected by `MetaSafetyValidator`.

3. **Cooldown periods.** Each policy has a minimum time between adjustments. Prevents oscillation.

4. **User approval for high-impact changes.** Slider promotions require explicit user approval.

5. **Audit log.** Every adjustment is recorded in `meta_adjustment` table. Reversible.

6. **Rollback mechanism.** `coremind meta rollback <adjustment_id>` reverts any adjustment.

### 11.2 Approval Gates Remain

Hard ASK classes remain in v2:
- All `finance.*` operations
- All `security.*` operations
- All `messaging.send_external` / `email.send`
- All `lock.*`, `garage_door.open`
- All `plugin.install`, `config.modify`

These are enforced at the agency resolution layer (`resolve_agency()`) **before** the slider check. No slider value, no user setting, and no meta-loop adjustment can bypass them.

### 11.3 Audit Trail

Every significant event in v2 produces an audit record:
- Slider changes → `autonomy_change`
- Meta-loop adjustments → `meta_adjustment`
- Investigations → `investigation_run`
- Discovered devices → `device_capability` (with `last_seen` updates)
- Actions executed → existing `audit.jsonl`

---

## 12. Deployment & Migration

### 12.1 Migration Strategy

v2 is designed for **incremental migration** without downtime:

```
Week 1: Phase 1 (Autonomy Slider)
  - Add AutonomyConfig with defaults matching v1 behavior
  - All existing tests pass
  - Dashboard shows new sliders (read-only initially)

Week 2: Phase 5 (Unified Actuator)
  - Add L0 discovery
  - Register capabilities
  - Existing plugin calls continue working

Week 3: Phase 3 (Embedding World)
  - Add embedding encoder + differ
  - Run in parallel with v1 prompts (A/B compare)
  - Switch to embedding prompts when validated

Week 4: Phase 4 (Auto-Investigation)
  - Hook L4 anomalies to investigation engine
  - Resolve known-stale investigations
  - Monitor resolution rate

Week 5: Phase 2 (Meta-Loop)
  - Enable L8 with conservative policies
  - Observe-only mode for 1 week
  - Enable adjustments gradually
```

### 12.2 Migration Scripts

`scripts/migrate_v1_to_v2.py`:

```python
"""Migrate CoreMind v1 data to v2 schema.

Steps:
1. Backup ~/.coremind/data to ~/.coremind/data.v1.bak
2. Create new SurrealDB tables (idempotent)
3. Create new Qdrant collections (idempotent)
4. Map v1 forced categories to v2 sliders (preserving behavior)
5. Validate: run system in observation mode for 1 hour, compare decisions
"""
```

### 12.3 Rollback Procedure

If something breaks:

```bash
# Stop daemon
coremind daemon stop

# Restore v1 data
mv ~/.coremind/data ~/.coremind/data.v2.broken
mv ~/.coremind/data.v1.bak ~/.coremind/data

# Reinstall v1 package
pip install coremind==1.x

# Restart
coremind daemon start
```

### 12.4 Health Checks

After v2 deployment, monitor:
- `coremind meta status` — confirm L8 is observing
- `coremind investigations list` — confirm anomalies are being investigated
- `coremind actuator list` — confirm devices are discovered
- `coremind world embed-stats` — confirm token reduction
- `coremind autonomy show` — confirm sliders are loaded

---

## Appendix A: File Layout

```
coremind/
├── src/coremind/
│   ├── action/
│   │   ├── autonomy.py              # NEW - Phase 1
│   │   ├── graduation.py            # NEW - Phase 1
│   │   ├── schemas_autonomy.py      # NEW - Phase 1
│   │   ├── executor.py              # MODIFIED - Phase 1, 5
│   │   └── router.py                # MODIFIED - Phase 1
│   ├── actuator/                    # NEW - Phase 5
│   │   ├── discovery.py
│   │   ├── registry.py
│   │   ├── mapper.py
│   │   ├── unified.py
│   │   └── schemas.py
│   ├── investigation/               # NEW - Phase 4
│   │   ├── engine.py
│   │   ├── tests.py
│   │   ├── schemas.py
│   │   └── designers.py
│   ├── meta/                        # NEW - Phase 2
│   │   ├── observer.py
│   │   ├── adjuster.py
│   │   ├── safety_validator.py
│   │   ├── policies.py
│   │   └── schemas.py
│   ├── world/
│   │   ├── embeddings.py            # NEW - Phase 3
│   │   ├── differ.py                # NEW - Phase 3
│   │   ├── prompts.py               # MODIFIED - Phase 3
│   │   └── snapshot.py              # MODIFIED - Phase 3
│   ├── intention/
│   │   ├── loop.py                  # MODIFIED - Phase 3, 4
│   │   ├── prompts.py               # MODIFIED - Phase 3
│   │   └── stale_investigation_pruner.py  # MODIFIED - Phase 4
│   ├── reasoning/
│   │   └── loop.py                  # MODIFIED - Phase 3, 4
│   └── core/
│       ├── daemon.py                # MODIFIED - all phases
│       └── config.py                # MODIFIED - all phases
├── docs/
│   ├── ARCHITECTURE.md              # v1 (unchanged)
│   └── phases/v2/                   # NEW
│       ├── README.md
│       ├── EXECUTIVE_SUMMARY.md
│       ├── ARCHITECTURE.md          # THIS FILE
│       ├── PHASE_1_AUTONOMY_SLIDER.md
│       ├── PHASE_2_SELF_IMPROVEMENT.md
│       ├── PHASE_3_EMBEDDING_WORLD.md
│       ├── PHASE_4_AUTO_INVESTIGATION.md
│       └── PHASE_5_UNIFIED_ACTUATOR.md
└── tests/
    ├── test_autonomy.py             # NEW
    ├── test_embeddings.py           # NEW
    ├── test_investigation.py        # NEW
    ├── test_meta_loop.py            # NEW
    └── test_actuator.py             # NEW
```

---

**Next steps:** Read the individual phase documents in order. Phase 1 has no prerequisites and can begin immediately.
