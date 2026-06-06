# Phase 6 — Self-Model (Personal Context Understanding)

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [Phase Index](README.md)
**Prerequisites:** Phase 5 complete (Unified Actuator)
**Estimated effort:** 25–35 hours across 9 sub-phases

---

## 1. Problem Statement

CoreMind has a rich World Model of the **environment** (108 entities: lights, sensors, cameras, emails). But it has no model of **the user** — who they are, what they want, how they think, who matters to them.

A personal intelligence system cannot be limited to "the living room is 24°C and you have 3 emails." It must understand the person it assists.

**Current state:** Minimal `PersonalizationConfig` (language, timezone, name) and a transient `NarrativeMemory` (mood trend, patterns, concerns). No structured knowledge about relationships, goals, projects, routines, identity, or preferences.

**Target state:** A continuously-updated, confidence-scored knowledge base about the user — feeding into reasoning, intention generation, and conversation — that grows richer over time through passive observation and explicit declarations.

---

## 2. Design Overview

```text
DATA SOURCES (passive)          USER DECLARATIONS (active)
┌──────────────────────────┐    ┌──────────────────────┐
│ GitHub, VS Code, Telegram│    │ Conversation, CLI,   │
│ WhatsApp, Email, Calendar│    │ Seed file            │
│ Health, Presence, Firefly│    └──────────┬───────────┘
│ Immich                   │               │
└──────────┬───────────────┘               │
           │                               │
           ▼                               ▼
┌──────────────────────────────────────────────────────┐
│           EXTRACTION ENGINE (LLM-powered)            │
│  Mistral Small 3.2 via LLM.complete_structured()    │
│  Produces SelfFact proposals with confidence tiers   │
└──────────────────────────────┬───────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────┐
│              SELF-MODEL STORE (SurrealDB)             │
│  self_fact table — versioned, confidence-scored      │
│  Deduplication, decay, supersession                  │
└──────────────────────────────┬───────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────┐
│           SELF-MODEL PROVIDER (read API)             │
│  Injects context into Reasoning, Intention,         │
│  Conversation prompts                               │
└──────────────────────────────────────────────────────┘
```

### Entity Types

| Type | Purpose | Example |
| ---- | ------- | ------- |
| `person` | People in user's life | `person:aurelie` (relationship=fille) |
| `goal` | Declared or inferred goals | `goal:retirement` (target_year=2043) |
| `project` | Active work projects | `project:coremind` (phase=6, intensity=high) |
| `routine` | Behavioral patterns | `routine:coding` (window=20:00-00:00) |
| `identity` | Who the user is | `identity:tech` (role=architecte_ia) |
| `preference` | Learned preferences | `preference:voice` (style=radio, max_45s) |

### Confidence Tiers

| Level | Method | Confidence | Source |
| ----- | ------ | ---------- | ------ |
| L1 | `declared` | 0.95–1.0 | User explicitly stated |
| L2 | `observed` | 0.70–0.90 | Pattern detected from data |
| L3 | `synthesized` | 0.50–0.70 | Inference from multiple signals |
| L4 | `questioned` | 0.30–0.50 | Hypothesis to investigate |

**Critical constraint:** L3 and L4 facts **NEVER** trigger proactive notifications. They appear only in conversations as observations or questions.

---

## 3. Sub-Phase Index

| Sub-Phase | Title | Depends On | Effort |
| --------- | ----- | ---------- | ------ |
| [6A](PHASE_6A_FOUNDATIONS.md) | Foundations (Schemas, Config, Store) | Phase 5 | 2–3h |
| [6B](PHASE_6B_EXTRACTION_ENGINE.md) | Extraction Engine (LLM Fact Pipeline) | 6A | 3–4h |
| [6C](PHASE_6C_DEV_COLLECTORS.md) | Development Collectors (GitHub + VS Code) | 6A | 4–5h |
| [6D](PHASE_6D_COMMUNICATION_COLLECTORS.md) | Communication Collectors (Telegram, WhatsApp, Email) | 6A | 3–4h |
| [6E](PHASE_6E_CALENDAR_HEALTH_COLLECTORS.md) | Calendar & Health Collectors | 6A | 2–3h |
| [6F](PHASE_6F_FINANCE_MEDIA_COLLECTORS.md) | Finance & Media Collectors (Firefly, Immich) | 6A | 2–3h |
| [6G](PHASE_6G_DECLARED_FACTS.md) | Declared Facts & User Feedback | 6A + 6B | 2–3h |
| [6H](PHASE_6H_INTEGRATION.md) | Integration Layer (Reasoning, Intention, Conversation) | 6A + 6B + ≥1 collector | 4–5h |
| [6I](PHASE_6I_CLI_DASHBOARD.md) | CLI + Dashboard + NarrativeMemory Migration | 6H | 3–4h |

**Parallelism:** 6C, 6D, 6E, 6F can all run in parallel (they only depend on 6A). 6G needs 6B. 6H needs 6A + 6B + at least one collector done. 6I is last.

---

## 4. Deliverables Summary

```
src/coremind/self_model/
├── __init__.py              ← 6A (done)
├── config.py                ← 6A (done)
├── entities.py              ← 6A (done)
├── errors.py                ← 6A (done)
├── store.py                 ← 6A (done)
├── extractor.py             ← 6B
├── confidence.py            ← 6B
├── declared.py              ← 6G
├── feedback.py              ← 6G
├── provider.py              ← 6H
├── cli.py                   ← 6I
├── prompts/                 ← 6B
│   ├── extract_from_events.jinja2
│   ├── extract_from_communication.jinja2
│   ├── extract_from_activity.jinja2
│   └── synthesize_cross_source.jinja2
└── collectors/              ← 6C–6F
    ├── __init__.py
    ├── base.py              ← Collector Protocol
    ├── github.py            ← 6C
    ├── vscode.py            ← 6C
    ├── telegram.py          ← 6D
    ├── whatsapp.py          ← 6D
    ├── email.py             ← 6D
    ├── calendar.py          ← 6E
    ├── health.py            ← 6E
    ├── presence.py          ← 6E
    ├── firefly.py           ← 6F
    └── immich.py            ← 6F

plugins/vscode-activity/     ← 6C
├── package.json
├── tsconfig.json
├── src/extension.ts
└── README.md
```

---

## 5. Success Criteria (Overall Phase 6)

1. After 1 week of running with all sources enabled, ≥50 self-model entities present across ≥4 entity types.
2. Declared facts (via CLI or conversation) persist at confidence=1.0 and appear in reasoning context.
3. Extraction produces valid facts from golden test fixtures (schema validation passes).
4. Reasoning layer produces measurably richer output when self-model is enabled vs disabled.
5. `NarrativeMemory` data fully migrated; old class deprecated.
6. `coremind self-model list` shows all active entities with confidence and source.
7. VS Code extension tracks project activity and produces `routine:coding` entities.
8. User can `forget` any entity and it's removed from future prompts (audit logged).
9. All new code passes `just lint && just test`.
10. No self-model data leaks to external services (verified by audit log inspection).

---

## 6. Explicitly Out of Scope

- Psychological profiling or mood detection algorithms.
- Sharing self-model data outside CoreMind.
- Facebook Messenger integration (no personal API).
- Notion integration (defer to Phase 7).
- Real-time personality assessment.
- Autonomous notifications from L3/L4 facts (spec explicitly forbids this).
- Multi-user support (CoreMind is personal-first).

---

## 7. Handoff

After Phase 6, CoreMind knows **who the user is**, not just what's around them. The reasoning layer can produce contextually-aware insights grounded in personal history, goals, and relationships. This enables Phase 7's deeper cognitive capabilities.
