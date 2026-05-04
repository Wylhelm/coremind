#!/usr/bin/env bash
# CoreMind — Start daemon + all plugins with correct env vars
set -e

COREMIND_HOME=/home/guillaume/.openclaw/workspace/coremind
COREMIND_VENV=$COREMIND_HOME/.venv/bin

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

# --- Stop any existing processes ---
pkill -f "coremind daemon" 2>/dev/null || true
pkill -f "coremind-plugin-" 2>/dev/null || true
pkill -f "openclaw_side_bridge" 2>/dev/null || true
sleep 2

# --- Start daemon ---
cd "$COREMIND_HOME"
nohup $COREMIND_VENV/coremind daemon start > /tmp/coremind-daemon.log 2>&1 &
echo "Daemon PID: $!"

# --- Start bridge ---
nohup $COREMIND_VENV/python3 "$COREMIND_HOME/integrations/openclaw-adapter/openclaw_side_bridge.py" > /tmp/coremind-bridge.log 2>&1 &
echo "Bridge PID: $!"

# --- Start plugins ---
for plugin in homeassistant firefly health openclaw-adapter weather vikunja tapo webcam; do
    nohup $COREMIND_VENV/coremind-plugin-$plugin > /tmp/coremind-plugin-$plugin.log 2>&1 &
    echo "Plugin $plugin PID: $!"
done

sleep 3
echo ""
echo "=== Status ==="
$COREMIND_VENV/coremind daemon status 2>&1 | head -4
echo ""
echo "Running processes: $(ps aux | grep -E 'coremind-plugin' | grep -v grep | wc -l) plugins"
