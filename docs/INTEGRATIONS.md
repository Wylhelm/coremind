# CoreMind — Integrations Guide

CoreMind is designed as a **standalone** cognitive framework, but most real-world users will run it alongside existing personal-computing systems. This document describes how CoreMind integrates with those systems and the contracts each integration must satisfy.

**Core principle:** CoreMind is never a dependency of another system, and no other system is a dependency of CoreMind. All integrations are **bidirectional adapters** that speak the `WorldEvent` / `Action` contract.

---

## Table of Contents

1. [Integration Model](#1-integration-model)
2. [OpenClaw](#2-openclaw-integration)
3. [Home Assistant](#3-home-assistant)
4. [Notion](#4-notion)
5. [Mem0 (as an alternative memory backend)](#5-mem0)
6. [Generic webhook systems](#6-generic-webhook-sources)
7. [Contract reference](#7-adapter-contract-reference)

---

## 1. Integration Model

Every external system is adapted to CoreMind through one of three shapes:

| Shape | Direction | Example |
|---|---|---|
| **Sensor plugin** | External → CoreMind (WorldEvents) | Gmail IMAP, Govee temperature, Apple Health webhook |
| **Effector plugin** | CoreMind → External (Actions) | Philips Hue, Sonos, Pushover notification |
| **Bidirectional adapter** | Both | Home Assistant, OpenClaw, Notion |

An integration is **opt-in** and **opt-out** at any time — disabling a plugin stops its event flow and removes its operations from the action router. CoreMind continues to run, with no data loss or schema migration required.

---

## 2. OpenClaw Integration

### 2.1 Purpose

[OpenClaw](https://openclaw.ai) is a personal-computing orchestration layer that already provides excellent infrastructure for:

- **Channels:** Telegram, Signal, Discord, Slack, iMessage, Matrix, WhatsApp, etc.
- **Skills:** a plugin ecosystem for discrete capabilities
- **Cron scheduling** with LLM-backed or plain execution
- **Secrets** management
- **LLM routing** (similar to LiteLLM)
- **Mem0** memory layer

CoreMind does **not** replicate any of these. Users who already run OpenClaw can plug CoreMind in to add the **continuous cognitive layer** they lack today (world model, intention loop, reflection).

### 2.2 Topology

```
┌──────────────────────────────────────────────────────────────────┐
│                          User                                    │
└──────────────┬──────────────────────────┬────────────────────────┘
               │                          │
               │ messages/commands        │ dashboard/CLI
               ▼                          ▼
   ┌───────────────────────┐   ┌───────────────────────────────┐
   │   OpenClaw gateway    │   │  CoreMind daemon              │
   │   (existing install)  │   │  (L1→L7 cognitive layers)     │
   └──┬─────────────────┬──┘   └──┬─────────────────────────┬──┘
      │                 │         │                         │
      │ native          │  ┌──────┴──────────┐    native    │
      │ channels        │  │ OpenClaw Bridge │    plugins   │
      │                 │  │ (Python gRPC)   │              │
      ▼                 │  │  ↕ notify_queue │              ▼
  Telegram               │  │  ↕ JSONL pipe   │         Home Assistant
  ...                    │  └──────┬──────────┘         Firefly III
                         │         │                    Apple Health
                         │    ┌────┴───────────┐       Open-Meteo
                         │    │  G-Bot          │       Vikunja
                         │    │  (heartbeat     │
                         │    │   reads queue)  │
                         │    └────────────────┘
                         │
  ┌──────────────────────┴──────────────────────────┐
  │  @coremindapp_bot  (approbations Ask/Approve)    │
  └─────────────────────────────────────────────────┘
```

Three notification paths:
1. **Approvals (Ask):** CoreMind → @coremindapp_bot direct (inline buttons)
2. **Notifications (Suggest):** CoreMind → Adapter → Bridge → Queue → G-Bot heartbeat → Telegram
3. **Safe:** Silently executed, logged in audit journal

### 2.3 The Adapter (v0.1.0 — Activated 2026-04-30)

Two components, deployed as systemd user services:

- **CoreMind side** (`coremind-openclaw.service`): Python plugin registered with the CoreMind daemon via `~/.coremind/keys/plugins/`. Listens on Unix sockets for gRPC calls from the daemon.
  - Config: `~/.coremind/plugins/openclaw_adapter.toml`

- **OpenClaw side** (`coremind-bridge.service`): Python gRPC server (`openclaw_side_bridge.py`) implementing the `OpenClawHalf` service. Listens on `unix://~/.coremind/run/openclaw-adapter.sock`.
  - Writes notifications to `~/.coremind/run/notify_queue.jsonl`
  - G-Bot's HEARTBEAT.md checks the queue and delivers messages with "🤖 CoreMind:" prefix

Future: the original TypeScript extension (`openclaw_side/`) is built but pending OpenClaw extension system compatibility.

### 2.4 Events from OpenClaw → CoreMind

The adapter emits `WorldEvent`s for OpenClaw activity so CoreMind can reason about it.

| OpenClaw event | CoreMind `WorldEvent` |
|---|---|
| User message received on channel X | `entity: {type: "conversation", id: "<channel>:<chat>"}`, `attribute: "message_received"`, `value: {from, text_excerpt, at}` |
| Assistant reply sent | `entity: {same}`, `attribute: "message_sent"`, `value: {to, text_excerpt, at, model_used}` |
| Skill invoked | `entity: {type: "skill", id: "<skill_id>"}`, `attribute: "invoked"`, `value: {args, result_status}` |
| Cron job ran | `entity: {type: "cron", id: "<job_id>"}`, `attribute: "executed"`, `value: {duration_ms, exit_code}` |
| Approval requested / answered | `entity: {type: "approval", id: "<approval_id>"}`, `attribute: "requested|approved|rejected"`, `value: {...}` |

Message bodies are **not** mirrored verbatim into CoreMind. Only metadata and short excerpts. Full bodies stay in OpenClaw's mem0.

### 2.5 Actions from CoreMind → OpenClaw

CoreMind can invoke the following OpenClaw-exposed operations through the adapter:

| CoreMind operation | OpenClaw action |
|---|---|
| `openclaw.notify` | Send a message via any configured channel |
| `openclaw.approve_request` | Present an approval card to the user (uses OpenClaw's existing approval UI) |
| `openclaw.invoke_skill` | Run an OpenClaw skill with arguments |
| `openclaw.schedule_cron` | Create or update a scheduled job |
| `openclaw.mem0_query` | Query OpenClaw's mem0 (if used as fallback L3 backend) |
| `openclaw.mem0_store` | Write to OpenClaw's mem0 |

All of these are subject to CoreMind's confidence gating and approval gates — but note that invoking `openclaw.approve_request` creates a **nested** approval surface: CoreMind asks OpenClaw to ask the user. This is intentional.

### 2.6 Shared Secrets

The adapter **never copies** secrets between the two systems. When CoreMind needs a secret that is already registered in OpenClaw:

1. CoreMind's plugin declares `required_permissions: ["openclaw:secrets:<name>"]`.
2. The user approves the permission once, at adapter install time.
3. Each access is logged in both OpenClaw's audit path and CoreMind's audit log.

No secret ever transits the adapter as plaintext unless it is being used in the current operation.

### 2.7 LLM Routing Delegation

By default, CoreMind runs its own LiteLLM instance. When OpenClaw is present, CoreMind can be configured to route LLM calls through OpenClaw's router instead:

```toml
# ~/.coremind/config.toml
[llm]
backend = "openclaw"      # "litellm" (default) | "openclaw" | "custom"

[llm.openclaw]
gateway_socket = "/run/openclaw/gateway.sock"
model_mapping = { reasoning_heavy = "opus47", intention = "opus47", reasoning_fast = "ollama-glm4-flash" }
```

Benefits: a single LLM-provider config, unified billing, shared rate limits.

### 2.8 Failure Modes

- **OpenClaw gateway down:** CoreMind continues running. Adapter queues outbound actions (bounded queue). Inbound events simply stop until OpenClaw returns. CoreMind emits a `integration.openclaw.degraded` meta-event.
- **CoreMind daemon down:** OpenClaw continues running with its normal behavior. The adapter on the OpenClaw side buffers events (bounded disk queue) until CoreMind returns.
- **Adapter crash:** both sides keep running; user is notified; adapter auto-restarts.

### 2.9 User Story

> *"I already use G-Bot (OpenClaw) as my daily assistant. Adding CoreMind takes 5 minutes: install the adapter on both sides, enable it, and within a day G-Bot starts sending me observations like 'I noticed you've been skipping breakfast this week. Is everything ok?' — things I never asked for, but useful."*

---

## 3. Home Assistant

### 3.1 Purpose

Home Assistant is the canonical source of smart-home state and the canonical effector for smart-home actions. CoreMind integrates as a bidirectional plugin (ships with the first release).

### 3.2 Events

- Subscribes to HA's WebSocket `state_changed` event stream.
- Maps each HA entity to a CoreMind `Entity` with deterministic id:
  - `sensor.bedroom_temperature` → `{type: "sensor", id: "bedroom.temperature"}`
  - `light.kitchen` → `{type: "light", id: "kitchen"}`
- Emits `WorldEvent`s with `source: "plugin.homeassistant"`.

### 3.3 Actions

Accepted operations:
- `homeassistant.turn_on` (light, switch)
- `homeassistant.turn_off` (light, switch)
- `homeassistant.set_brightness` (light)
- `homeassistant.set_color` (light)
- `homeassistant.set_temperature` (climate)
- `homeassistant.trigger_scene` (scene)
- `homeassistant.start_vacuum_segment` (vacuum with segment support)

Each accepted operation validates parameters against HA's service schema before executing.

### 3.4 Config

```toml
# ~/.coremind/plugins/enabled/homeassistant.toml
[homeassistant]
base_url = "http://homeassistant.local:8123"
access_token_ref = "secrets:ha_token"
entity_prefixes = ["sensor.", "binary_sensor.", "light.", "climate.", "vacuum."]
```

---

## 4. Notion

### 4.1 Purpose

Notion holds knowledge the user has externalized: projects, notes, databases, task lists. CoreMind benefits from awareness of this knowledge without mirroring it.

### 4.2 Integration Shape

**Sensor-only** in v0.1. Emits events:
- Page created / updated / archived → `entity: {type: "page", id: "<page_id>"}`
- Database row created / updated → `entity: {type: "db_row", id: "<row_id>"}`
- Task checked off (if using a Tasks DB) → `entity: {type: "task", id: "<task_id>"}`, `attribute: "completed"`

Full page content is summarized and stored in L3 semantic memory. Raw Markdown is not mirrored into L2.

### 4.3 Actions (v0.2+)

Later: `notion.create_task`, `notion.append_to_page`, `notion.update_row`.

---

## 5. Mem0

### 5.1 Purpose

Mem0 is a mature personal memory layer. Users already invested in Mem0 can reuse it as CoreMind's L3 semantic backend.

### 5.2 Integration Shape

**Memory backend**, not a plugin. CoreMind's `SemanticMemory` abstraction has three backend implementations:

```python
SemanticMemory(backend="qdrant")    # default, embedded
SemanticMemory(backend="mem0")      # Mem0 OSS
SemanticMemory(backend="openclaw-mem0")  # OpenClaw's mem0 instance via adapter
```

Config example:

```toml
# ~/.coremind/config.toml
[memory.semantic]
backend = "mem0"

[memory.semantic.mem0]
qdrant_url = "http://localhost:6333"
collection = "coremind_semantic"
embedding_provider = "ollama"
embedding_model = "nomic-embed-text"
embedding_url = "http://localhost:11434"
```

Both the qdrant and mem0 backends satisfy the same interface:
```python
async def remember(text: str, tags: list[str], metadata: dict) -> str
async def recall(query: str, k: int, tags: list[str] | None) -> list[Memory]
async def forget(memory_id: str, reason: str) -> None
```

### 5.3 Trade-offs

| | Qdrant direct | Mem0 |
|---|---|---|
| Speed | ★★★ | ★★ |
| Embedded install | Yes | Qdrant + mem0 layer |
| Graph memory | No (flat vectors) | Yes (entity extraction + Neo4j optional) |
| Cost | Free | Free (OSS) |
| Multi-agent sharing | No | Yes |

CoreMind recommends Qdrant direct for single-user, Mem0 for households or when sharing memory with other agents already on Mem0.

---

## 6. Generic Webhook Sources

Any system that can emit webhooks (Zapier, n8n, IFTTT, GitHub, Stripe, …) can be a CoreMind sensor via the built-in **webhook plugin**:

- `coremind-plugin-webhook` exposes an HTTPS endpoint on `127.0.0.1:9911/webhooks/<source_id>`.
- Each source has a shared secret + an event template (JSONPath extraction rules for building `WorldEvent`s from the webhook payload).
- No code required for common cases.

Example: GitHub commit → CoreMind event:

```toml
[webhook.source.github-coremind]
secret_ref = "secrets:github_webhook_secret"
template = "github-push.jinja"  # ships with the plugin
entity_type = "repo"
entity_id_from = "$.repository.full_name"
```

---

## 7. Adapter Contract Reference

Every adapter must implement the CoreMind plugin contract (see [`spec/plugin.proto`](../spec/plugin.proto)) plus these invariants:

### 7.1 Invariants

1. **Signed events.** All `WorldEvent`s emitted by the adapter are signed with the adapter's own ed25519 keypair. CoreMind verifies before ingest.
2. **No credential leakage.** Adapter must never include credentials in `WorldEvent` payloads or logs.
3. **Bounded state.** Adapter has a bounded in-memory and disk footprint. Buffers drop oldest when full with an audit-logged meta-event.
4. **Graceful degradation.** External system unavailable → adapter emits `integration.<name>.degraded` event, then quiet until system returns.
5. **Reversibility.** If the adapter exposes effector operations, each operation must declare its reversal (or declare explicitly "not reversible", with justification).
6. **Permission honesty.** The manifest lists every permission the adapter might exercise. Exceeding declared permissions is a fatal manifest mismatch.

### 7.2 Metadata the adapter must publish

In its manifest:

```json
{
  "id": "coremind.plugin.<name>",
  "version": "x.y.z",
  "kind": "sensor" | "effector" | "bidirectional",
  "integrates_with": "<external-system-name>",
  "external_system_version_range": ">=x.y, <z.0",
  "provides_entities": ["..."],
  "emits_attributes": ["..."],
  "accepts_operations": ["..."],
  "required_permissions": ["..."],
  "author": "...",
  "license": "AGPL-3.0"
}
```

### 7.3 Testing an adapter

Adapters live in their own repos but must pass the CoreMind **conformance suite**:

```bash
coremind-conformance test --plugin ./my-adapter/
```

The conformance suite:
- Starts a disposable CoreMind daemon
- Loads the adapter
- Runs a scripted scenario (event emission, signature verification, action dispatch, graceful shutdown)
- Reports pass/fail per invariant

---

## 8. Summary

| Integration | Shape | Phase shipped | Repo |
|---|---|---|---|
| Home Assistant | Bidirectional | Phase 2 (sensor) / Phase 3 (effector) | official |
| Gmail (IMAP) | Sensor | Phase 2 | official |
| OpenClaw | Bidirectional | Phase 2.5 / Phase 4 polish | official separate repo |
| Notion | Sensor (+ effector later) | Phase 4 | official |
| Mem0 backend | Memory backend | Phase 2 (optional) | built-in |
| Generic webhook | Sensor | Phase 4 | built-in |

All integrations preserve the core guarantee: **CoreMind runs standalone. Integrations are opt-in enrichments, never prerequisites.**

---

**See also:** [`ARCHITECTURE.md § 14`](ARCHITECTURE.md#14-integration-with-existing-systems) for the architectural discussion.
