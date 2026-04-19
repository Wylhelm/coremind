# CoreMind — Executive Summary

**Version:** 0.1 (Design)
**Date:** April 2026
**Status:** Pre-implementation

---

## 1. The Thesis

> *A system that does not respond — one that **notices**.*

CoreMind is an open-source framework for building **continuous personal intelligence**: a cognitive daemon that runs alongside its user, perceives their world in real time, builds a coherent model of it, and autonomously generates its own questions and actions about that world.

The central innovation is simple but radical: **make the LLM its own user**. Instead of waiting for external prompts, CoreMind's language model receives prompts generated from the continuous observation of the user's life. It becomes a subject, not a tool.

## 2. What CoreMind Is Not

- **Not a chatbot.** There is no conversational surface at the center. Conversation is a side-channel, not the purpose.
- **Not an assistant.** Assistants wait for instructions. CoreMind initiates.
- **Not a SaaS product.** No mandatory cloud, no telemetry, no lock-in.
- **Not an automation engine.** Home Assistant runs rules you wrote. CoreMind generates rules from what it sees.

## 3. What CoreMind Is

- A **cognitive daemon** that runs continuously, locally.
- A **living graph** of the user's universe: people, objects, events, projects, physiological states, relationships.
- An **introspection loop** that produces hypotheses and actions proactively.
- A **plugin ecosystem** — any data source, any LLM, any effector.
- A **signed, reversible action layer** — every autonomous action is logged, attributable, and undoable.

## 4. The Five Pillars

| Pillar | Principle |
|---|---|
| 🔐 **Sovereignty** | User data never leaves the machine by default. Cloud is an explicit, opt-in plugin. |
| 🌱 **Emergence** | Intelligence is not programmed — it arises from the perception ↔ reasoning ↔ action loop. |
| 🪞 **Reversibility** | Every autonomous action is signed, logged, and reversible. No black magic. |
| 🧩 **Plurality** | Any LLM, any sensor, any effector, through a stable plugin protocol. |
| 🫀 **Embodiment** | Intelligence without a body is dead. CoreMind acts — with graduated agency and explicit consent. |

## 5. Architecture — Seven Cognitive Layers

```
┌────────────────────────────────────────────────────────────┐
│  L7 — Reflection   "Am I useful? Were my hypotheses right?"│  Weekly self-evaluation
├────────────────────────────────────────────────────────────┤
│  L6 — Action        Graduated agency: Safe / Suggest / Ask │  Signed, journaled effectors
├────────────────────────────────────────────────────────────┤
│  L5 — Intention     "What deserves my attention?"          │  Self-prompting loop
├────────────────────────────────────────────────────────────┤
│  L4 — Reasoning     "What does this pattern mean?"         │  LLM over graph snapshots
├────────────────────────────────────────────────────────────┤
│  L3 — Memory        Episodic + Semantic + Procedural       │  Vector + graph + relational
├────────────────────────────────────────────────────────────┤
│  L2 — World Model   Living graph of entities & events      │  SurrealDB / Neo4j
├────────────────────────────────────────────────────────────┤
│  L1 — Perception    Unified WorldEvent stream from plugins │  gRPC + signed payloads
└────────────────────────────────────────────────────────────┘
```

Each layer has a single, clear responsibility, and communicates with adjacent layers through well-defined contracts. Layers can be replaced independently.

## 6. The Atomic Unit: `WorldEvent`

Every signal in CoreMind — a temperature reading, an email, a transaction, a heartbeat, a git commit — becomes a `WorldEvent` in a unified, signed format:

```json
{
  "id": "evt_01HX8K2M3...",
  "timestamp": "2026-04-19T15:04:23.891Z",
  "source": "plugin.homeassistant",
  "entity": { "type": "room", "id": "bedroom" },
  "attribute": "temperature",
  "value": 22.4,
  "unit": "celsius",
  "delta": { "absolute": 1.2, "relative_pct": 5.7 },
  "confidence": 0.98,
  "signature": "ed25519:..."
}
```

Signed cryptographically by the emitting plugin. Every event is traceable, verifiable, replayable.

## 7. Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| Core daemon | Python 3.12 (asyncio + uvloop) — migration path to Rust | Velocity now, performance later |
| World graph | SurrealDB | Hybrid graph/doc/timeseries, embedded, modern |
| Memory store | Qdrant + multilingual-e5 embeddings | Proven, multilingual, fast |
| LLM router | LiteLLM | Model-agnostic: Ollama, Claude, GPT, local, remote |
| Plugin SDK | gRPC + Protobuf over Unix socket / TCP | Multi-language plugins (Python, JS, Go, Rust) |
| Dashboard | SvelteKit (read-only observability) | Observe your own cognition |
| CLI | `coremind` — Rust or Python Click | Operator-friendly |
| Distribution | Docker Compose + standalone binary | `curl ... | sh` one-liner install |

## 8. Differentiation

| Competitor | What they do | What CoreMind adds |
|---|---|---|
| Home Assistant | Reactive automation on user-written rules | **Proactive** rule generation, emergent intent |
| Rewind AI | Passive screen journal | Multi-source graph + **active reasoning** |
| Pi / Character.AI | Conversational AI companion | No conversation at the center — continuous observation |
| AutoGPT / BabyAGI | Executes user-defined goals | **Generates its own goals** from world observation |
| Mem0 | Memory layer for agents | A complete cognitive system of which memory is one layer |

## 9. Ethical Framework (Non-Negotiable)

These are baked into the core, not bolted on:

1. **Kill switch:** `coremind shutdown --freeze` stops all autonomous activity, preserves state.
2. **Approval gates:** All actions that (a) send data off-machine, (b) touch finances, or (c) modify critical systems require human confirmation — **even if the intent comes from the LLM itself**.
3. **Audit journal:** Every autonomous action writes to `~/.coremind/audit.log` — signed, append-only, reviewable.
4. **No training:** User data is never used to train remote models. Ever.
5. **License:** AGPL-3.0 — strong copyleft. No proprietary closed fork.

## 10. MVP Roadmap (6 weeks + optional 3–5 days)

The project is phased into discrete, shippable milestones — each documented in `docs/phases/`:

| Phase | Goal | Duration |
|---|---|---|
| **Phase 0** | Foundations: specs, schemas, repo structure | 1 week |
| **Phase 1** | Core daemon + World Model (L1 + L2) | 1 week |
| **Phase 2** | Memory + Reasoning (L3 + L4) | 1.5 weeks |
| **Phase 2.5** *(optional)* | OpenClaw bidirectional adapter | 3–5 days |
| **Phase 3** | Intention + Action (L5 + L6) | 1.5 weeks |
| **Phase 4** | Reflection + official plugins | 1 week |

Each phase is an independent deliverable with clear success criteria. Phase 2.5 is skippable for users who don't run OpenClaw.

## 10bis. Integration Strategy

CoreMind is **standalone-first** but explicitly built to integrate with existing personal-computing ecosystems. Rather than replacing orchestration layers like [OpenClaw](https://openclaw.ai), CoreMind plugs in via thin bidirectional adapters that respect clean failure domains.

- **Standalone mode:** CoreMind ships with its own CLI, dashboard, and channel adapters. Works end-to-end with zero other systems required.
- **Integrated mode:** the OpenClaw adapter (Phase 2.5) exposes CoreMind's intents and notifications through OpenClaw's channel surface (Telegram, Signal, Discord, etc.), and feeds OpenClaw's activity back into CoreMind as `WorldEvent`s.
- **No hard dependency:** CoreMind never requires OpenClaw. OpenClaw never requires CoreMind. Either system going down does not break the other.

This is the **Option B** integration pattern: standalone daemon + thin adapter. Full rationale and architecture in [`docs/INTEGRATIONS.md`](INTEGRATIONS.md) and [`docs/ARCHITECTURE.md` §14](ARCHITECTURE.md#14-integration-with-existing-systems).

## 11. Target Audience

- **Developers** building personal AI systems
- **Self-hosters** who reject SaaS for personal data
- **AI researchers** exploring continuous cognition architectures
- **Privacy-first users** who want an assistant that never phones home

Initial goal: 100 stars in 3 months, 10 external contributors, 5 community plugins.

## 12. The Ask (If CoreMind Gets Funded)

This is a solo / small-team project for the first 6 weeks. After that:

- 1 FTE to build the community and plugin ecosystem
- Infrastructure for a hosted demo (opt-in cloud daemon)
- Paid support / consulting tier (still AGPL)

But the framework must work end-to-end for a single user, self-hosted, at zero operational cost — **that's the demo**.

---

## 13. The Big Question CoreMind Answers

Not *"Can an AI do this for me?"* — but:

> *"What would an intelligence that lived with me for a year, that saw everything I saw, that noticed what I missed, tell me about my own life?"*

That's the product.

---

**Next step:** [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — the full technical design.
