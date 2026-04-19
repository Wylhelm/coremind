# CoreMind — Phase Roadmap

Sequential build guide. Each phase is self-contained, has hard success criteria, and hands off a known-good foundation to the next.

A coding agent (Codex, Claude Code, Cursor, etc.) should work through them **in order**. No phase is considered complete until its success criteria are demonstrably met.

---

## Total estimate: ~6 weeks (+ 3-5 days optional for Phase 2.5)

| # | Title | Duration | Core output |
|---|---|---|---|
| **0** | [Foundations](PHASE_0_FOUNDATIONS.md) | ~1 week | Specs, schemas, protocol, CI, repo structure |
| **1** | [Core Daemon + World Model](PHASE_1_CORE_DAEMON.md) | ~1 week | Live event pipeline into SurrealDB, one reference plugin |
| **2** | [Memory + Reasoning](PHASE_2_MEMORY_REASONING.md) | ~1.5 weeks | 3-kind memory, LLM-powered reasoning loop, 2 real plugins |
| **2.5** | [OpenClaw Adapter](PHASE_2_5_OPENCLAW_ADAPTER.md) *(optional)* | 3–5 days | Bidirectional integration with OpenClaw ecosystem |
| **3** | [Intention + Action](PHASE_3_INTENTION_ACTION.md) | ~1.5 weeks | Self-prompting, graduated agency, signed audit journal |
| **4** | [Reflection + Ecosystem](PHASE_4_REFLECTION_ECOSYSTEM.md) | ~1 week | Weekly self-evaluation, dashboard, v0.1.0 release |

**Phase 2.5 is optional.** Skip it if you don't use OpenClaw. It can also run in parallel with Phase 3 if you have capacity. See its own doc for details.

---

## How to use this roadmap

1. Open the phase you're tackling.
2. Read the **Goals** and **Deliverables Checklist** first.
3. Follow the **Tasks for the Coding Agent** section as ordered work items.
4. Validate against **Success Criteria** before merging the phase's work.
5. Check **Explicitly Out of Scope** to avoid scope creep.
6. When done, move to the next phase's handoff section.

---

## Critical rules across all phases

- **Every autonomous side effect is signed and journaled.** No exceptions.
- **No user data leaves the host** unless an opt-in plugin does so — and the event stream shows it.
- **Structured outputs only** for all LLM calls. No free-form parsing.
- **Tests land with the feature**, not after.
- **Docs land with the feature**, not after.
- **Every layer is independently replaceable** — don't hardwire across boundaries.
