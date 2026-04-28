# Development Guide

**Version:** 0.1
**Status:** Stable
**Audience:** Contributors running CoreMind locally for the first time.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12+ | `python3.12 --version` to verify |
| Docker | 24+ | Needed for SurrealDB |
| `just` | Any | [Installation](https://github.com/casey/just) |

---

## 1. Set up the environment

```bash
just setup
```

This creates a `.venv/` and installs all dev dependencies (including the `coremind` package in editable mode and the `coremind_plugin_systemstats` plugin).

---

## 2. Start the backing services

CoreMind requires SurrealDB for the World Model (L2).

```bash
docker compose up -d
```

This starts SurrealDB on `ws://127.0.0.1:8000/rpc` (user `root`, password `root`). These are the daemon's built-in defaults — no config file needed for local development.

To stop:

```bash
docker compose down
```

---

## 3. Verify lint and tests pass

```bash
just lint   # ruff check + mypy --strict
just test   # unit tests (no external services required)
```

---

## 4. Start the daemon

```bash
.venv/bin/coremind daemon start
```

The daemon will:

1. Load (or generate) its ed25519 keypair at `~/.coremind/keys/daemon.ed25519`.
2. Open the SurrealDB connection and apply the schema.
3. Open the plugin host Unix socket at `~/.coremind/run/plugin_host.sock`.
4. Begin the ingest loop, waiting for events from plugins.

Structured JSON logs are written to stderr. Set `COREMIND_LOG_LEVEL=debug` for verbose output.

---

## 5. Launch the system statistics plugin

In a second terminal:

```bash
.venv/bin/python -m coremind_plugin_systemstats
```

The plugin:

1. Generates its own keypair at `~/.coremind/keys/plugins/coremind_plugin_systemstats.ed25519` on first run.
2. Connects to the daemon over the Unix socket.
3. Emits a signed `WorldEvent` every 30 seconds with CPU usage, memory usage, and system uptime.

---

## 6. Verify with the CLI

Watch the live event stream:

```bash
.venv/bin/coremind events tail
```

Query recent events:

```bash
.venv/bin/coremind events query --limit 20
```

Inspect the world model snapshot:

```bash
.venv/bin/coremind world snapshot
```

List registered plugins (events by source):

```bash
.venv/bin/coremind plugin list
```

---

## Configuration (optional)

The daemon reads `~/.coremind/config.toml`. All fields have defaults — the file is not required for local development.

```toml
world_db_url      = "ws://127.0.0.1:8000/rpc"
world_db_username = "root"
world_db_password = "root"
plugin_socket     = "~/.coremind/run/plugin_host.sock"
max_plugins       = 64
```

Individual keys can also be overridden via `COREMIND_*` environment variables (e.g. `COREMIND_WORLD_DB_URL`).

---

## Integration tests

Integration tests require the backing services to be running:

```bash
docker compose up -d
just test-integration
```

These tests are marked `@pytest.mark.integration` and are excluded from the default `just test` run.
