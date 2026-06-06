#!/usr/bin/env python3
"""Quick sleep query for CoreMind presence detector.
Returns last night's totalSleep hours, or None if unavailable."""
import os, sys, json, requests
from datetime import datetime, timedelta, timezone

INFLUX_URL = "http://localhost:8086"
INFLUX_TOKEN = os.environ.get("INFLUXDB_TOKEN", "health-token-secret")
ORG = "health"
BUCKET = "apple_health"

query = f'''from(bucket: "{BUCKET}")
  |> range(start: -48h)
  |> filter(fn: (r) => r._measurement == "sleep_analysis")
  |> filter(fn: (r) => r._field == "totalSleep")
  |> last()'''

resp = requests.post(
    f"{INFLUX_URL}/api/v2/query",
    params={"org": ORG},
    headers={"Authorization": f"Token {INFLUX_TOKEN}", "Accept": "application/csv", "Content-Type": "application/vnd.flux"},
    data=query.encode(),
    timeout=15,
)

if resp.status_code != 200:
    print(json.dumps({"hours": None, "error": f"HTTP {resp.status_code}"}))
    sys.exit(0)

lines = [l for l in resp.text.strip().split("\n") if l and not l.startswith("#")]
if len(lines) < 2:
    print(json.dumps({"hours": None, "error": "no_data"}))
    sys.exit(0)

# Parse CSV: find _value column
header = lines[0].split(",")
val_idx = next((i for i, col in enumerate(header) if col.strip() == "_value"), None)
time_idx = next((i for i, col in enumerate(header) if col.strip() == "_time"), None)

data = lines[-1].split(",")
value = float(data[val_idx]) if val_idx is not None and val_idx < len(data) else None
ts = data[time_idx].strip() if time_idx is not None and time_idx < len(data) else None

# Check freshness: must be within last 48h
if ts:
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    age_h = (datetime.now(timezone.utc) - t).total_seconds() / 3600
    if age_h > 48:
        print(json.dumps({"hours": None, "error": f"stale_data_{age_h:.0f}h"}))
        sys.exit(0)

print(json.dumps({"hours": round(value, 1) if value else None}))
