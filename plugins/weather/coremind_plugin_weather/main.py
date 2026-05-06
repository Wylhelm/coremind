"""Weather sensor plugin — queries Open-Meteo API and emits weather metrics.

Free API, no key required. Polls every 30 minutes for Quebec City.

Usage: python -m coremind_plugin_weather
"""

from __future__ import annotations
import asyncio, os, uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc, grpc.aio, requests, structlog
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from coremind.crypto.signatures import canonical_json, ensure_plugin_keypair, sign
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc

log = structlog.get_logger(__name__)
PLUGIN_ID = "coremind.plugin.weather"
PLUGIN_VERSION = "0.1.0"
KEY_STORE_ID = "coremind_plugin_weather"
DEFAULT_SOCKET_PATH = Path.home() / ".coremind" / "run" / "plugin_host.sock"
POLL_INTERVAL = int(os.environ.get("WEATHER_POLL_SECONDS", "1800"))
LAT = os.environ.get("WEATHER_LAT", "46.8139")  # Quebec City
LON = os.environ.get("WEATHER_LON", "-71.2080")
CONFIDENCE = 0.9

_METRICS = [
    ("temperature", "temperature_2m", "°C"),
    ("humidity", "relative_humidity_2m", "%"),
    ("apparent_temperature", "apparent_temperature", "°C"),
    ("precipitation_probability", "precipitation_probability", "%"),
    ("wind_speed", "wind_speed_10m", "km/h"),
    ("weather_code", "weather_code", "wmo"),
]


def build_signed_event(key, entity_type, entity_id, attribute, value, unit=None):
    eid = uuid.uuid4().hex
    ts = Timestamp()
    ts.FromDatetime(datetime.now(UTC))
    ev = plugin_pb2.WorldEvent(
        id=eid,
        timestamp=ts,
        source=PLUGIN_ID,
        source_version=PLUGIN_VERSION,
        signature=b"",
        entity=plugin_pb2.EntityRef(type=entity_type, entity_id=entity_id),
        attribute=attribute,
        value=Value(number_value=float(value)),
        confidence=CONFIDENCE,
    )
    if unit:
        ev.unit = unit
    d = MessageToDict(ev, preserving_proto_field_name=True)
    d.pop("signature", None)
    ev.signature = sign(canonical_json(d), key)
    return ev


async def run():
    key = ensure_plugin_keypair(KEY_STORE_ID)
    ch = f"unix://{DEFAULT_SOCKET_PATH}"
    reconnect_delay = 10

    log.info("weather.starting", plugin_id=PLUGIN_ID)

    # Outer loop: survive daemon restarts and connection loss
    while True:
        try:
            async with grpc.aio.insecure_channel(ch) as channel:
                stub = plugin_pb2_grpc.CoreMindHostStub(channel)
                meta = (("x-plugin-id", PLUGIN_ID),)
                log.info("weather.connected", plugin_id=PLUGIN_ID)

                while True:
                    try:
                        url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&current={','.join(m[1] for m in _METRICS)}"
                        r = requests.get(url, timeout=10)
                        data = r.json().get("current", {})
                        for attr, key_name, unit in _METRICS:
                            v = data.get(key_name)
                            if v is not None:
                                ev = build_signed_event(key, "location", "quebec", attr, v, unit)
                                await stub.EmitEvent(ev, metadata=meta)
                                log.info("weather.emitted", attribute=attr, value=v)
                    except grpc.RpcError as exc:
                        log.warning(
                            "weather.rpc_error_reconnecting", error=exc.details(), exc_info=False
                        )
                        break
                    except Exception as e:
                        log.warning("weather.error", error=str(e))

                    await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("weather.connection_lost_reconnecting")

        await asyncio.sleep(reconnect_delay)


def main():
    asyncio.run(run())
