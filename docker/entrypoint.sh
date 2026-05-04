#!/bin/bash
# CoreMind container entrypoint — starts daemon + plugins + bridge
set -e

echo "🧠 CoreMind starting..."

# Wait for SurrealDB
echo "  ⏳ Waiting for SurrealDB..."
for i in $(seq 1 30); do
  curl -s http://surrealdb:8000/health >/dev/null 2>&1 && break
  sleep 2
done

# Config
cat > /root/.coremind/config.toml << 'TOML'
world_db_url = "ws://surrealdb:8000/rpc"
world_db_username = "root"
world_db_password = "root"

[intention]
enabled = true
interval_seconds = 3600
max_questions = 3
min_salience = 0.25
min_confidence = 0.55

[action]
suggest_grace_seconds = 30
approval_ttl_seconds = 86400

[dashboard]
enabled = false

[llm]
[llm.reasoning]
model = "ollama/mistral-large-3:675b-cloud"
[llm.intention]
model = "ollama/mistral-large-3:675b-cloud"
[llm.reflection]
model = "ollama/deepseek-v4-flash:cloud"

[llm.embedding]
model = "nomic-embed-text"
provider = "ollama"
url = "${OLLAMA_API_BASE:-http://host.docker.internal:11434}"

[notify]
primary = "telegram"

[quiet_hours]
enabled = true
timezone = "${TZ:-America/Toronto}"
quiet_start = "23:00"
quiet_end = "07:00"

[notify.telegram]
enabled = true
chat_id = "${COREMIND_TELEGRAM_CHAT_ID:-6394043863}"
bot_token_secret = "telegram_bot_token"
TOML

# Start daemon
echo "  🚀 Starting daemon..."
python -m coremind.cli daemon start &

# Wait for daemon socket
for i in $(seq 1 20); do
  [ -S /root/.coremind/run/plugin_host.sock ] && break
  sleep 1
done

# Start bridge
echo "  🌉 Starting OpenClaw bridge..."
python integrations/openclaw-adapter/openclaw_side_bridge.py &

# Start plugins (only the ones with secrets configured)
for plugin in homeassistant firefly health vikunja weather tapo; do
  if [ -n "${HA_TOKEN:-}" ] || [ "$plugin" != "homeassistant" ]; then
    echo "  📡 Starting plugin: $plugin"
    python -m "coremind_plugin_${plugin}" &
  fi
done

echo "✅ CoreMind ready"

# Keep container alive
wait
