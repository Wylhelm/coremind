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
just ci   # Full CI: ruff check + format + mypy + specs + proto-gen + 579 tests + 6 e2e
```

Individual steps:
```bash
just lint      # ruff + mypy
just test      # 569 unit tests
just test-scenarios  # 6 end-to-end scenarios
```

**Always run `just ci` before pushing.** OpenCode should be used for code changes (see CONVENTIONS.md).

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

---

## Production Deployment

```bash
# 1. Kill old plugins FIRST (they hold stale gRPC channels)
for pid in $(ps aux | awk '/[c]oremind-plugin/{print $2}'); do kill "$pid" 2>/dev/null; done
for pid in $(ps aux | awk '/[s]ide_bridge/{print $2}'); do kill "$pid" 2>/dev/null; done
sleep 2

# 2. Kill old daemon
kill $(cat ~/.coremind/run/daemon.pid 2>/dev/null) 2>/dev/null
sleep 2
rm -f ~/.coremind/run/daemon.pid

# 3. Start daemon with required env vars
cd ~/.openclaw/workspace/coremind
export OLLAMA_API_BASE=http://10.0.0.175:11434
export COREMIND_TELEGRAM_BOT_TOKEN="..."
nohup .venv/bin/coremind daemon start > /tmp/cm.log 2>&1 &
sleep 6

# 4. Start plugins with ALL required env vars
export HA_TOKEN="$(cat ~/.openclaw/secrets/ha-token)"
export FIREFLY_TOKEN="$(cat ~/.openclaw/secrets/firefly-token)"
export FIREFLY_URL=http://localhost:8080
export INFLUXDB_TOKEN="health-token-secret"
source ~/.openclaw/secrets/tapo-credentials
export TAPO_USERNAME TAPO_PASSWORD TAPO_IP
nohup .venv/bin/python3.12 integrations/openclaw-adapter/openclaw_side_bridge.py >> ~/.coremind/logs/bridge.log 2>&1 &
for plugin in homeassistant firefly openclaw-adapter weather vikunja tapo health; do
    nohup .venv/bin/coremind-plugin-$plugin >> ~/.coremind/logs/plugin-$plugin.log 2>&1 &
done
```

**Critical rules:**
- Kill plugins BEFORE daemon — gRPC channels to old socket become stale
- `INFLUXDB_TOKEN` required for health plugin
- `COREMIND_TELEGRAM_BOT_TOKEN` required for Telegram notifications
- `min_salience=0.35` is optimal for deepseek-v4-flash (scores 0.16-0.46)

These tests are marked `@pytest.mark.integration` and are excluded from the default `just test` run.
