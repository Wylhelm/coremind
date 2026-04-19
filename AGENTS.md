# AGENTS.md — CoreMind

This file is recognized by GitHub Copilot, Claude Code, Cursor, and other AI coding tools as always-on instructions for this workspace.

**The source of truth is [`.github/copilot-instructions.md`](.github/copilot-instructions.md)** — read it first, then return here for tool-specific guidance below.

---

## Mission

CoreMind is an open-source framework for **continuous personal intelligence**. 7 cognitive layers: Perception → World Model → Memory → Reasoning → Intention → Action → Reflection. The central innovation is *the LLM becomes its own user* — internal prompts generated from continuous observation.

## Key docs (read in this order when starting a session)

1. [`.github/copilot-instructions.md`](.github/copilot-instructions.md) — conventions, non-negotiables, tech stack
2. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — authoritative technical design
3. [`docs/phases/README.md`](docs/phases/README.md) — what we're building right now

## Current active phase

Update this line when starting a new phase:

> **Active phase: Phase 0 — Foundations** (see `docs/phases/PHASE_0_FOUNDATIONS.md`)

Do not implement anything outside the active phase.

## Custom agents available

When using GitHub Copilot Chat in VS Code, these custom agents are at your disposal:

- **@architect** — design decisions, not code
- **@phase-executor** — implement tasks from the active phase (default daily driver)
- **@reviewer** — senior-engineer code review
- **@debugger** — systematic root-cause analysis
- **@integrator** — plugins and external adapters

Slash commands available (custom prompts):

- `/execute-phase-task` — run a specific phase task
- `/design-review` — request a design review
- `/add-plugin` — scaffold a new plugin

## Tech stack one-liner

Python 3.12 + asyncio + Pydantic v2 + SurrealDB + Qdrant + LiteLLM + gRPC + ed25519 signatures. Ruff + mypy strict + pytest. AGPL-3.0.

## Non-negotiables (the short list)

1. Every autonomous side-effect is signed and journaled.
2. User data never leaves the host by default.
3. Structured LLM outputs only.
4. Tests land with the feature.
5. No `print`, no bare `except`, no naive datetimes.

The full list is in [`.github/copilot-instructions.md`](.github/copilot-instructions.md).

## Common commands

```bash
just setup          # create venv, install deps
just lint           # ruff + mypy
just test           # pytest
just spec-validate  # validate JSON schemas
just proto-gen      # regenerate protobuf stubs
just dev-up         # docker-compose up (SurrealDB + Qdrant)
just dev-down       # stop the dev stack
```

## When in doubt

- Architecture doc is authoritative.
- Simpler > clever.
- Observable > magical.
- Ask the user before scope-creeping or deciding something significant unilaterally.
