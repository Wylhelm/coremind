#!/bin/bash
# CoreMind container entrypoint
set -e
echo "🧠 CoreMind starting..."
SURREALDB_HOST="${SURREALDB_HOST:-localhost}"
SURREALDB_PORT="${SURREALDB_PORT:-8001}"
echo "  ⏳ Waiting for SurrealDB at $SURREALDB_HOST:$SURREALDB_PORT..."
for i in $(seq 1 30); do
  curl -s "http://${SURREALDB_HOST}:${SURREALDB_PORT}/health" >/dev/null 2>&1 && break
  sleep 2
done

# Write config
cat > /root/.coremind/config.toml << TOML
world_db_url = "ws://${SURREALDB_HOST}:${SURREALDB_PORT}/rpc"
world_db_username = "root"
world_db_password = "root"
[intention]
enabled = true
interval_seconds = 3600
max_questions = 3
min_salience = 0.25
min_confidence = 0.55
[notify]
primary = "telegram"
[quiet_hours]
enabled = true
timezone = "America/Toronto"
quiet_start = "23:00"
quiet_end = "07:00"
[notify.telegram]
enabled = true
chat_id = "6394043863"
bot_token_secret = "telegram_bot_token"
[llm.reasoning]
model = "ollama/mistral-large-3:675b-cloud"
[llm.intention]
model = "ollama/mistral-large-3:675b-cloud"
TOML

echo "Starting daemon..."
coremind daemon start &
sleep 5
echo "Starting bridge..."
python /app/integrations/openclaw-adapter/openclaw_side_bridge.py &
echo "Starting plugins..."
for p in homeassistant tapo; do
  coremind-plugin-$p &
done
echo "✅ CoreMind ready"
wait
