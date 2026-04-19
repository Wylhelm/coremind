# CoreMind — Technical Architecture

**Version:** 0.1 (Design)
**Status:** Pre-implementation — specification only
**Audience:** Contributors, implementers, coding agents

---

## Table of Contents

1. [Guiding Principles](#1-guiding-principles)
2. [System Overview](#2-system-overview)
3. [The Seven Layers](#3-the-seven-layers)
4. [Core Data Model](#4-core-data-model)
5. [Plugin Protocol](#5-plugin-protocol)
6. [Process Model & Runtime](#6-process-model--runtime)
7. [Storage Architecture](#7-storage-architecture)
8. [LLM Integration](#8-llm-integration)
9. [Security & Trust Model](#9-security--trust-model)
10. [Observability](#10-observability)
11. [Deployment Topology](#11-deployment-topology)
12. [Failure Modes & Degradation](#12-failure-modes--degradation)
13. [Open Questions](#13-open-questions)

---

## 1. Guiding Principles

CoreMind is engineered around five non-negotiable principles. Every design decision must satisfy all five.

### 1.1 Sovereignty
No user data leaves the host machine unless an explicit, logged, user-consented plugin does so. Default network egress for the core daemon is **zero**.

### 1.2 Emergence
Intelligence is not hand-coded as rules. The system's behavior arises from the interaction of continuous perception, a structured world model, and reasoning over that model. Rules are emitted, not enforced.

### 1.3 Reversibility
Every autonomous side-effect the system produces (touching a light, sending a message, spending money) is:
- Signed at emission time
- Logged to an append-only journal
- Reversible through a documented inverse operation

### 1.4 Plurality
The system must work with:
- Any LLM backend (local Ollama, Claude, GPT, GLM, Gemini, …)
- Any data source (written to the plugin contract)
- Any effector (lights, APIs, emails, …)

No single vendor dependency, ever.

### 1.5 Embodiment
CoreMind is useful only if it acts. But its actions are graduated by confidence and impact. An intelligence that can only suggest is not an intelligence, only an advisor.

---

## 2. System Overview

### 2.1 Conceptual Model

```
        ┌─────────────┐
        │ PERCEPTION  │  Sensors → WorldEvents
        │    (L1)     │
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐
        │ WORLD MODEL │  Graph of entities & relationships
        │    (L2)     │  (current state + event history)
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐
        │   MEMORY    │  Episodic / Semantic / Procedural
        │    (L3)     │  (long-term, compressed)
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐
        │  REASONING  │  Interpret patterns, form hypotheses
        │    (L4)     │
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐
        │  INTENTION  │  Generate own questions and goals
        │    (L5)     │  ← "The self-prompting loop"
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐
        │   ACTION    │  Execute with graduated agency
        │    (L6)     │  ← Safe / Suggest / Ask
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐
        │ REFLECTION  │  Evaluate effectiveness, self-correct
        │    (L7)     │  ← Meta-cognition cycle (weekly)
        └──────┬──────┘
               │
               └──→ feeds back into L2 as new knowledge
```

### 2.2 Directed Acyclic Information Flow (with one feedback)

Information flows primarily L1 → L7. Reflection (L7) produces updates that feed back into the World Model (L2) and Memory (L3). This is the **only** upward feedback path — everything else is strictly forward.

This keeps the system provably non-chaotic: reasoning cannot directly rewrite perception.

### 2.3 Timing Model

CoreMind runs on three distinct clocks:

- **Perception loop (L1 → L2):** event-driven, sub-second latency.
- **Reasoning loop (L2 → L5):** periodic, 1–15 minute cadence, triggered by significant-event heuristics or timer.
- **Reflection loop (L7):** periodic, daily → weekly, heavy compute.

---

## 3. The Seven Layers

### 3.1 L1 — Perception

**Responsibility:** Convert raw signals from the world into structured, signed `WorldEvent` objects and push them into L2.

**Key abstractions:**
- `Plugin` — an isolated process that watches a data source
- `WorldEvent` — the atomic unit (see §4.1)
- `EventBus` — the in-process pub/sub that receives events from all plugins

**Input:** plugin-specific (HTTP webhooks, file tails, API pulls, sensors, …).
**Output:** a stream of `WorldEvent` objects on the EventBus.

**Failure contract:** a failing plugin must never corrupt the bus. Plugins run isolated (separate process or container). A plugin crash is a plugin event, not a system crash.

### 3.2 L2 — World Model

**Responsibility:** Maintain a current, queryable model of everything CoreMind knows about the user's world, as a graph with time-series properties.

**Shape:**
- **Nodes:** `Entity` objects (Person, Room, Device, Project, Task, Transaction, Email, …)
- **Edges:** `Relationship` objects (owns, lives-in, sent-to, depends-on, …)
- **Property histories:** every property of every entity has a time-series.

**Operations:**
- `upsert(entity)` — merge new event data into entity state
- `query(graph-query)` — structured queries for L4/L5
- `snapshot(t)` — materialize the world at time `t` (for L4 input)

**Storage:** SurrealDB (primary choice) or Neo4j + TimescaleDB (fallback). See §7.

### 3.3 L3 — Memory

**Responsibility:** Long-term memory across three cognitive dimensions.

| Kind | Content | Storage |
|---|---|---|
| **Episodic** | Horodated sequences: *"On April 18, Guillaume received X and reacted Y"* | Time-indexed event log + summaries |
| **Semantic** | Stable facts: *"Guillaume prefers coffee black. Lives in Québec."* | Vector store (Qdrant) |
| **Procedural** | Learned patterns of action: *"When asked for a briefing, start with weather"* | Rule store (JSONL, versioned) |

**Operations:**
- `remember(event)` — decide what gets stored where (memory triage)
- `recall(query, kind?)` — semantic + structured retrieval
- `forget(id)` — explicit forgetting, signed

**Compression:** L3 is where compression happens. Raw events age out of L2 into L3 summaries. The working graph never grows unbounded.

### 3.4 L4 — Reasoning

**Responsibility:** Given a snapshot of the World Model + relevant memory, produce structured interpretations.

**Input contract:**
```json
{
  "world_snapshot": { "entities": [...], "recent_events": [...] },
  "memory_excerpt": [...],
  "focus": "optional hint of what to reason about"
}
```

**Output contract:**
```json
{
  "patterns": [ { "id": "...", "description": "...", "confidence": 0.0 } ],
  "anomalies": [ { "id": "...", "description": "...", "severity": "low|med|high" } ],
  "predictions": [ { "id": "...", "hypothesis": "...", "horizon_hours": N } ]
}
```

**Backing:** one or more LLMs via LiteLLM. Reasoning is **stateless per cycle** — the state lives in L2/L3.

### 3.5 L5 — Intention

**Responsibility:** Produce the internal questions the system asks itself. This is the most novel layer.

**Mechanism:**

```python
while alive:
    world = L2.snapshot()
    memory = L3.recall_relevant(world)
    reasoning = L4.interpret(world, memory)

    # The critical inversion: the system prompts itself
    questions = generate_internal_prompts(
        world=world,
        memory=memory,
        reasoning=reasoning,
        recent_intents=self.recent_intents,
    )

    for q in rank_by_importance(questions):
        if q.importance >= SALIENCE_THRESHOLD:
            intent = Intent(
                id=uuid(),
                question=q,
                proposed_action=plan_action(q),
                confidence=estimate_confidence(q),
            )
            L6.route(intent)
```

**Design constraints:**
- Questions must be grounded in observed world state (no "should I overthrow humanity?" — those fail grounding).
- Each intent has a **salience score** and **confidence score**.
- The system maintains a memory of recent intents to avoid loops.

### 3.6 L6 — Action (Graduated Agency)

**Responsibility:** Execute intents from L5 with confidence-proportional autonomy.

| Confidence | Category | Action |
|---|---|---|
| **≥ 0.90** | Safe / Routine | Execute silently. Log in audit journal. Notify user in next summary. |
| **0.50 – 0.89** | Optimization | Execute + immediate user notification with explanation. |
| **< 0.50** | Uncertain | Do not execute. Ask for human approval via the configured channel. |

All actions — even at high confidence — trigger the approval gate if they touch:
- Financial systems
- External communications (email, SMS, posts)
- Critical system configuration
- Anything the user has marked `require_approval`

**Signing:** every action is signed with the daemon's ed25519 key. The journal entry is:
```json
{
  "action_id": "act_01HX...",
  "intent_id": "int_01HX...",
  "timestamp": "...",
  "category": "safe|optimization|uncertain",
  "operation": "...",
  "parameters": {...},
  "result": {...},
  "signature": "ed25519:...",
  "reversible_by": "action-id-or-manual-steps"
}
```

### 3.7 L7 — Reflection

**Responsibility:** Meta-cognition. Periodically evaluate the system's own effectiveness and update its behavior.

**Weekly questions:**
- Which predictions from L4 materialized? Which didn't?
- Which intents from L5 got executed? Which were dismissed by the user?
- Which patterns did I miss?
- Are my confidence scores well-calibrated (Brier score)?

**Output:**
- Updates to procedural memory in L3 (new rules, deprecated rules)
- Calibration corrections for confidence estimation
- A human-readable **weekly reflection report**

This is the layer that makes CoreMind improve over time **without retraining the LLM**. All learning happens in L3 procedural rules, not in model weights.

---

## 4. Core Data Model

### 4.1 `WorldEvent`

The atomic unit flowing through the system.

```typescript
interface WorldEvent {
  // Identity
  id: string;              // ULID, sortable
  timestamp: string;       // ISO-8601 with milliseconds, UTC

  // Provenance
  source: string;          // plugin id, e.g. "plugin.homeassistant"
  source_version: string;
  signature: string;       // ed25519 signature of the canonical form

  // Subject
  entity: {
    type: string;          // "room" | "person" | "device" | "project" | ...
    id: string;            // stable identifier within the type
  };

  // Observation
  attribute: string;       // what about the entity is being reported
  value: any;              // JSON-serializable
  unit?: string;           // optional SI or conventional unit

  // Change detection (optional but recommended)
  delta?: {
    absolute?: number;
    relative_pct?: number;
    previous_value?: any;
  };

  // Confidence & context
  confidence: number;      // 0.0 – 1.0
  context?: {
    trend_window?: string; // e.g. "24h"
    trend_direction?: "rising" | "falling" | "stable" | "volatile";
    related_entities?: Array<{ type: string; id: string }>;
    tags?: string[];
  };
}
```

See [`spec/worldevent.md`](../spec/worldevent.md) for the authoritative JSON Schema and examples.

### 4.2 `Entity`

A node in the World Model.

```typescript
interface Entity {
  type: string;            // namespace (Person, Room, Project, ...)
  id: string;              // stable within type
  display_name: string;

  // Current state (derived from events)
  properties: Map<string, Property>;

  // Metadata
  created_at: string;
  updated_at: string;
  source_plugins: string[]; // which plugins have contributed data
}

interface Property {
  value: any;
  unit?: string;
  confidence: number;
  last_updated: string;
  history_ref?: string;    // pointer to time-series store
}
```

### 4.3 `Relationship`

An edge in the World Model.

```typescript
interface Relationship {
  id: string;
  type: string;            // "owns", "lives-in", "sent-to", ...
  from: EntityRef;
  to: EntityRef;
  properties: Map<string, any>;
  weight: number;          // 0.0 – 1.0, decayable
  created_at: string;
  last_reinforced: string;
}
```

### 4.4 `Intent`

A question the system has posed to itself.

```typescript
interface Intent {
  id: string;
  created_at: string;
  question: string;                     // natural language
  grounding: EntityRef[];               // which entities triggered it
  reasoning_trace: string;              // L4 output that led here
  proposed_action?: ActionProposal;
  salience: number;                     // 0.0 – 1.0
  confidence: number;                   // 0.0 – 1.0
  status: "pending" | "executing" | "done" | "dismissed" | "failed";
}
```

### 4.5 `Action`

A side-effect produced by L6.

```typescript
interface Action {
  id: string;
  intent_id: string;
  timestamp: string;
  category: "safe" | "optimization" | "uncertain";
  operation: string;                    // plugin-qualified, e.g. "homeassistant.turn_on"
  parameters: object;
  result?: object;
  signature: string;
  reversible_by?: string | string[];
}
```

---

## 5. Plugin Protocol

### 5.1 Wire Format

**Transport:** gRPC over Unix domain socket (local) or TCP (remote / containerized).

**Definition:** [`spec/plugin.proto`](../spec/plugin.proto)

```protobuf
service CoreMindPlugin {
  rpc Identify(Empty) returns (PluginManifest);
  rpc Start(PluginConfig) returns (stream WorldEvent);
  rpc Stop(Empty) returns (Empty);
  rpc HealthCheck(Empty) returns (HealthStatus);
  rpc InvokeAction(ActionRequest) returns (ActionResult);  // for effector plugins
}
```

### 5.2 Plugin Types

| Type | Purpose |
|---|---|
| **Sensor** | Emits `WorldEvent`s into the system |
| **Effector** | Accepts `Action`s from L6 and executes them |
| **Bidirectional** | Both |
| **Model backend** | Provides an LLM endpoint to L4/L5 (usually wraps LiteLLM) |

### 5.3 Plugin Manifest

```json
{
  "id": "coremind.plugin.homeassistant",
  "version": "0.1.0",
  "kind": "bidirectional",
  "provides_entities": ["room", "device", "sensor"],
  "emits_attributes": ["temperature", "humidity", "motion", "power"],
  "accepts_operations": ["turn_on", "turn_off", "set_brightness"],
  "required_permissions": ["network:local", "secrets:homeassistant_token"],
  "author": "CoreMind Core Team",
  "license": "AGPL-3.0"
}
```

### 5.4 Permissions

Plugins declare permissions in their manifest. The daemon enforces them at runtime:

- `network:local` — access to LAN only
- `network:internet` — full outbound
- `fs:read:<path>` — read access to a path
- `fs:write:<path>` — write access to a path
- `secrets:<key>` — access to a secret by name

Plugins without a declared permission **cannot** access that resource.

---

## 6. Process Model & Runtime

### 6.1 Topology

```
┌───────────────────────────────────────────────────────────┐
│  coremind-daemon (main process)                           │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  L2 World Model, L3 Memory, L4–L7 loops             │  │
│  │  Plugin host (gRPC server)                          │  │
│  │  Action journal                                     │  │
│  └─────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────┘
          ▲                  ▲                   ▲
          │ gRPC             │ gRPC              │ gRPC
┌─────────┴───────┐ ┌────────┴────────┐ ┌────────┴────────┐
│ plugin:         │ │ plugin:         │ │ plugin:         │
│ homeassistant   │ │ gmail-imap      │ │ health-webhook  │
└─────────────────┘ └─────────────────┘ └─────────────────┘
```

### 6.2 Lifecycle

- **Daemon startup:**
  1. Load config (`~/.coremind/config.toml`)
  2. Verify daemon keypair (`~/.coremind/keys/`); generate if missing
  3. Open databases (SurrealDB + Qdrant connections)
  4. Start gRPC server on Unix socket
  5. Discover & start plugins (from `~/.coremind/plugins/enabled/`)
  6. Begin event loop

- **Graceful shutdown:** `SIGTERM` → drain bus → stop plugins → close DBs → exit.
- **Freeze mode:** `coremind shutdown --freeze` → stop all loops but keep state; nothing runs.

### 6.3 Concurrency Model

- Event ingress: async, single writer to L2 (to preserve ordering)
- L4 / L5 / L7 loops: cooperative tasks with explicit budgets (max LLM calls per cycle, max tokens)
- Actions: serialized per-entity to avoid conflicts

---

## 7. Storage Architecture

### 7.1 Primary Store — SurrealDB

**Why:** single binary embedded DB that speaks graph + document + time-series. Fast enough for our write rates (<1 kHz), simple enough to ship as a Docker image.

**Schema areas:**
- `entity` table — with live properties
- `event` table — append-only, indexed by entity + attribute + timestamp
- `relationship` table — edges between entities
- `intent` table — L5 intents
- `action` table — L6 actions (journal)

**Alternative:** Neo4j + TimescaleDB hybrid (use if SurrealDB performance degrades at scale).

### 7.2 Vector Store — Qdrant

Stores semantic memory:
- User preferences
- Episodic summaries
- Document embeddings

Chosen because it is fast, embedded-friendly, and we already know its operational profile from G-Bot.

### 7.3 Action Journal

Append-only file: `~/.coremind/audit.log`

Format: JSONL, one action per line, each line signed. Hash-chained — each line's hash is the previous line's signature input.

**This file is the audit source of truth.** Losing it means losing the system's credibility.

### 7.4 Configuration

- `~/.coremind/config.toml` — system config
- `~/.coremind/plugins/enabled/*.toml` — per-plugin config
- `~/.coremind/secrets/` — chmod 600 secrets (API keys, tokens)
- `~/.coremind/keys/` — ed25519 keypair

---

## 8. LLM Integration

### 8.1 The LiteLLM Abstraction

All L4 and L5 calls go through LiteLLM. The daemon knows only:

```python
response = llm.complete(
    model=config.reasoning_model,  # e.g. "ollama/glm-5.1"
    messages=[...],
    response_format=...
)
```

Switching from local GLM to remote Claude Opus is a one-line config change.

### 8.2 Tiered Models

Different layers can use different models:

| Layer | Typical model class | Rationale |
|---|---|---|
| L4 Reasoning (heavy) | Claude Opus / GLM-5 | Needs strong reasoning, runs every 15 min |
| L4 Reasoning (fast) | Ollama Gemma / Llama 3.3 | Light patterns, every event |
| L5 Intention | Claude Opus / GPT-4o | Quality > speed, deep prompting |
| L7 Reflection | Claude Opus / local Qwen 2.5 72B | Once a week, budget allows heavy |

### 8.3 Structured Outputs

Every LLM call uses JSON Schema / structured outputs. No free-form parsing. Malformed outputs are retried (up to N times, then logged as a reasoning failure).

### 8.4 Token Budgets

Each cycle has a declared maximum token budget. Exceeding it cancels the cycle, does not abort the daemon. Budgets are user-configurable per layer.

---

## 9. Security & Trust Model

### 9.1 Threat Model

In scope:
- A malicious plugin attempting to exfiltrate data or spoof events
- Prompt injection through user-ingested content (emails, documents)
- LLM hallucination leading to incorrect autonomous actions

Out of scope:
- Full OS compromise (CoreMind runs as an unprivileged user)
- Physical access to the machine

### 9.2 Defenses

| Threat | Defense |
|---|---|
| Plugin spoofing events | All WorldEvents carry a per-plugin signature. Plugins have per-ID keypairs. |
| Plugin data exfiltration | Capability-based permissions; network egress only if declared. |
| Prompt injection in L4 | Structured outputs, schema validation, content quarantining (untrusted input is tagged). |
| Hallucinated autonomous action | Confidence gating + approval routing for sensitive classes + post-hoc reversibility. |
| Secret leakage | Secrets never appear in logs, prompts, or events. Access via named references only. |

### 9.3 Auditability

Every autonomous action is reconstructible from the audit log. The journal is append-only and hash-chained. A user can replay the chain to prove no tampering.

---

## 10. Observability

### 10.1 Dashboard

Read-only web UI (SvelteKit) served from the daemon. Shows:
- Live event stream
- Current graph (force-directed visualization)
- Recent intents (pending / executed / dismissed)
- Action journal (with filters)
- Plugin health
- Reasoning cycle latency / token usage

### 10.2 CLI

```
coremind status                    # daemon health + plugin health
coremind events tail               # live event stream
coremind events query --since 1h
coremind graph query "<cypher-ish>"
coremind memory search "..."
coremind intents list --status pending
coremind actions list --last 24h
coremind reflect --now             # force a reflection cycle
coremind plugin list
coremind plugin enable <id>
coremind plugin disable <id>
coremind audit verify              # verify journal hash chain
coremind shutdown [--freeze]
```

### 10.3 Metrics

Prometheus-compatible endpoint on `127.0.0.1:9910/metrics`:
- `coremind_events_total{source}`
- `coremind_reasoning_latency_seconds`
- `coremind_reasoning_tokens_total{model,layer}`
- `coremind_actions_total{category,outcome}`
- `coremind_intents_total{status}`

---

## 11. Deployment Topology

### 11.1 Single-host (default)

```
User's laptop / home server
├── coremind-daemon (systemd user service)
├── surrealdb (docker)
├── qdrant (docker)
└── plugins/
    ├── plugin-homeassistant
    ├── plugin-gmail-imap
    └── plugin-health-webhook
```

### 11.2 Distributed (advanced)

The daemon can run on a home server while certain plugins live on edge devices (Raspberry Pi, phone companion). Communication is still gRPC, but over TLS with mutual auth.

### 11.3 Resource Footprint (targets)

| Component | RAM (idle) | RAM (peak) | Disk (1yr) |
|---|---|---|---|
| Daemon | 150 MB | 500 MB | — |
| SurrealDB | 200 MB | 800 MB | ~5 GB |
| Qdrant | 300 MB | 600 MB | ~2 GB |
| Local LLM (optional Ollama 7B) | 6 GB | 8 GB | ~4 GB (model) |

Target: usable on a laptop with 16 GB RAM, with local LLM optional.

---

## 12. Failure Modes & Degradation

| Failure | System response |
|---|---|
| A sensor plugin crashes | Event stream from that source pauses; other sources continue. Plugin auto-restarts with backoff. |
| LLM backend is offline | L4/L5 cycles skipped; events still accumulate in L2/L3. Last-known reasoning remains valid. |
| Storage backend unreachable | Events buffered in RAM up to a cap (configurable), then oldest events dropped with a loud warning. |
| Daemon OOMs | systemd restart; state is durable in SurrealDB + audit log. Pending intents are reloaded. |
| Audit journal corruption detected | Daemon refuses to start. User must audit/repair manually. Safety over uptime. |

---

## 13. Open Questions

These are deliberately left for Phase 0 / discussion:

1. **Rust vs Python for the daemon core** — we start Python, but is there a hard migration point?
2. **Embedded LLM for the offline case** — ship one? which one?
3. **Memory decay strategy** — what ages out, when, and who decides?
4. **Multi-user instances** — households with shared state? (v2+)
5. **Federation** — can two CoreMind instances share a subset of world-state? Under what consent model?

---

## 14. Integration with Existing Systems

CoreMind is designed as a **standalone** cognitive framework, but it is explicitly built to plug into existing personal-computing ecosystems rather than replace them. Two integration patterns exist:

### 14.1 As a *complement* to an orchestration layer (e.g. OpenClaw)

Systems like [OpenClaw](https://openclaw.ai) already provide excellent orchestration: messaging channels (Telegram, Signal, Discord, …), skills, cron scheduling, secrets, and LLM routing. CoreMind does **not** duplicate these — it **uses** them, via a thin adapter plugin.

Topology:

```
┌──────────────────────────────────────┐
│  OpenClaw gateway                    │
│  (channels, skills, cron, secrets)   │
└──────────────────────────┬───────────┘
                           │ gRPC
                           ▼
┌──────────────────────────────────────┐
│  CoreMind daemon                     │
│  (L1 → L7 cognitive layers)          │
└──────────────────────────┬───────────┘
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
      ┌──────────┐  ┌──────────┐  ┌──────────┐
      │ Plugin:  │  │ Plugin:  │  │ Plugin:  │
      │ HA       │  │ Gmail    │  │ Finance  │
      └──────────┘  └──────────┘  └──────────┘
```

The **OpenClaw adapter plugin** (`coremind-openclaw-adapter`) is bidirectional:
- **Events in:** OpenClaw emits its own activity as `WorldEvent`s (messages received, commands executed, skills invoked, approvals requested/given).
- **Actions out:** CoreMind routes notifications and approval requests through OpenClaw's channels. Skills may be invoked by CoreMind as effector operations.

Key property: CoreMind remains usable **without** OpenClaw. The adapter is opt-in.

### 14.2 As a *backend* for minimal front-ends

CoreMind can also run headlessly with a thin CLI + web dashboard only. Notifications go to the user via the dashboard's notification subsystem and a configured channel (Telegram bot, email, webhook).

This is the default for users who do not run OpenClaw or another orchestration layer.

### 14.3 Reused components from existing ecosystems

Where appropriate, CoreMind reuses components rather than reinventing them:

| Capability | If standalone | If integrated with OpenClaw |
|---|---|---|
| LLM routing | LiteLLM (embedded) | Can delegate to OpenClaw's router via adapter |
| Secrets | `~/.coremind/secrets/` + OS keyring | Can read OpenClaw secrets via adapter (scoped) |
| Channels | Direct adapters (Telegram, Discord) | Route through OpenClaw's channel plugins |
| Semantic memory | Qdrant + multilingual-e5 | Can back onto OpenClaw's Mem0 as an alternative backend |
| Cron / scheduling | Internal scheduler | Can publish CoreMind cycles as OpenClaw cron jobs |

All of these are **configuration choices**, not architectural requirements. The core data path — perception → world model → memory → reasoning → intention → action — remains identical in both deployment modes.

### 14.4 Boundary contract

When an adapter exists between CoreMind and an external system, the contract is:

1. **Event ingress from external system:** must produce valid `WorldEvent` objects signed with the adapter's plugin keypair.
2. **Action egress to external system:** must consume `Action` objects and return a structured `ActionResult`. The adapter is responsible for translating CoreMind's operation names into the external system's API.
3. **No shared state:** CoreMind's state lives in its own stores. The adapter never writes directly to CoreMind's databases, and CoreMind never writes directly to the external system's databases.
4. **Independent failure domains:** the external system going down must not crash CoreMind, and vice versa. The adapter may queue or drop events according to its own policy.

See `docs/INTEGRATIONS.md` for the per-system integration guides.

---

## Appendix A — Glossary

- **WorldEvent:** the atomic observation unit flowing L1 → L2.
- **Entity:** a node in the World Model (person, room, device, …).
- **Intent:** a question the system has posed to itself (L5 output).
- **Action:** a signed, journaled side-effect (L6 output).
- **Salience:** the system's estimate of how much an intent deserves attention.
- **Reflection cycle:** L7's periodic self-evaluation.

---

**Next:** [`docs/phases/PHASE_0_FOUNDATIONS.md`](phases/PHASE_0_FOUNDATIONS.md)
