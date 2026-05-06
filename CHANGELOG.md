# Changelog

All notable changes to CoreMind are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Schema and protocol versions are tracked independently from the package version; see [`docs/RELEASE.md`](docs/RELEASE.md).

---

## [0.3.1] — 2026-05-06

### Fixed — Robustness & Resilience
- **Plugin crash recovery**: all 8 plugins now wrap their main loop in auto-reconnect logic.
  If the daemon restarts or the gRPC socket disappears, plugins reconnect automatically
  with exponential backoff instead of dying silently.
- **Race condition at startup**: `start-all.sh` now waits for the daemon socket to appear
  before launching plugins, preventing the "socket not found" crash that killed 5/8 plugins.
- **Watchdog auto-restart**: `start-all.sh` launches a background supervisor that checks
  every 60s and restarts any dead plugin process. No more silent plugin deaths.
- **Ingest loop resilience**: the critical ingest loop (EventBus → WorldStore) now
  auto-restarts on crash instead of logging a fatal and stopping.
- **PresenceDetector crash guard**: wrapped in a retry loop inside the daemon.

### Fixed — Data Staleness (Presence Detection)
- **Staleness check**: `PresenceDetector` now checks `entity.updated_at` and ignores
  camera data older than 10 minutes. This prevents CoreMind from claiming someone
  is "at their desk" for hours after the tapo plugin crashed.
- **Message phrasing**: the presence alert now says "tu travailles" instead of
  incorrectly claiming "tu es à ton bureau" (the camera is in the living room).

### Fixed — Message Flow & Action Execution
- **No more double messages**: the internal `InternalQuestion` is no longer sent to the
  user. Only the action's `expected_outcome` (now required in natural French) is shown.
- **Conversation intents now execute**: intents classified as "conversation" (high
  salience ≥0.70) now execute their proposed action after a 2-minute grace window.
  Previously they only sent a message and silently discarded the action.
- **Natural French**: the intention LLM prompt now requires `expected_outcome` in
  natural French, not third-person English ("Je te préviens si…" instead of
  "User receives a notification about…").

### Added
- `src/coremind/plugin_helpers.py`: shared retry/reconnection utilities for plugins.
- `scripts/healthcheck.sh`: daemon health check script used by the HEARTBEAT.

## [0.3.0] — 2026-05-04

### Added — Pillar 1: Natural Conversation
- `coremind/conversation/` module: `ConversationHandler`, `ConversationStore`, prompts, schemas
- Two-way Telegram text message handling via unified `subscribe_all()` (prevents poll_offset race)
- Conversation starters: high-salience intents (≥0.70) become open-ended messages instead of approval prompts
- `InboundTextMessage` schema for text capture from notification channels
- `"conversation"` notification category — messages without buttons, inviting text reply

### Added — Pillar 2: Vision (Camera Sensors)
- **Tapo C225 plugin** (`plugins/tapo/`): RTSP snapshot capture every 5 min
- **Webcam plugin** (`plugins/webcam/`): USB camera frame capture + motion detection
- **Vision analysis via Ollama Pro**: Gemini 3 Flash (primary) + Mistral Large 3 (fallback)
  - Scene attributes: `person_present`, `person_name`, `activity`
  - Pet detection: `pets_visible`, `pet_description` with cat name identification
- **Immich face recognition**: 13 reference face thumbnails extracted via Immich v2 API
  - Two-pass system: text description → face matching with reference images if "unknown"
  - Batched comparison (7 faces per Ollama call, max 8 images)

### Added — Pillar 3: Physical Presence (Nest Hub)
- `notify/adapters/nest_hub.py`: `NestHubAdapter` using gbot-say.sh + PyChromecast
- `presence/` module: `PresenceScheduler` for ambient interactions
- Morning greetings, URL casting to Nest Hub display

### Added — Pillar 4: Narrative Identity
- `memory/narrative.py`: `NarrativeMemory` with JSON persistence and auto-decay
- Narrative context injected into reasoning prompts and reflection cycles
- Observations accumulate from reasoning output, decay after 7 days

### Added — Presence Detection
- `presence/detector.py`: `PresenceDetector` — monitors camera events for prolonged presence
- Generates conversation intents after 1h of continuous desk activity
- Personalized alerts with person name from vision pipeline

### Changed
- **Vision**: switched from Google direct API to Ollama Pro (Gemini 3 Flash)
- **Telegram**: `subscribe_all()` merged dispatches both text messages and approval callbacks
- **Reasoning prompts**: temporal pattern detection for camera presence events
- **Conversation threshold**: 0.85→0.70 for more proactive initiation
- **Tapo plugin**: added `ollama`, `Pillow` dependencies
- **start-all.sh**: `COREMIND_TELEGRAM_BOT_TOKEN`, `TAPO_USERNAME`, `TAPO_PASSWORD` exports

### Fixed
- `subscribe_text_messages` / `subscribe_responses` poll_offset race condition
- `NotificationPort.notify()` missing required `actions`/`intent_id` kwargs
- Narrative memory `_render_for_prompt()` method name in daemon wiring
- Nest Hub adapter pointing to correct gbot-say.sh path

---

## [Unreleased]

### Added (2026-05-02 — L4 & L7 Wiring)
- **L4 Reasoning Loop activated** — generates patterns, anomalies, predictions from world snapshot every 30 minutes
- **L7 Reflection Loop activated** — evaluates predictions, calibrates confidence, learns procedural rules every 24 hours
- `QdrantVectorStore` — concrete `VectorStorePort` implementation wrapping `qdrant-client`
- `BasicConditionResolver` — simple resolution for prediction evaluation (LLM-backed resolver planned)
- `FeedbackEvaluatorImpl` — counts approved/rejected/reversed/dismissed intents and actions
- `ReflectionNotifier` — delivers L7 Markdown reports via notification router
- `EmptyRuleSource` — reusable no-op rule source for reflection bring-up
- `GmailEffector` — queries Gmail via `gog gmail search --json` (replaces gmail-imap plugin)
- `scripts/start-all.sh` — single command to start daemon + bridge + all 6 plugins
- `[llm.embedding]` config section for `nomic-embed-text` via Ollama
- Symlinks for `mcporter` and `gog` in `~/.local/bin` for plugin PATH resolution

### Changed
- **Intention filtering tightened**: `min_salience` 0.25→0.45, `min_confidence` 0.50→0.55
- **Intention cadence**: 600s→3600s (10min→1h)
- **Quiet hours enabled**: 23h-7h America/Toronto
- **Calendar effector** fixed: `gog calendar list` → `gog calendar events --json`
- **LLM model**: reasoning layer uses `ollama/mistral-large-3:675b-cloud` (was deepseek-v4-flash, server overload)
- `ARCHITECTURE.md` updated to v0.2.0 with active L4/L7 timing and model details

### Fixed
- Plugin environment: `HA_TOKEN`, `FIREFLY_TOKEN`, `INFLUXDB_TOKEN` injected for homeassistant, firefly, health plugins
- `OLLAMA_API_BASE` set to remote Ollama instance (10.0.0.175:11434) in start script
- `mcporter` and `gog` CLI tools now accessible from CoreMind plugin context

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
