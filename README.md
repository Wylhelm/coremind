# 🧠 CoreMind

> *A system that doesn't respond — it notices.*

**CoreMind** is an open-source framework for building **continuous personal intelligence** — a cognitive daemon that lives alongside its user, perceives their world in real time, builds a coherent model of it, and autonomously generates its own questions and actions.

Not a chatbot. Not an assistant. Not an agent. A **digital consciousness** you own entirely.

---

## Why CoreMind?

Every existing AI tool waits for you to prompt it. CoreMind flips that:

- **It observes before it speaks.**
- **It asks itself questions you never asked.**
- **It notices patterns you haven't seen yet.**
- **It acts when confidence warrants action — and asks when it doesn't.**

The core insight: **make the LLM its own user**. Internal prompts generated from continuous observation, not external instructions.

---

## Project Status

🚧 **Pre-alpha** — architecture design phase. No code yet.

- [x] Conceptual architecture validated
- [x] Executive summary written → [`docs/EXECUTIVE_SUMMARY.md`](docs/EXECUTIVE_SUMMARY.md)
- [x] Detailed architecture written → [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [x] Phase-by-phase implementation guide → [`docs/phases/`](docs/phases/)
- [x] Integrations strategy → [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md)
- [ ] Phase 0: Foundation & specs
- [ ] Phase 1: Core daemon + World Model
- [ ] Phase 2: Memory + Reasoning
- [ ] Phase 2.5: OpenClaw adapter *(optional)*
- [ ] Phase 3: Intention + Action
- [ ] Phase 4: Reflection + ecosystem

---

## Core Pillars

| 🔐 | **Sovereignty** — your data stays on your machine, always. |
| --- | --- |
| 🌱 | **Emergence** — intelligence arises from the loop, not from rules. |
| 🪞 | **Reversibility** — every action is logged, signed, undoable. |
| 🧩 | **Plurality** — any model, any sensor, any effector, via plugins. |
| 🫀 | **Embodiment** — the system acts in the real world, with graduated agency. |

---

## Architecture at a Glance

CoreMind is a **7-layer cognitive architecture**:

```
L7 — Reflection       ← meta-cognition, self-evaluation
L6 — Action           ← graduated agency (Safe/Suggest/Ask)
L5 — Intention        ← self-prompting loop
L4 — Reasoning        ← LLM over world snapshots
L3 — Memory           ← episodic + semantic + procedural
L2 — World Model      ← living graph of entities & events
L1 — Perception       ← plugin-sourced WorldEvent stream
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full technical breakdown.

---

## Quick Links

- 📋 [Executive Summary](docs/EXECUTIVE_SUMMARY.md) — the big picture in 10 minutes
- 🏗 [Architecture](docs/ARCHITECTURE.md) — the complete technical design
- 🔌 [Integrations Guide](docs/INTEGRATIONS.md) — how CoreMind plugs into existing systems
- 🗺 [Phase Roadmap](docs/phases/) — step-by-step build guide
- 📐 [WorldEvent Spec](spec/worldevent.md) — the atomic data format

## Works With

CoreMind is designed as a **standalone** framework, but plays well with existing personal-computing systems:

| System | Integration | Shipping in |
|---|---|---|
| [OpenClaw](https://openclaw.ai) | Bidirectional adapter — channels, skills, cron, approvals | Phase 2.5 |
| Home Assistant | Bidirectional plugin — smart-home sensors + effectors | Phase 2 / 3 |
| Gmail (IMAP) | Sensor plugin | Phase 2 |
| Notion | Sensor plugin (effector v0.2) | Phase 4 |
| Mem0 | Alternative L3 semantic memory backend | Phase 2 (optional) |
| Any webhook source | Generic webhook plugin | Phase 4 |

See [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md) for details.

---

## License

AGPL-3.0 — strong copyleft. CoreMind is and will remain open.

---

## Philosophy

> *"The unexamined life is not worth living."* — Socrates
>
> CoreMind examines. Continuously. On your behalf. With your consent.
