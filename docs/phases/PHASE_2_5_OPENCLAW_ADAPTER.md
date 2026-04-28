# Phase 2.5 — OpenClaw Adapter (optional, parallel)

**Duration:** ~3–5 days
**Prerequisite:** Phase 2 complete (memory + reasoning operational)
**Deliverable:** Bidirectional adapter that lets CoreMind observe OpenClaw activity and dispatch notifications/approvals/skills through OpenClaw's channels.

**This phase is optional.** Skip it if you don't run OpenClaw. It can also run in parallel with Phase 3 if you have capacity.

---

## Why this phase

OpenClaw already provides:
- Multi-channel messaging (Telegram, Signal, Discord, Slack, iMessage, Matrix, WhatsApp, …)
- Skill ecosystem
- Cron scheduling
- Secrets management
- Approval UI

Instead of reimplementing any of these, CoreMind plugs in via an adapter. Users of OpenClaw gain CoreMind's cognitive capabilities with no friction; CoreMind gains OpenClaw's entire channel surface for free.

---

## Goals

- Adapter package installable on both sides (OpenClaw extension + CoreMind plugin).
- OpenClaw activity (messages, skill invocations, cron runs) flows into CoreMind as `WorldEvent`s.
- CoreMind can invoke OpenClaw operations (`notify`, `approve_request`, `invoke_skill`) as effector actions.
- Optional: route CoreMind's LLM calls through OpenClaw's router.
- Optional: use OpenClaw's mem0 as an alternative L3 semantic backend.
- Conformance tests pass.

---

## Deliverables Checklist

- [ ] `integrations/openclaw-adapter/` — separate package (published later as a standalone repo too)
- [ ] `integrations/openclaw-adapter/coremind_side/` — CoreMind plugin half
- [ ] `integrations/openclaw-adapter/openclaw_side/` — OpenClaw extension half
- [ ] `integrations/openclaw-adapter/proto/` — adapter-specific proto extensions
- [ ] `integrations/openclaw-adapter/docs/SETUP.md` — install + config guide for end users
- [ ] `tests/integrations/openclaw_adapter/` — both sides tested from the main tree

The adapter currently ships in the main repo; once it stabilises it can be
lifted to its own repository and its tests moved under
`integrations/openclaw-adapter/tests/`. Until then the canonical test
location is the main-tree path above.

### Deferred to a later phase (explicitly out of scope here)

- `src/coremind/llm/openclaw_backend.py` — LiteLLM delegation to OpenClaw
  (section 2.5.6). Requires a new `InferModel` RPC on `OpenClawHalf`.
- `src/coremind/memory/backends/openclaw_mem0.py` — mem0 backend via
  adapter (section 2.5.7).
- `coremind-conformance test --plugin integrations/openclaw-adapter/coremind_side/`
  — the conformance CLI itself does not yet exist; wire this in when the
  conformance suite lands.

---

## Architecture

```
┌────────────────────────────────────────────────────────┐
│ OpenClaw gateway (Node.js, port 18789)                 │
│                                                        │
│   ┌────────────────────────────────────────────────┐   │
│   │ Extension: @coremind/openclaw-adapter          │   │
│   │                                                │   │
│   │  - Subscribes to OpenClaw event bus            │   │
│   │  - Translates OC events → WorldEvents          │   │
│   │  - Exposes OC operations as RPC endpoints      │   │
│   │  - gRPC client connecting to CoreMind          │   │
│   └──────────────────┬─────────────────────────────┘   │
│                      │                                 │
└──────────────────────┼─────────────────────────────────┘
                       │ gRPC over Unix socket
                       │ (or TLS TCP cross-host)
                       │
┌──────────────────────▼─────────────────────────────────┐
│ CoreMind daemon (Python, port N/A)                     │
│                                                        │
│   ┌────────────────────────────────────────────────┐   │
│   │ Plugin: coremind-openclaw-adapter              │   │
│   │                                                │   │
│   │  - gRPC server for the OpenClaw half           │   │
│   │  - Emits received events onto the EventBus    │   │
│   │  - Translates CoreMind Actions → OC RPCs      │   │
│   └────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────┘
```

The **OpenClaw half** is a TypeScript/JavaScript package (to match OpenClaw's runtime). The **CoreMind half** is Python. They speak gRPC.

---

## Tasks for the Coding Agent

### 2.5.1 Design the adapter protocol

**File:** `integrations/openclaw-adapter/proto/adapter.proto`

Define the gRPC service that the two halves use to talk to each other. Base it on `plugin.proto` with OpenClaw-specific additions:

```protobuf
syntax = "proto3";
package coremind.openclaw_adapter.v1;

import "plugin.proto";

// Implemented by the CoreMind half
service CoreMindHalf {
  rpc IngestEvent (coremind.plugin.v1.WorldEvent) returns (Empty);
  rpc HealthCheck (Empty) returns (Health);
}

// Implemented by the OpenClaw half
service OpenClawHalf {
  rpc Notify         (NotifyRequest)         returns (NotifyResult);
  rpc ApprovalRequest (ApprovalRequest)      returns (ApprovalResult);
  rpc InvokeSkill    (SkillInvocation)       returns (SkillResult);
  rpc ScheduleCron   (CronScheduleRequest)   returns (CronScheduleResult);
  rpc Mem0Query      (Mem0QueryRequest)      returns (Mem0QueryResult);
  rpc Mem0Store      (Mem0StoreRequest)      returns (Mem0StoreResult);
  rpc ListChannels   (Empty)                 returns (ChannelList);
  rpc ListSkills     (Empty)                 returns (SkillList);
  rpc HealthCheck    (Empty)                 returns (Health);
}

message NotifyRequest {
  string channel = 1;      // "telegram", "discord", "signal", ...
  string target = 2;       // chat/user id
  string text = 3;
  map<string, string> metadata = 4;
}
// ... etc.
```

### 2.5.2 Build the OpenClaw half (TypeScript)

**Directory:** `integrations/openclaw-adapter/openclaw_side/`

Structure:
```
openclaw_side/
├── package.json
├── tsconfig.json
├── src/
│   ├── index.ts                    # OpenClaw extension entry point
│   ├── openclaw_extension.ts       # registers with OpenClaw's plugin API
│   ├── event_bridge.ts             # listens to OC events, forwards as WorldEvents
│   ├── rpc_server.ts               # gRPC server implementing OpenClawHalf
│   ├── coremind_client.ts          # gRPC client to CoreMind half
│   ├── translators/
│   │   ├── oc_event_to_worldevent.ts
│   │   ├── message_received.ts
│   │   ├── skill_invoked.ts
│   │   ├── cron_executed.ts
│   │   └── approval_event.ts
│   └── config.ts
└── manifest.json                   # OpenClaw extension manifest
```

Implementation notes:
- Use `@openclaw/plugin-sdk` for the extension hooks.
- Events emitted from OC (via the gateway's event bus) are translated to `WorldEvent` with `source: "plugin.openclaw.adapter"`.
- Message bodies longer than 200 chars are **truncated** in the event payload; full body stays in OpenClaw mem0 and is only pulled if CoreMind explicitly requests it.
- The gRPC client to the CoreMind half reconnects with exponential backoff if CoreMind is down.
- Maintains a bounded on-disk queue of events to emit when CoreMind is unreachable.

### 2.5.3 Build the CoreMind half (Python)

**Directory:** `integrations/openclaw-adapter/coremind_side/`

Structure:
```
coremind_side/
├── pyproject.toml
├── coremind_plugin_openclaw/
│   ├── __init__.py
│   ├── manifest.toml
│   ├── main.py                      # plugin entry point
│   ├── plugin_side.py               # implements CoreMindPlugin (from plugin.proto)
│   ├── openclaw_client.py           # gRPC client to OpenClawHalf
│   ├── action_dispatcher.py         # CoreMind actions → OpenClaw RPC calls
│   └── server.py                    # gRPC server implementing CoreMindHalf
└── tests/
    └── test_end_to_end.py
```

Registered operations (in manifest):
```toml
[[accepts_operations]]
name = "openclaw.notify"
params_schema = "schemas/notify.json"
reversible = false

[[accepts_operations]]
name = "openclaw.approve_request"
params_schema = "schemas/approve_request.json"
reversible = false

[[accepts_operations]]
name = "openclaw.invoke_skill"
params_schema = "schemas/invoke_skill.json"
reversible = "depends"    # some skills are reversible, some aren't

[[accepts_operations]]
name = "openclaw.schedule_cron"
params_schema = "schemas/schedule_cron.json"
reversible = "openclaw.cancel_cron"
```

### 2.5.4 Permission declarations

The adapter's manifest (both halves) declares:

```toml
required_permissions = [
  "network:local",                       # Unix socket or localhost TCP
  "openclaw:channels:*",                 # can send through any channel
  "openclaw:skills:*",                   # can invoke any registered skill
  "openclaw:mem0:read",                  # if mem0 backend enabled
  "openclaw:mem0:write",                 # same
  "openclaw:cron:manage",                # if cron integration enabled
]
```

The user approves these at install time. They can narrow the scope (e.g. `openclaw:channels:telegram` only) via the adapter's config.

### 2.5.5 Event translation examples

**OpenClaw: "message received on Telegram"**

```json
// OpenClaw internal event
{
  "kind": "message.received",
  "channel": "telegram",
  "chat_id": "telegram:6394043863",
  "sender_id": "6394043863",
  "sender_name": "Guillaume",
  "text": "what's for dinner tonight?",
  "timestamp": "2026-04-19T20:14:02Z"
}
```

Becomes:

```json
// CoreMind WorldEvent
{
  "id": "evt_01HX...",
  "timestamp": "2026-04-19T20:14:02.123Z",
  "source": "plugin.openclaw.adapter",
  "source_version": "0.1.0",
  "entity": {
    "type": "conversation",
    "id": "telegram:6394043863"
  },
  "attribute": "message_received",
  "value": {
    "from": { "id": "6394043863", "display_name": "Guillaume" },
    "text_excerpt": "what's for dinner tonight?",
    "length_chars": 29,
    "has_media": false
  },
  "confidence": 1.0,
  "signature": "ed25519:..."
}
```

**CoreMind: "notify the user"**

```json
// CoreMind Action
{
  "operation": "openclaw.notify",
  "parameters": {
    "channel": "telegram",
    "target": "6394043863",
    "text": "I noticed you haven't slept well in 3 nights. Everything ok?"
  }
}
```

Becomes an RPC call to `OpenClawHalf.Notify` → OpenClaw sends the Telegram message → returns success + message id.

### 2.5.6 Optional: LLM backend delegation

**File:** `src/coremind/llm/openclaw_backend.py`

If the user prefers one LLM-config-to-rule-them-all, CoreMind can delegate LLM calls to OpenClaw's router.

- New LLM backend `OpenClawLLMBackend` implementing the same `LLM` interface as LiteLLM.
- Calls go out to OpenClaw via adapter RPC `OpenClawHalf.InferModel` (to be added to the proto).
- Structured-output enforcement, retries, token budgeting — all on the CoreMind side, same as LiteLLM.
- Config:
  ```toml
  [llm]
  backend = "openclaw"
  [llm.openclaw]
  model_mapping = {
    reasoning_heavy = "opus47",
    reasoning_fast  = "ollama-glm4-flash",
    intention       = "opus47",
    reflection      = "opus47",
  }
  ```

### 2.5.7 Optional: Mem0 backend

**File:** `src/coremind/memory/backends/openclaw_mem0.py`

Implements `SemanticMemoryBackend` but stores through OpenClaw's mem0 plugin via adapter RPCs.

Benefits:
- Unified personal memory across G-Bot + CoreMind
- Reuses OpenClaw's embeddings, Qdrant instance

Drawbacks:
- Extra hop latency
- Coupling to OpenClaw's mem0 schema

### 2.5.8 Tests

- Unit: event translators (OC event → WorldEvent) for each kind
- Unit: action dispatchers (CoreMind Action → OC RPC) for each operation
- Integration: docker-compose with a stub OpenClaw + a CoreMind daemon; assert round trips
- Conformance: runs the CoreMind conformance suite against the CoreMind half

### 2.5.9 Docs

**File:** `integrations/openclaw-adapter/docs/SETUP.md`

Step-by-step for end users:

```
# Install on OpenClaw side
openclaw plugins install @coremind/openclaw-adapter

# Install on CoreMind side
coremind plugin install coremind-openclaw-adapter

# Configure
coremind plugin configure coremind-openclaw-adapter
# prompts for: OpenClaw socket path, channels to expose, skills to expose

# Verify
coremind plugin status coremind-openclaw-adapter
# Expected: connected, X events received, 0 errors
```

Include troubleshooting for:
- Unix socket permission issues
- OpenClaw version mismatch
- gRPC handshake failures

---

## Success Criteria

1. Sending a Telegram message to G-Bot while both daemons are running produces a `message_received` `WorldEvent` in CoreMind within 2 seconds.
2. A CoreMind action `openclaw.notify` successfully sends a Telegram message via OpenClaw.
3. Stopping CoreMind while OpenClaw continues running: OpenClaw operates normally, adapter enters degraded mode, events buffered on disk.
4. Stopping OpenClaw while CoreMind continues running: CoreMind continues ingesting events from other plugins, adapter emits `integration.openclaw.degraded`, actions through the adapter queue or fail fast (config choice).
5. Permission narrowing works: if user restricts to `openclaw:channels:telegram`, an attempted `discord` notify from CoreMind is rejected by the adapter with a clear error.
6. Conformance suite passes.
7. `docs/SETUP.md` lets a fresh user install the full stack in under 15 minutes.

---

## Explicitly Out of Scope

- Multi-instance federation (two OpenClaw + two CoreMind sharing state) — v2+.
- Replacing OpenClaw's native features (we are an add-on, not a fork).
- Backward compatibility with OpenClaw versions older than the minimum declared in the manifest.

---

## Handoff

If Phase 2.5 is completed, Phase 3 can optionally:
- Use OpenClaw's approval UI instead of implementing a CoreMind-native one first.
- Route all channel operations through OpenClaw rather than implementing Telegram / Discord adapters separately.

If Phase 2.5 is skipped, Phase 3 implements its own Telegram channel adapter as originally planned.

**Next:** [`PHASE_3_INTENTION_ACTION.md`](PHASE_3_INTENTION_ACTION.md)
