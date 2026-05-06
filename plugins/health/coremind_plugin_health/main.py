"""Apple Health sensor plugin — queries InfluxDB and emits health metrics.

Polls the InfluxDB apple_health bucket every 5 minutes for the latest
step count, sleep duration, heart rate, and other metrics, then signs
and emits each observation as a WorldEvent to the CoreMind daemon.

Usage::

    python -m coremind_plugin_health
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import grpc
import grpc.aio
import requests
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp

from coremind.crypto.signatures import canonical_json, ensure_plugin_keypair, sign
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc

log = structlog.get_logger(__name__)

PLUGIN_ID: str = "coremind.plugin.health"
PLUGIN_VERSION: str = "0.1.0"
KEY_STORE_ID: str = "coremind_plugin_health"
DEFAULT_SOCKET_PATH: Path = Path.home() / ".coremind" / "run" / "plugin_host.sock"
POLL_INTERVAL_SECONDS: int = 300  # 5 minutes

# InfluxDB config
INFLUX_URL = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
INFLUX_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")
INFLUX_ORG = os.environ.get("INFLUXDB_ORG", "health")
INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "apple_health")

# Metrics to collect: (attribute_name, flux_query, display_unit)
# Time range: last 2h to ensure we catch recent syncs
_METRICS: list[tuple[str, str, str | None]] = [
    (
        "step_count",
        'from(bucket:"{bucket}") |> range(start: -2h) |> filter(fn:(r) => r._measurement == "step_count") |> filter(fn:(r) => r._field == "value") |> last()',
        "steps",
    ),
    (
        "sleep_hours",
        'from(bucket:"{bucket}") |> range(start: -48h) |> filter(fn:(r) => r._measurement == "sleep_analysis") |> filter(fn:(r) => r._field == "totalSleep") |> last()',
        "hours",
    ),
    (
        "heart_rate",
        'from(bucket:"{bucket}") |> range(start: -2h) |> filter(fn:(r) => r._measurement == "heart_rate") |> filter(fn:(r) => r._field == "value") |> last()',
        "bpm",
    ),
    (
        "resting_heart_rate",
        'from(bucket:"{bucket}") |> range(start: -48h) |> filter(fn:(r) => r._measurement == "resting_heart_rate") |> filter(fn:(r) => r._field == "value") |> last()',
        "bpm",
    ),
    (
        "active_energy",
        'from(bucket:"{bucket}") |> range(start: -48h) |> filter(fn:(r) => r._measurement == "active_energy_burned") |> filter(fn:(r) => r._field == "value") |> last()',
        "kcal",
    ),
    (
        "weight",
        'from(bucket:"{bucket}") |> range(start: -30d) |> filter(fn:(r) => r._measurement == "body_mass") |> filter(fn:(r) => r._field == "value") |> last()',
        "kg",
    ),
]

CONFIDENCE: float = 0.92


def _make_timestamp(dt: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def query_influx(flux: str) -> float | None:
    """Execute a Flux query and return the last numeric value, or None."""
    try:
        headers = {
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        }
        resp = requests.post(
            f"{INFLUX_URL}/api/v2/query",
            params={"org": INFLUX_ORG},
            headers=headers,
            data=flux.format(bucket=INFLUX_BUCKET),
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("health.influx_error", status=resp.status_code, body=resp.text[:200])
            return None
        lines = [l for l in resp.text.strip().split("\n") if l and not l.startswith("#")]
        if not lines:
            return None
        # CSV: header row has columns like _time,_value,_field,_measurement
        # We want the _value from the last data row (data rows start at line 1 after header)
        header = lines[0].split(",")
        try:
            val_idx = header.index("_value")
        except ValueError:
            # InfluxDB 3 CSV format may differ; try again without header
            lines = lines[1:]  # Skip annotation row
            if not lines:
                return None
            header = lines[0].split(",")
            val_idx = header.index("_value")
        for line in lines[1:]:  # Skip header
            parts = line.split(",")
            if len(parts) > val_idx:
                return float(parts[val_idx])
        return None
    except Exception as exc:
        log.warning("health.query_error", error=str(exc))
        return None


def build_signed_event(
    private_key: Ed25519PrivateKey,
    attribute: str,
    value: float,
    unit: str | None = None,
) -> plugin_pb2.WorldEvent:
    """Build a signed WorldEvent for a health metric."""
    event_id = uuid.uuid4().hex
    ts = _make_timestamp(datetime.now(UTC))

    unsigned = plugin_pb2.WorldEvent(
        id=event_id,
        timestamp=ts,
        source=PLUGIN_ID,
        source_version=PLUGIN_VERSION,
        signature=b"",
        entity=plugin_pb2.EntityRef(type="person", entity_id="guillaume"),
        attribute=attribute,
        value=Value(number_value=value),
        confidence=CONFIDENCE,
    )
    if unit:
        unsigned.unit = unit

    unsigned_dict = MessageToDict(unsigned, preserving_proto_field_name=True)
    unsigned_dict.pop("signature", None)
    payload = canonical_json(unsigned_dict)
    sig = sign(payload, private_key)
    unsigned.signature = sig
    return unsigned


async def _emit_metrics(
    stub: plugin_pb2_grpc.CoreMindHostStub,
    private_key: Ed25519PrivateKey,
) -> None:
    """Collect all health metrics and emit one WorldEvent each."""
    metadata = (("x-plugin-id", PLUGIN_ID),)
    for attribute, flux_template, unit in _METRICS:
        value = query_influx(flux_template)
        if value is None:
            continue
        event = build_signed_event(private_key, attribute, value, unit)
        try:
            await stub.EmitEvent(event, metadata=metadata)
            log.info("health.emitted", attribute=attribute, value=value)
        except grpc.RpcError as exc:
            log.error("health.emit_failed", attribute=attribute, error=exc.details())
            return


async def run() -> None:
    """Main event loop: query health metrics periodically, emit to CoreMind."""
    private_key = ensure_plugin_keypair(KEY_STORE_ID)
    channel_addr = f"unix://{DEFAULT_SOCKET_PATH}"
    reconnect_delay = 10

    log.info("health.starting", plugin_id=PLUGIN_ID, interval=POLL_INTERVAL_SECONDS)

    # Outer loop: survive daemon restarts and connection loss
    while True:
        try:
            async with grpc.aio.insecure_channel(channel_addr) as channel:
                stub = plugin_pb2_grpc.CoreMindHostStub(channel)
                log.info("health.connected", plugin_id=PLUGIN_ID)

                while True:
                    try:
                        await _emit_metrics(stub, private_key)
                    except grpc.RpcError as exc:
                        log.warning(
                            "health.rpc_error_reconnecting", error=exc.details(), exc_info=False
                        )
                        break

                    await asyncio.sleep(POLL_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("health.connection_lost_reconnecting")

        await asyncio.sleep(reconnect_delay)


def main() -> None:
    """Synchronous entry point."""
    asyncio.run(run())
