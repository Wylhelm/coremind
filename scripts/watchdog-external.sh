#!/bin/bash
# CoreMind External Watchdog — surveillance indépendante
# Lancé par cron toutes les 15 minutes
# Ne dépend PAS de CoreMind lui-même — alerte même si tout est crashé
#
# Usage: bash watchdog-external.sh
# Sortie: 0 = OK, 1 = WARN (plugin manquant, auto-réparé), 2 = ALERT (intervention requise)

EXPECTED_PLUGINS=8
MAX_PLUGINS=12  # Above this = duplicate processes, kill and restart
TELEGRAM_CHAT_ID="6394043863"
TELEGRAM_BOT_TOKEN="$(cat /home/guillaume/.coremind/secrets/telegram_bot_token 2>/dev/null || echo '')"
LOG="/home/guillaume/workspace/gbot-logs/coremind-watchdog.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# --- Helper: nuke all coremind processes safely ---
nuke_coremind() {
    log "🧨 Nuking all CoreMind processes..."
    pkill -9 -f "coremind daemon" 2>/dev/null || true
    pkill -9 -f "coremind-plugin-" 2>/dev/null || true
    pkill -9 -f "openclaw_side_bridge" 2>/dev/null || true
    # Also kill the watchdog itself (will be restarted by start-all.sh)
    pkill -f "coremind.*watchdog" 2>/dev/null || true
    sleep 2
}

# --- Check: Daemon alive ---
DAEMON_COUNT=$(ps aux | grep "[c]oremind daemon" | wc -l)
if [ "$DAEMON_COUNT" -eq 0 ]; then
    log "🚨 ALERT: CoreMind daemon is DEAD"
    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
        curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
            -d "chat_id=$TELEGRAM_CHAT_ID" \
            -d "text=🚨 CoreMind daemon est MORT — pas de PID trouvé. Intervention requise." \
            > /dev/null
    fi
    exit 2
fi

# --- Check: Plugins alive (with dedup detection) ---
PLUGIN_COUNT=$(ps aux | grep "[c]oremind-plugin-" | grep -v grep | wc -l)

# 🧹 Dedup check: if WAY too many plugins, we have a zombie situation
if [ "$PLUGIN_COUNT" -gt "$MAX_PLUGINS" ]; then
    log "🚨 CRITICAL: $PLUGIN_COUNT plugins running (max is $MAX_PLUGINS) — ZOMBIE APOCALYPSE!"
    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
        curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
            -d "chat_id=$TELEGRAM_CHAT_ID" \
            -d "text=🧟 CoreMind: $PLUGIN_COUNT plugins détectés (attendu: $EXPECTED_PLUGINS). Nettoyage automatique en cours..." \
            > /dev/null
    fi
    nuke_coremind
    # Restart cleanly
    sleep 3
    bash /home/guillaume/.openclaw/workspace/coremind/scripts/start-all.sh >> /home/guillaume/.coremind/logs/startup.log 2>&1
    log "✅ Full restart triggered after zombie cleanup"
    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
        sleep 10
        NEW_COUNT=$(ps aux | grep "[c]oremind-plugin-" | grep -v grep | wc -l)
        curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
            -d "chat_id=$TELEGRAM_CHAT_ID" \
            -d "text=✅ CoreMind redémarré proprement: $NEW_COUNT/$EXPECTED_PLUGINS plugins actifs." \
            > /dev/null
    fi
    exit 0
fi

# Normal: too few plugins → auto-repair

if [ "$PLUGIN_COUNT" -lt "$EXPECTED_PLUGINS" ]; then
    MISSING=$((EXPECTED_PLUGINS - PLUGIN_COUNT))
    log "⚠️  WARN: $PLUGIN_COUNT/$EXPECTED_PLUGINS plugins running ($MISSING missing)"
    
    # Lister les plugins manquants
    EXPECTED_LIST="homeassistant firefly openclaw-adapter weather vikunja tapo webcam health"
    RUNNING_LIST=$(ps aux | grep "[c]oremind-plugin-" | grep -v grep | awk '{print $NF}' | xargs -I{} basename {} | sed 's/coremind-plugin-//' | sort)
    
    MISSING_LIST=""
    for p in $EXPECTED_LIST; do
        if ! echo "$RUNNING_LIST" | grep -q "^$p$"; then
            MISSING_LIST="$MISSING_LIST $p"
        fi
    done
    
    # Tenter un auto-redémarrage si c'est juste 1-2 plugins
    if [ "$MISSING" -le 2 ]; then
        VENV=/home/guillaume/.openclaw/workspace/coremind/.venv/bin
        export OLLAMA_API_BASE="http://10.0.0.175:11434"
        export COREMIND_TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN"
        export HA_TOKEN="$(cat /home/guillaume/.openclaw/secrets/ha-token 2>/dev/null)"
        export FIREFLY_URL="http://localhost:8080"
        export FIREFLY_TOKEN="$(cat /home/guillaume/.openclaw/secrets/firefly-token 2>/dev/null)"
        export VIKUNJA_URL="http://localhost:3456"
        export VIKUNJA_TOKEN="$(cat /home/guillaume/.openclaw/secrets/vikunja-token 2>/dev/null)"
        export TAPO_USERNAME="$(cat /home/guillaume/.coremind/secrets/camera_username 2>/dev/null || echo 'admin')"
        export TAPO_PASSWORD="$(cat /home/guillaume/.coremind/secrets/camera_password 2>/dev/null || echo '')"
        export TAPO_IP="10.0.0.131"
        export INFLUXDB_URL="http://localhost:8086"
        export INFLUXDB_TOKEN="health-token-secret"
        export INFLUXDB_ORG="health"
        export INFLUXDB_BUCKET="apple_health"
        
        for plugin in $MISSING_LIST; do
            log "🔄 Auto-restarting: $plugin"
            nohup env OLLAMA_API_BASE="$OLLAMA_API_BASE" COREMIND_TELEGRAM_BOT_TOKEN="$COREMIND_TELEGRAM_BOT_TOKEN" HA_TOKEN="$HA_TOKEN" FIREFLY_URL="$FIREFLY_URL" FIREFLY_TOKEN="$FIREFLY_TOKEN" TAPO_USERNAME="$TAPO_USERNAME" TAPO_PASSWORD="$TAPO_PASSWORD" INFLUXDB_URL="$INFLUXDB_URL" INFLUXDB_TOKEN="$INFLUXDB_TOKEN" INFLUXDB_ORG="$INFLUXDB_ORG" INFLUXDB_BUCKET="$INFLUXDB_BUCKET" $VENV/coremind-plugin-$plugin >> ~/.coremind/logs/plugin-$plugin.log 2>&1 &
        done
        sleep 3
        NEW_COUNT=$(ps aux | grep "[c]oremind-plugin-" | grep -v grep | wc -l)
        log "Après repair: $NEW_COUNT/$EXPECTED_PLUGINS plugins"
        
        if [ "$NEW_COUNT" -ge "$EXPECTED_PLUGINS" ]; then
            log "✅ Auto-repair réussi"
            exit 0
        fi
    fi
    
    # Si on arrive ici, l'auto-repair a échoué ou trop de plugins manquants → alerter
    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
        curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
            -d "chat_id=$TELEGRAM_CHAT_ID" \
            -d "text=⚠️ CoreMind: $PLUGIN_COUNT/$EXPECTED_PLUGINS plugins actifs. Manquants:$MISSING_LIST" \
            > /dev/null
    fi
    exit 1
fi

# --- Check: Daemon stale (> 24h old) ---
DAEMON_PID=$(ps aux | grep "[c]oremind daemon" | grep -v grep | awk '{print $2}' | head -1)
if [ -n "$DAEMON_PID" ]; then
    DAEMON_AGE=$(($(date +%s) - $(stat -c %Y /proc/$DAEMON_PID 2>/dev/null || echo 0)))
    if [ "$DAEMON_AGE" -gt 86400 ]; then
        log "⚠️  INFO: Daemon PID $DAEMON_PID is $(($DAEMON_AGE/86400))d old — consider restarting"
    fi
fi

# Tout est OK — log périodique (seulement toutes les 4h pour pas spammer)
LAST_OK=$(stat -c %Y "$LOG" 2>/dev/null || echo 0)
if [ $(( $(date +%s) - LAST_OK )) -gt 14400 ]; then
    log "✅ All good: daemon + $PLUGIN_COUNT/$EXPECTED_PLUGINS plugins running"
fi

exit 0