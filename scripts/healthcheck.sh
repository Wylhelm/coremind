#!/usr/bin/env bash
# CoreMind Health Check — monitors daemon, DB, and notification pipeline
# Exit 0 = healthy, Exit 1 = warning, Exit 2 = critical
# Output lines starting with ALERT: are sent to Telegram

set -euo pipefail

COREMIND_HOME="${COREMIND_HOME:-$HOME/.coremind}"
AUDIT_LOG="$COREMIND_HOME/audit.log"
PID_FILE="$COREMIND_HOME/run/daemon.pid"
MAX_SILENCE_HOURS="${MAX_SILENCE_HOURS:-2}"
ALERT_FILE="/tmp/coremind-health-alerts.txt"

# Clear previous alerts
> "$ALERT_FILE"

alert() { echo "ALERT: $1" | tee -a "$ALERT_FILE"; }
warn()  { echo "WARN: $1" | tee -a "$ALERT_FILE"; }

EXIT_CODE=0

# --- 1. Daemon process ---
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "✅ Daemon running (PID $PID)"
    else
        alert "Daemon process DEAD — PID file exists but process $PID not found"
        EXIT_CODE=2
    fi
else
    alert "Daemon PID file missing — CoreMind may not be running"
    EXIT_CODE=2
fi

# --- 2. SurrealDB ---
if curl -s --max-time 3 http://127.0.0.1:8001/health > /dev/null 2>&1; then
    echo "✅ SurrealDB responding"
else
    # Try docker
    if docker ps --filter name=surrealdb --format '{{.Status}}' 2>/dev/null | grep -q "Up"; then
        warn "SurrealDB container running but /health not responding"
    else
        alert "SurrealDB container NOT running"
        EXIT_CODE=2
    fi
fi

# --- 3. Last notification (silence detection) ---
if [ -f "$AUDIT_LOG" ]; then
    LAST_NOTIF=$(grep "notification.send" "$AUDIT_LOG" | tail -1 | python3 -c "
import sys, json
d = json.loads(sys.stdin.readline())
print(d['timestamp'])
" 2>/dev/null || echo "")

    if [ -n "$LAST_NOTIF" ]; then
        LAST_EPOCH=$(date -d "$LAST_NOTIF" +%s 2>/dev/null || echo 0)
        NOW_EPOCH=$(date +%s)
        SILENCE_MINS=$(( (NOW_EPOCH - LAST_EPOCH) / 60 ))
        SILENCE_HOURS=$(( SILENCE_MINS / 60 ))

        if [ "$SILENCE_MINS" -gt $((MAX_SILENCE_HOURS * 60)) ]; then
            alert "SILENCE: No notification for ${SILENCE_HOURS}h${SILENCE_MINS}m (max ${MAX_SILENCE_HOURS}h)"
            EXIT_CODE=2
        elif [ "$SILENCE_MINS" -gt $((MAX_SILENCE_HOURS * 45)) ]; then
            # Suppress quiet noise warning during night hours (23:00-08:00)
            HOUR=$(date +%H)
            if [ "$HOUR" -ge 23 ] || [ "$HOUR" -lt 8 ]; then
                echo "✅ Last notification: ${SILENCE_MINS}min ago (night hours — suppressed)"
            else
                warn "Quiet: ${SILENCE_MINS}min since last notification (threshold: ${MAX_SILENCE_HOURS}h)"
                [ $EXIT_CODE -eq 0 ] && EXIT_CODE=1
            fi
        else
            echo "✅ Last notification: ${SILENCE_MINS}min ago"
        fi
    else
        warn "No notification.send events in audit log"
        [ $EXIT_CODE -eq 0 ] && EXIT_CODE=1
    fi
else
    alert "Audit log not found at $AUDIT_LOG"
    EXIT_CODE=2
fi

# --- 4. Intention cycle activity ---
# Read the configured interval from config.toml (default 60 min if unreadable).
if [ -f "$COREMIND_HOME/config.toml" ]; then
    EVENT_DRIVEN=$(python3 -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('$COREMIND_HOME/config.toml','rb') as f:
    cfg = tomllib.load(f)
    print('true' if cfg.get('intention',{}).get('event_driven',True) else 'false')
    if cfg.get('intention',{}).get('event_driven',True):
        print(cfg.get('intention',{}).get('routine_interval_seconds',21600))
    else:
        print(cfg.get('intention',{}).get('interval_seconds',600))
" 2>/dev/null || echo -e "true\n21600")
    IS_EVENT_DRIVEN=$(echo "$EVENT_DRIVEN" | head -1)
    INTENTION_INTERVAL_SECONDS=$(echo "$EVENT_DRIVEN" | tail -1)
else
    INTENTION_INTERVAL_SECONDS=21600
fi
INTENTION_INTERVAL_MINS=$(( INTENTION_INTERVAL_SECONDS / 60 ))
# Allow 2x interval before warning (handles startup grace + normal variance).
MAX_INTENT_MINS=$(( INTENTION_INTERVAL_MINS * 2 ))

LAST_INTENT=$(grep -h "intention.cycle.done\|intention.cycle_failed" ~/.coremind/logs/daemon.log 2>/dev/null | tail -1 | grep -oP '\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}' || echo "")
if [ -n "$LAST_INTENT" ]; then
    INTENT_EPOCH=$(date -d "$LAST_INTENT" +%s 2>/dev/null || echo 0)
    INTENT_MINS=$(( (NOW_EPOCH - INTENT_EPOCH) / 60 ))
    if [ "$INTENT_MINS" -gt "$MAX_INTENT_MINS" ]; then
        warn "Intention cycle: ${INTENT_MINS}min since last run (expected every ${INTENTION_INTERVAL_MINS}min)"
    else
        echo "✅ Last intention cycle: ${INTENT_MINS}min ago (interval: ${INTENTION_INTERVAL_MINS}min)"
    fi
else
    warn "No intention cycle activity in daemon log"
fi

# --- 5. Daemon log errors (last 30 min) ---
ERRORS_30M=$(grep -h "\[error" ~/.coremind/logs/daemon.log 2>/dev/null | tail -50 | wc -l || true)
if [ "$ERRORS_30M" -gt 10 ]; then
    warn "High error rate: $ERRORS_30M errors in recent log"
fi

echo ""
echo "Exit code: $EXIT_CODE"
if [ "$EXIT_CODE" -ge 2 ]; then
    echo "STATUS: CRITICAL"
elif [ "$EXIT_CODE" -eq 1 ]; then
    echo "STATUS: WARNING"
else
    echo "STATUS: HEALTHY"
fi
exit $EXIT_CODE
