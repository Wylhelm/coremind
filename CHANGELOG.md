# Changelog

All notable changes to CoreMind are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Schema and protocol versions are tracked independently from the package version; see [`docs/RELEASE.md`](docs/RELEASE.md).

---

## [Unreleased]

### Added
- OpenClaw adapter activated: Python bridge (`openclaw_side_bridge.py`) replaces TypeScript extension
- G-Bot heartbeat integration: CoreMind notifications delivered via JSONL queue
- `openclaw.plugin.json` manifest for future TypeScript extension loading

### Fixed
- Telegram approval callbacks now properly handled (missing `subscribe_responses()` consumer in daemon)
- `answerCallbackQuery` added to Telegram adapter — buttons no longer spin indefinitely
- `~` tilde expansion in Unix socket paths (adapter main.py)
- TypeScript strict mode fixes: exactOptionalPropertyTypes, unused variable

---

## [0.1.0] — Unreleased

First public release. Establishes the seven-layer cognitive architecture, a working daemon, three reference plugins, the audit-signed action layer, and a read-only dashboard.

### Phase 0 — Foundations

#### Added
- Repository scaffolding, `pyproject.toml` (Python 3.12+, AGPL-3.0-or-later), `Justfile` developer workflow.
- Authoritative specs: [`spec/worldevent.schema.json`](spec/worldevent.schema.json), [`spec/worldevent.md`](spec/worldevent.md), [`spec/plugin.proto`](spec/plugin.proto), [`spec/audit_log.md`](spec/audit_log.md).
- Architecture docs: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/EXECUTIVE_SUMMARY.md`](docs/EXECUTIVE_SUMMARY.md), [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md), phase-by-phase roadmap under `docs/phases/`.
- Lint/type/test toolchain: Ruff, mypy strict, pytest, JSON schema validation script.
- ed25519 + RFC 8785 (JCS) crypto primitives in `coremind.crypto.signatures`.
- `CoreMindError` hierarchy (`coremind.errors`).
- Generated gRPC stubs for the plugin protocol (`just proto-gen`).

### Phase 1 — Core Daemon + World Model

#### Added
- Async daemon shell with structured logging (`structlog`) and a typed `EventBus`.
- World Model store backed by SurrealDB (`src/coremind/world/`) with schema apply on startup.
- Plugin host: Unix-socket gRPC server, signed-handshake registration, signature verification on every event.
- Reference plugin `coremind_plugin_systemstats` emitting CPU/memory/uptime events.
- CLI: `coremind daemon`, `coremind events`, `coremind world snapshot`, `coremind plugin list`.

### Phase 2 — Memory + Reasoning

#### Added
- Three-kind memory layer (`coremind.memory`): episodic, semantic (Qdrant), and procedural rules.
- Reasoning loop (`coremind.reasoning`) with structured-output LLM wrapper (`LLM.complete_structured()` over LiteLLM).
- Reasoning cycle that snapshots the world, drafts salience and predictions, and writes typed outputs back to the bus.
- Two real plugins: `coremind_plugin_gmail_imap` (IMAP→World ingestion) and `coremind_plugin_homeassistant` (Home Assistant sensors).
- Provider allowlist enforcement on every LLM call.

### Phase 2.5 — OpenClaw Adapter (optional)

#### Added
- Bidirectional adapter under `integrations/openclaw-adapter/` exposing OpenClaw activity as `WorldEvent`s and accepting selected effector intents.
- Dedicated `adapter.proto` reusing `WorldEvent` payloads; generated stubs vendored under `coremind_side/_generated/` and `openclaw_side/`.

### Phase 3 — Intention + Action

#### Added
- Intention loop (`coremind.intention`) that turns reasoning salience into typed `Intent`s, each grounded in cited entities.
- Quiet-hours and persistence support for the intent queue.
- Action layer (`coremind.action`):
  - Effector router that dispatches intents to registered effectors.
  - Approval manager with three categories (`safe`, `suggest`, `ask`) and forced-approval classes that override confidence.
  - Append-only, signed JSONL audit journal with hash chaining (`~/.coremind/audit.log`).
- Notification Port abstraction with a Telegram adapter; CLI flow for approvals.
- `coremind audit verify` CLI command for hash-chain integrity.

### Phase 4 — Reflection + Ecosystem

#### Added
- Weekly reflection loop (`coremind.reflection`):
  - Prediction evaluator scoring `falsifiable_by` conditions against world history.
  - Calibration tracker (per layer, per model) with Brier score and reliability buckets.
  - Rule learner (v1) proposing procedural-rule promotions and deprecations; activation always human-gated.
  - Agency learner that proposes category promotions/demotions from the approval ledger as `ask` intents — never silently applied.
  - Markdown weekly report and on-demand `coremind reflect --now`.
- Web dashboard at `127.0.0.1:9900` (Starlette + Jinja2, server-rendered):
  - Pages: status overview, events (live SSE tail), reasoning cycles, pending intents, audit journal, weekly reports.
  - `Dashboard NotificationPort` adapter with in-dashboard approval buttons.
  - `/api/approvals` endpoint guarded by bearer-token auth, Origin validation, and operator identity binding; submissions flow through the same `ApprovalManager` as channel adapters.
  - Strict CSP, autoescaped templates, XSS-hardened SSE client.
- Documentation: [`docs/RELEASE.md`](docs/RELEASE.md), [`docs/FAQ.md`](docs/FAQ.md), [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md), `CHANGELOG.md`.

#### Deferred (post-v0.1.0)
- Second channel adapter beyond Telegram (Discord/Signal/email) — Task 4.7 deferred.
- Plugin Development Kit doc (`docs/PDK.md`) and `examples/plugin-template-python/` — Task 4.8 deferred.
- Force-directed `/graph` World Model visualization — requires a client-side bundle.
- Standalone single-file binary install path.

### Security

- Every autonomous side effect is signed with the daemon's ed25519 key and journaled in a hash-chained log.
- Plugins authenticate via per-plugin ed25519 keypairs; signature verification has no shortcut for "trusted" local plugins.
- API keys, tokens, and secrets live under `~/.coremind/secrets/` (chmod 600) and are accessed only through the `SecretsStore` port. They are never logged, never placed in `WorldEvent` payloads, and never included in LLM prompts.
- All external-facing inputs (webhooks, IMAP content, Notion pages, user messages) are tainted until classified; tainted content cannot flow into action-shaping paths without sanitization.
- Forced-approval action classes (financial, identity, destructive) override autonomous confidence thresholds.

[Unreleased]: https://github.com/gagnongui/coremind/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gagnongui/coremind/releases/tag/v0.1.0
