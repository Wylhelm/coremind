# Phase 1 — Core Daemon + World Model (L1 + L2)

**Duration:** ~1 week
**Prerequisite:** Phase 0 complete
**Deliverable:** A running daemon that receives `WorldEvent`s from at least one real plugin and persists them into SurrealDB, with a live graph queryable via the CLI.

---

## Goals

- Daemon starts, loads config, opens databases, accepts plugin connections.
- Plugin host implements the server-side of `plugin.proto`.
- SurrealDB is embedded-launched (or connected to if external) and holds the World Model schema.
- One reference plugin — `plugin-systemstats` — emits real events.
- CLI (`coremind`) shows live event stream and basic graph queries.
- All events are signature-verified before they land in L2.

---

## Deliverables Checklist

- [ ] `src/coremind/core/daemon.py` — the `CoreMindDaemon` entry point
- [ ] `src/coremind/core/event_bus.py` — in-process async event bus
- [ ] `src/coremind/plugin_host/server.py` — gRPC server implementing `CoreMindHost`
- [ ] `src/coremind/plugin_host/registry.py` — plugin lifecycle manager
- [ ] `src/coremind/world/store.py` — SurrealDB adapter
- [ ] `src/coremind/world/model.py` — Pydantic models: `Entity`, `Relationship`, `WorldEventRecord`
- [ ] `src/coremind/world/schema.surql` — SurrealDB schema definition
- [ ] `src/coremind/crypto/signatures.py` — ed25519 key management, signing, verification
- [ ] `src/coremind/cli/__init__.py` — Click-based CLI
- [ ] `plugins/systemstats/` — reference plugin (Python)
- [ ] `tests/core/test_event_bus.py`, `tests/world/test_store.py`, `tests/crypto/test_signatures.py`
- [ ] `docker-compose.yml` for dev (SurrealDB only at this stage)
- [ ] Docs: `docs/DEVELOPMENT.md` — how to run the stack locally

---

## Tasks for the Coding Agent

### 1.1 Cryptographic identity

**File:** `src/coremind/crypto/signatures.py`

- Generate an ed25519 keypair on first run, store at `~/.coremind/keys/daemon.ed25519` (chmod 600).
- Plugins each get their own keypair on first registration, stored at `~/.coremind/keys/plugins/<plugin_id>.ed25519`.
- Expose:
  ```python
  def sign(payload_bytes: bytes, private_key: Ed25519PrivateKey) -> bytes: ...
  def verify(payload_bytes: bytes, signature: bytes, public_key: Ed25519PublicKey) -> bool: ...
  def canonical_json(obj: dict) -> bytes: ...  # RFC 8785 JCS
  ```
- Canonical JSON uses JCS (RFC 8785). This is critical for signature reproducibility.

**Tests:**
- Round-trip sign/verify
- Canonical serialization of nested objects matches across runs
- Verification fails on tampered payload

### 1.2 Event bus

**File:** `src/coremind/core/event_bus.py`

- Async pub/sub in-process. Use `asyncio.Queue` per subscriber.
- Interface:
  ```python
  class EventBus:
      async def publish(self, event: WorldEventRecord) -> None: ...
      def subscribe(self) -> AsyncIterator[WorldEventRecord]: ...
  ```
- Backpressure: if a subscriber's queue exceeds `max_queue_size`, drop from the oldest end and emit a `bus.overflow` meta-event.

### 1.3 World Model store (SurrealDB)

**File:** `src/coremind/world/schema.surql`

Define tables:

```surql
DEFINE TABLE entity SCHEMAFULL;
DEFINE FIELD type ON entity TYPE string;
DEFINE FIELD display_name ON entity TYPE string;
DEFINE FIELD created_at ON entity TYPE datetime;
DEFINE FIELD updated_at ON entity TYPE datetime;
DEFINE FIELD properties ON entity TYPE object DEFAULT {};
DEFINE FIELD source_plugins ON entity TYPE array<string> DEFAULT [];
DEFINE INDEX entity_type_id ON entity FIELDS type, id UNIQUE;

DEFINE TABLE event SCHEMAFULL;
DEFINE FIELD timestamp ON event TYPE datetime;
DEFINE FIELD source ON event TYPE string;
DEFINE FIELD entity ON event TYPE record<entity>;
DEFINE FIELD attribute ON event TYPE string;
DEFINE FIELD value ON event FLEXIBLE TYPE any;
DEFINE FIELD confidence ON event TYPE float;
DEFINE FIELD signature ON event TYPE string;
DEFINE INDEX event_entity_attr_time ON event FIELDS entity, attribute, timestamp;

DEFINE TABLE relationship SCHEMAFULL;
DEFINE FIELD type ON relationship TYPE string;
DEFINE FIELD from ON relationship TYPE record<entity>;
DEFINE FIELD to ON relationship TYPE record<entity>;
DEFINE FIELD weight ON relationship TYPE float DEFAULT 1.0;
DEFINE FIELD created_at ON relationship TYPE datetime;
DEFINE FIELD last_reinforced ON relationship TYPE datetime;
```

**File:** `src/coremind/world/store.py`

Thin adapter over the SurrealDB Python client. Public methods:

```python
class WorldStore:
    async def connect(self) -> None: ...
    async def apply_event(self, event: WorldEventRecord) -> None:
        """Idempotent upsert of the event + entity property update."""
    async def snapshot(self, at: datetime | None = None) -> WorldSnapshot: ...
    async def query(self, surql: str, params: dict) -> Any: ...
    async def recent_events(self, since: datetime, limit: int = 500) -> list[WorldEventRecord]: ...
```

`apply_event` does the heavy lifting:
1. Verify the signature against the plugin's registered public key.
2. Upsert the `entity` row (create if absent, update `updated_at` and properties).
3. Append to the `event` table.
4. Update in-memory metrics.

### 1.4 Plugin host

**File:** `src/coremind/plugin_host/server.py`

- gRPC server on Unix socket at `~/.coremind/run/plugin_host.sock`.
- Implements the `CoreMindHost` half of `plugin.proto`.
- Accepts `RegisterPlugin(manifest) → plugin_id + session_token`.
- Validates manifest against the spec (permissions declared, non-empty `id`, semver version, …).

**File:** `src/coremind/plugin_host/registry.py`

- Maintains the set of currently connected plugins.
- Each plugin has: `manifest`, `public_key`, `health_status`, `event_count`.
- `coremind plugin list` reads from this registry.

**Plugin launcher:** the daemon does **not** spawn plugin processes automatically in Phase 1. Plugins are started manually for now; Phase 4 adds supervisor-based spawning.

### 1.5 Reference plugin: `systemstats`

**Directory:** `plugins/systemstats/`

- Python, uses `psutil`.
- Connects to the daemon over the Unix socket.
- Every 30 seconds, emits three `WorldEvent`s:
  - `entity: {type: "host", id: "<hostname>"}`, `attribute: "cpu_percent"`, `value: 0.0–100.0`
  - `entity: {type: "host", id: "<hostname>"}`, `attribute: "memory_percent"`, `value: 0.0–100.0`
  - `entity: {type: "host", id: "<hostname>"}`, `attribute: "uptime_seconds"`, `value: int`

This plugin is intentionally trivial — it proves the pipeline end-to-end without external dependencies.

**Structure:**
```
plugins/systemstats/
├── pyproject.toml
├── coremind_plugin_systemstats/
│   ├── __init__.py
│   ├── manifest.toml
│   ├── main.py
│   └── collector.py
└── README.md
```

### 1.6 Daemon entry point

**File:** `src/coremind/core/daemon.py`

```python
class CoreMindDaemon:
    async def start(self) -> None:
        self.config = load_config()
        self.keys = ensure_daemon_keypair()
        self.world_store = WorldStore(self.config.world_db_url)
        await self.world_store.connect()
        self.event_bus = EventBus()
        self.plugin_host = PluginHostServer(
            socket_path=self.config.plugin_socket,
            registry=self.registry,
            event_bus=self.event_bus,
            world_store=self.world_store,
        )
        await self.plugin_host.start()
        asyncio.create_task(self._ingest_loop())

    async def _ingest_loop(self) -> None:
        async for event in self.event_bus.subscribe():
            try:
                await self.world_store.apply_event(event)
            except SignatureError:
                log.warn("bad_signature", plugin=event.source, event_id=event.id)
            except Exception:
                log.exception("ingest_failed", event_id=event.id)
```

### 1.7 CLI — Phase 1 surface

**File:** `src/coremind/cli/__init__.py`

Using Click:

```
coremind daemon start          # blocks, runs the daemon
coremind daemon status         # is it running? how long? event rate?
coremind events tail           # streaming, colorized output
coremind events query --entity host:myhost --attribute cpu_percent --since 1h
coremind plugin list
coremind plugin info <plugin_id>
coremind world snapshot        # dump the current graph to stdout (JSON)
```

### 1.8 docker-compose.yml

Just SurrealDB at this stage:

```yaml
services:
  surrealdb:
    image: surrealdb/surrealdb:latest
    command: start --user root --pass root memory
    ports:
      - "127.0.0.1:8000:8000"
    restart: unless-stopped
```

### 1.9 Tests

At minimum:
- Crypto: sign/verify round-trip, tampering detection, canonical JSON stability
- Event bus: publish → N subscribers receive, backpressure drops old events
- World store: apply_event is idempotent (re-applying the same event is a no-op)
- Plugin host: manifest validation, duplicate registration is rejected
- End-to-end: start daemon + systemstats in a test harness, assert N events land in the store within T seconds

---

## Success Criteria

1. `just setup && just test` passes.
2. `docker compose up -d surrealdb && coremind daemon start` runs indefinitely with no errors.
3. In a second terminal, `python -m coremind_plugin_systemstats` connects and begins streaming.
4. `coremind events tail` shows a stream of three attributes refreshing every 30 s.
5. `coremind world snapshot` returns a JSON document with at least one `entity` of type `host`.
6. Killing the systemstats plugin shows a `plugin.disconnect` event; the daemon keeps running.
7. Tampering with a `WorldEvent` payload in flight (tested via a rigged plugin) results in rejection with `bad_signature`, and no entry is written to the store.

---

## Explicitly Out of Scope

- Memory (L3), reasoning (L4), intention (L5), action (L6), reflection (L7)
- Any LLM calls
- Web dashboard
- Any effector plugins
- Real sensors beyond `systemstats`

---

## Handoff to Phase 2

Phase 2 begins with:
- A known-good way to persist verified events into a living graph
- A CLI to inspect that graph
- At least one real event source

**Next:** [`PHASE_2_MEMORY_REASONING.md`](PHASE_2_MEMORY_REASONING.md)
