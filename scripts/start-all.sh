#!/usr/bin/env bash
# CoreMind — Start daemon + all plugins with correct env vars
# v2: Robust startup with socket wait, retry, and auto-restart watchdog
set -e

COREMIND_HOME=/home/guillaume/.openclaw/workspace/coremind
COREMIND_VENV=$COREMIND_HOME/.venv/bin
LOG_DIR="$HOME/.coremind/logs"
RUN_DIR="$HOME/.coremind/run"
PID_DIR="$RUN_DIR/pids"

# --- LLM Backends ---
export OLLAMA_API_BASE="http://10.0.0.175:11434"

# --- Secrets ---
export COREMIND_TELEGRAM_BOT_TOKEN=$(cat /home/guillaume/.coremind/secrets/telegram_bot_token)
export TAPO_USERNAME=$(cat /home/guillaume/.coremind/secrets/camera_username 2>/dev/null || echo 'admin')
export TAPO_PASSWORD=$(cat /home/guillaume/.coremind/secrets/camera_password 2>/dev/null || echo '')
export GEMINI_API_KEY=$(python3 -c "import json; c=json.load(open('$HOME/.opencode/config.json')); print(c['providers']['google']['apiKey'])" 2>/dev/null || echo '')
export HA_TOKEN=$(cat /home/guillaume/.openclaw/secrets/ha-token)
export FIREFLY_URL="http://localhost:8080"
export FIREFLY_TOKEN=$(cat /home/guillaume/.openclaw/secrets/firefly-token)
export INFLUXDB_URL="http://localhost:8086"
export INFLUXDB_TOKEN="health-token-secret"
export INFLUXDB_ORG="health"
export INFLUXDB_BUCKET="apple_health"

# --- Logging ---
mkdir -p "$LOG_DIR" "$RUN_DIR" "$PID_DIR"

# --- Ensure SurrealDB is running ---
docker compose -f "$COREMIND_HOME/docker-compose.yml" up -d surrealdb 2>/dev/null || true

echo "Waiting for SurrealDB..."
for i in $(seq 1 15); do
    if /usr/bin/docker exec coremind-surrealdb-1 /surreal is-ready --endpoint http://localhost:8000 > /dev/null 2>&1; then
        echo "SurrealDB ready"
        break
    fi
    sleep 2
done

# --- Stop any existing processes ---
pkill -f "coremind daemon" 2>/dev/null || true
pkill -f "coremind-plugin-" 2>/dev/null || true
pkill -f "openclaw_side_bridge" 2>/dev/null || true
pkill -f "coremind_watchdog" 2>/dev/null || true
sleep 2

# --- Start daemon ---
cd "$COREMIND_HOME"
nohup env \
  OLLAMA_API_BASE="$OLLAMA_API_BASE" \
  COREMIND_TELEGRAM_BOT_TOKEN="$COREMIND_TELEGRAM_BOT_TOKEN" \
  HA_TOKEN="$HA_TOKEN" \
  $COREMIND_VENV/coremind daemon start > "$LOG_DIR/daemon.log" 2>&1 &
DAEMON_PID=$!
echo "Daemon PID: $DAEMON_PID"
echo "$DAEMON_PID" > "$PID_DIR/daemon.pid"

# --- Wait for daemon socket to be ready (critical: was a race condition) ---
SOCKET_PATH="$RUN_DIR/plugin_host.sock"
echo "Waiting for daemon socket..."
for i in $(seq 1 30); do
    if [ -S "$SOCKET_PATH" ]; then
        echo "Daemon socket ready after ${i}s"
        break
    fi
    sleep 1
done
if [ ! -S "$SOCKET_PATH" ]; then
    echo "⚠️  WARNING: Daemon socket not ready after 30s — plugins may fail to connect"
fi

# --- Start bridge ---
nohup env \
  OLLAMA_API_BASE="$OLLAMA_API_BASE" \
  COREMIND_TELEGRAM_BOT_TOKEN="$COREMIND_TELEGRAM_BOT_TOKEN" \
  $COREMIND_VENV/python3 "$COREMIND_HOME/integrations/openclaw-adapter/openclaw_side_bridge.py" > "$LOG_DIR/bridge.log" 2>&1 &
echo "Bridge PID: $!"

# --- Plugin definitions ---
# Format: "plugin_name:display_name"
PLUGINS=(
    "homeassistant"
    "firefly"
    "openclaw-adapter"
    "weather"
    "vikunja"
    "tapo"
    "webcam"
    "health"
)

# --- Launch function with retry ---
launch_plugin() {
    local plugin=$1
    local log_file="$LOG_DIR/plugin-$plugin.log"
    local pid_file="$PID_DIR/plugin-$plugin.pid"
    local max_retries=5
    local attempt=1

    while [ $attempt -le $max_retries ]; do
        nohup env \
          OLLAMA_API_BASE="$OLLAMA_API_BASE" \
          COREMIND_TELEGRAM_BOT_TOKEN="$COREMIND_TELEGRAM_BOT_TOKEN" \
          HA_TOKEN="$HA_TOKEN" \
          FIREFLY_URL="$FIREFLY_URL" \
          FIREFLY_TOKEN="$FIREFLY_TOKEN" \
          $COREMIND_VENV/coremind-plugin-$plugin >> "$log_file" 2>&1 &
        local pid=$!
        echo "$pid" > "$pid_file"
        
        # Wait a moment and check if it survived
        sleep 2
        if kill -0 "$pid" 2>/dev/null; then
            echo "✅ Plugin $plugin (PID $pid)"
            return 0
        fi
        
        echo "⚠️  Plugin $plugin died immediately (attempt $attempt/$max_retries)"
        attempt=$((attempt + 1))
        sleep 2
    done
    
    echo "❌ Plugin $plugin failed to start after $max_retries attempts"
    return 1
}

# --- Start all plugins with staggered delays ---
echo ""
echo "Starting plugins..."
DELAY=0
for plugin in "${PLUGINS[@]}"; do
    # Stagger startup to avoid thundering herd on daemon socket
    sleep $DELAY
    launch_plugin "$plugin"
    DELAY=1  # 1s between each plugin after the first
done

# --- Background watchdog: auto-restart dead plugins ---
# Runs as a background process that checks every 60s
nohup bash -c '
PID_DIR='"$PID_DIR"'
LOG_DIR='"$LOG_DIR"'
VENV='"$COREMIND_VENV"'
PLUGINS=("${PLUGINS[@]}")

export OLLAMA_API_BASE='"$OLLAMA_API_BASE"'
export COREMIND_TELEGRAM_BOT_TOKEN='"$COREMIND_TELEGRAM_BOT_TOKEN"'
export HA_TOKEN='"$HA_TOKEN"'
export FIREFLY_URL='"$FIREFLY_URL"'
export FIREFLY_TOKEN='"$FIREFLY_TOKEN"'

while true; do
    for plugin in "${PLUGINS[@]}"; do
        pid_file="$PID_DIR/plugin-$plugin.pid"
        if [ -f "$pid_file" ]; then
            pid=$(cat "$pid_file")
            if ! kill -0 "$pid" 2>/dev/null; then
                echo "[$(date -Iseconds)] 🔄 Restarting dead plugin: $plugin" >> "$LOG_DIR/watchdog.log"
                nohup env \
                  OLLAMA_API_BASE="$OLLAMA_API_BASE" \
                  COREMIND_TELEGRAM_BOT_TOKEN="$COREMIND_TELEGRAM_BOT_TOKEN" \
                  HA_TOKEN="$HA_TOKEN" \
                  FIREFLY_URL="$FIREFLY_URL" \
                  FIREFLY_TOKEN="$FIREFLY_TOKEN" \
                  $VENV/coremind-plugin-$plugin >> "$LOG_DIR/plugin-$plugin.log" 2>&1 &
                echo $! > "$pid_file"
            fi
        fi
    done
    sleep 60
done
' > "$LOG_DIR/watchdog.log" 2>&1 &
WATCHDOG_PID=$!
echo "Watchdog PID: $WATCHDOG_PID"
echo "$WATCHDOG_PID" > "$PID_DIR/watchdog.pid"

sleep 3
echo ""
echo "=== Status ==="
"$COREMIND_VENV/coremind" daemon status 2>&1 | head -4
echo ""
echo "Running plugins: $(ps aux | grep -E 'coremind-plugin-[a-z]' | grep -v grep | wc -l)/${#PLUGINS[@]}"
echo ""
echo "Logs: $LOG_DIR/"
echo "Watchdog: active (PID $WATCHDOG_PID, check interval 60s)"
