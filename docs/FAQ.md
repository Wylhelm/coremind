# Frequently Asked Questions

**Version:** 0.1
**Status:** Stable
**Audience:** Anyone evaluating, installing, or contributing to CoreMind.

For runtime errors and recovery procedures, see [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

---

## What CoreMind is and isn't

### Is CoreMind a chatbot or an agent framework?

No. CoreMind is a **cognitive daemon**: a long-running process that observes a user's world via plugins, maintains a structured world model, and autonomously generates its own questions and actions. It does not wait for prompts. The seven layers (Perception → World Model → Memory → Reasoning → Intention → Action → Reflection) are described in [`ARCHITECTURE.md`](ARCHITECTURE.md).

### Does CoreMind replace tools like LangChain, AutoGen, or CrewAI?

It targets a different problem. Those frameworks orchestrate task-bounded agent workflows triggered by a user prompt. CoreMind runs continuously, builds a model of the user over time, and emits its own intents. They can coexist: a CoreMind plugin can call into an external agent framework as one of its effectors.

### What does "the LLM becomes its own user" mean?

The reasoning loop (L4) consumes a snapshot of the world model and asks the LLM what is salient, surprising, or worth pursuing. The intention loop (L5) turns those answers into structured intents. There is no human prompt at the top of the cycle — observation is.

---

## Sovereignty and data

### Where does my data live?

On your machine. The World Model is in a local SurrealDB instance, semantic memory is in a local Qdrant instance, and the audit journal is a JSONL file under `~/.coremind/audit.log`. No cloud component is required for the core daemon.

### Does CoreMind ever phone home?

The core daemon makes **zero** outbound network calls by default. Plugins can make outbound calls only if the user grants the corresponding declared permission in `~/.coremind/config.toml`. The plugin event stream records every external call site, so an audit always shows what left the host.

### What about LLM providers? Don't they see my data?

Yes — that is a known and explicit trade-off. Hosted models (Anthropic, OpenAI, Google) receive whatever is included in a structured prompt by L4 or L5. Mitigations:

- The daemon supports any LiteLLM-compatible endpoint, including local models (Ollama, llama.cpp). Configure `llm.model` to a local provider for a fully offline setup.
- Prompts are constructed from typed snapshots, not raw event bodies. Message bodies live in L3 (semantic memory) and are not pulled into prompts unless the reasoning task explicitly cites the entity.
- The reasoning model allowlist is enforced; an unexpected provider cannot be silently substituted.

Embedded LLM support — shipping a small model out of the box and making offline mode the default — is on the post-v0.1.0 roadmap.

### Can I run CoreMind fully offline?

Yes, with these substitutions: a local LLM via Ollama or llama.cpp, the local sentence-transformers embeddings extra (`pip install 'coremind[embeddings-local]'`), and plugins that do not require external services. SurrealDB and Qdrant are already local.

---

## Plugins

### How do I add a new sensor or effector?

Write a plugin that implements the gRPC contract in [`spec/plugin.proto`](../spec/plugin.proto). The reference Python plugins under [`plugins/`](../plugins/) are the canonical examples. The PDK doc covers scaffolding, signing, and publishing.

### Why must plugins sign their events?

Signatures are the only trust boundary CoreMind has. The daemon does not trust the IPC channel; it trusts the keypair the plugin registered. A compromised plugin cannot impersonate another plugin's events. Even local "trusted" plugins go through the signature check — there is no shortcut.

### Can plugins call each other?

No. Plugins emit `WorldEvent`s and consume effector intents from the daemon. Cross-plugin interaction happens through the world model, not through plugin-to-plugin RPC. This keeps the trust graph simple.

### What languages can I write plugins in?

Anything that speaks gRPC and ed25519. Python is the first-class path (libraries provided). JavaScript/TypeScript and Go are supported via the gRPC contract. The post-release roadmap lists official SDK libraries for JS/TS, Go, and Rust.

---

## Agency and safety

### How does CoreMind decide whether to act on its own?

Every action carries an `action_class`. Classes are mapped to one of three agency modes in config: `safe` (autonomous), `suggest` (proposes; runs after a short window unless dismissed), or `ask` (requires explicit approval). Confidence thresholds gate `safe` execution. **Forced-approval classes** (anything touching financial, identity, or destructive surfaces) always require approval, regardless of confidence. The full table is in [`ARCHITECTURE.md`](ARCHITECTURE.md) §15.

### Can CoreMind raise its own agency level?

Indirectly and only with consent. The reflection loop (L7) analyzes the approval ledger and may **propose** a category change (e.g. `ask` → `suggest`) when a consistent pattern of user approvals exists. The proposal itself is filed as an `ask` intent — CoreMind never silently changes its own permissions. See [`ARCHITECTURE.md`](ARCHITECTURE.md) §15.7.

### Is every action reversible?

Every action is **journaled** with a signature, so every action is *replayable* and *audit­able*. Reversibility depends on the effector. Effectors declare a `reversibility` field; the action layer refuses to autonomously execute irreversible classes. The audit journal lets a human reconstruct the world state at any point.

### What happens if a plugin or LLM provider crashes mid-cycle?

The reasoning cycle is bounded and idempotent: a partial cycle aborts cleanly, emits a meta-event on the bus, and the next cycle starts from the latest world snapshot. The journal is append-only and chained, so a crash never produces a partial signed entry.

---

## Operations

### How heavy is the daemon?

Resting cost is modest: SurrealDB, Qdrant, and the Python process. Concrete profiling lands in the v0.1.x cycle. The hot path (event bus, signature verification) is a candidate for a Rust port post-v0.1.0; until then it is pure Python with `uvloop` in production.

### How do I see what CoreMind is doing?

Three options, all read-only:

1. The dashboard at `http://127.0.0.1:9900` (events, world snapshot, intents, journal, weekly reports).
2. The CLI: `coremind events tail`, `coremind world snapshot`, `coremind audit verify`.
3. The structured logs on stderr (JSON in production, human-readable in dev).

### How do I uninstall?

`pipx uninstall coremind` removes the daemon. The `~/.coremind/` directory contains your keys, audit journal, and config — back them up before deletion if you want a forensic record. The SurrealDB and Qdrant data directories are managed by docker-compose volumes; remove them with `docker compose down -v`.

---

## Project

### Why AGPL-3.0?

To keep network-deployed forks of CoreMind under the same terms as local deployments. If a hosted service is ever built on top of CoreMind, AGPL ensures its source is available to its users. The license is non-negotiable.

### How do I contribute?

Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) and the active phase doc under [`docs/phases/`](phases/). The phase docs are explicit about what is in and out of scope. Every PR must pass `just lint && just test` and land tests with the feature.

### Where is the roadmap after v0.1.0?

The "Post-Release: Ecosystem Phase" section of [`docs/phases/PHASE_4_REFLECTION_ECOSYSTEM.md`](phases/PHASE_4_REFLECTION_ECOSYSTEM.md) lists community-track and core-track priorities (plugin SDKs, federation RFC, Rust hot path, embedded LLM, memory decay, multi-user instances).
