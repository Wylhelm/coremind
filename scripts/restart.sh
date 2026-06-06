#!/bin/bash
# CoreMind restart script
set -e

COREMIND_DIR="$HOME/.openclaw/workspace/coremind"
cd "$COREMIND_DIR"

# Kill existing processes
echo "Killing old processes..."
kill $(cat ~/.coremind/run/daemon.pid 2>/dev/null) 2>/dev/null || true
pkill -f "coremind-plugin" 2>/dev/null || true
pkill -f "openclaw_side_bridge" 2>/dev/null || true
sleep 3

# Start daemon
echo "Starting daemon..."
rm -f ~/.coremind/run/daemon.pid
export OLLAMA_API_BASE=http://10.0.0.175:11434
export COREMIND_TELEGRAM_BOT_TOKEN="$(cat ~/.openclaw/secrets/coremind-telegram-token 2>/dev/null || echo 'MISSING')"
nohup .venv/bin/coremind daemon start >> /tmp/coremind-daemon.log 2>&1 &
sleep 6

# Start bridge + all plugins
echo "Starting plugins..."
export HA_TOKEN="$(cat ~/.openclaw/secrets/ha-token)"
export FIREFLY_TOKEN="$(cat ~/.openclaw/secrets/firefly-token)"
export FIREFLY_URL=http://localhost:8080
export INFLUXDB_TOKEN="health-token-secret"
source ~/.openclaw/secrets/tapo-credentials
export TAPO_USERNAME TAPO_PASSWORD TAPO_IP

VENV=.venv/bin
nohup $VENV/python3.12 integrations/openclaw-adapter/openclaw_side_bridge.py >> ~/.coremind/logs/bridge.log 2>&1 &

for plugin in homeassistant firefly weather vikunja tapo health gog openclaw-adapter webcam worlddata; do
    nohup $VENV/coremind-plugin-$plugin >> ~/.coremind/logs/plugin-$plugin.log 2>&1 &
    echo "  started $plugin"
done

sleep 3
echo "RESTART COMPLETE"
