"""World Data plugin — external data sources for the CoreMind World Model.

Collects crypto prices, traffic conditions, air quality, gas prices, and more.
Emits signed WorldEvents via gRPC to the CoreMind daemon.

Usage: python -m coremind_plugin_worlddata
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
import grpc.aio
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp

from coremind.crypto.signatures import canonical_json, ensure_plugin_keypair, sign
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc

from coremind_plugin_worlddata.collectors import (
    AirQualityCollector,
    CryptoCollector,
    GasPriceCollector,
    TrafficCollector,
)

log = structlog.get_logger(__name__)

PLUGIN_ID = "coremind.plugin.worlddata"
PLUGIN_VERSION = "0.1.0"
KEY_STORE_ID = "coremind_plugin_worlddata"
DEFAULT_SOCKET_PATH = Path.home() / ".coremind" / "run" / "plugin_host.sock"

# Poll intervals (seconds)
CRYPTO_INTERVAL = int(os.environ.get("WORLDDATA_CRYPTO_INTERVAL", "300"))  # 5 min
TRAFFIC_INTERVAL = int(os.environ.get("WORLDDATA_TRAFFIC_INTERVAL", "900"))  # 15 min
AIRQUALITY_INTERVAL = int(os.environ.get("WORLDDATA_AIRQUALITY_INTERVAL", "3600"))  # 1 h
GASPRICE_INTERVAL = int(os.environ.get("WORLDDATA_GASPRICE_INTERVAL", "3600"))  # 1 h


def _build_signed_event(
    key: Ed25519PrivateKey,
    entity_type: str,
    entity_id: str,
    attribute: str,
    value: Any,
    confidence: float,
    unit: str | None = None,
) -> plugin_pb2.WorldEvent:
    """Sign and return a WorldEvent protobuf message."""
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
        confidence=confidence,
    )

    if isinstance(value, (int, float)):
        ev.value.CopyFrom(Value(number_value=float(value)))
    else:
        ev.value.CopyFrom(Value(string_value=str(value)))

    if unit:
        ev.unit = unit

    d = MessageToDict(ev, preserving_proto_field_name=True)
    d.pop("signature", None)
    ev.signature = sign(canonical_json(d), key)
    return ev


async def _emit_events(
    stub: plugin_pb2_grpc.CoreMindHostStub,
    key: Ed25519PrivateKey,
    events: list[dict[str, Any]],
) -> int:
    """Emit a batch of events via gRPC. Returns count of emitted events."""
    meta = (("x-plugin-id", PLUGIN_ID),)
    emitted = 0
    for ev_dict in events:
        try:
            ev = _build_signed_event(
                key,
                entity_type=ev_dict["entity_type"],
                entity_id=ev_dict["entity_id"],
                attribute=ev_dict["attribute"],
                value=ev_dict["value"],
                confidence=ev_dict["confidence"],
                unit=ev_dict.get("unit"),
            )
            await stub.EmitEvent(ev, metadata=meta)
            emitted += 1
        except grpc.RpcError as exc:
            log.warning("worlddata.rpc_error", error=exc.details(), exc_info=False)
            break
        except Exception:
            log.exception("worlddata.emit_failed")
    return emitted


async def run() -> None:
    """Main plugin loop. Survives daemon restarts and connection loss."""
    key = ensure_plugin_keypair(KEY_STORE_ID)
    ch = f"unix://{DEFAULT_SOCKET_PATH}"
    reconnect_delay = 10

    crypto = CryptoCollector()
    traffic = TrafficCollector()
    airquality = AirQualityCollector()
    gasprice = GasPriceCollector()

    log.info("worlddata.starting", plugin_id=PLUGIN_ID)

    while True:
        try:
            async with grpc.aio.insecure_channel(ch) as channel:
                stub = plugin_pb2_grpc.CoreMindHostStub(channel)
                log.info("worlddata.connected", plugin_id=PLUGIN_ID)

                crypto_countdown = 0
                traffic_countdown = 0
                airquality_countdown = 0
                gasprice_countdown = 0

                while True:
                    # --- Crypto ---
                    if crypto_countdown <= 0:
                        try:
                            events = crypto.fetch()
                            if events:
                                emitted = await _emit_events(stub, key, events)
                                if emitted:
                                    log.info("worlddata.crypto_emitted", count=emitted)
                            crypto_countdown = CRYPTO_INTERVAL
                        except Exception:
                            log.exception("worlddata.crypto_failed")

                    # --- Traffic ---
                    if traffic_countdown <= 0:
                        try:
                            events = traffic.fetch()
                            if events:
                                emitted = await _emit_events(stub, key, events)
                                if emitted:
                                    log.info("worlddata.traffic_emitted", count=emitted)
                            traffic_countdown = TRAFFIC_INTERVAL
                        except Exception:
                            log.exception("worlddata.traffic_failed")

                    # --- Air Quality ---
                    if airquality_countdown <= 0:
                        try:
                            events = airquality.fetch()
                            if events:
                                emitted = await _emit_events(stub, key, events)
                                if emitted:
                                    log.info("worlddata.airquality_emitted", count=emitted)
                            airquality_countdown = AIRQUALITY_INTERVAL
                        except Exception:
                            log.exception("worlddata.airquality_failed")

                    # --- Gas Price ---
                    if gasprice_countdown <= 0:
                        try:
                            events = gasprice.fetch()
                            if events:
                                emitted = await _emit_events(stub, key, events)
                                if emitted:
                                    log.info("worlddata.gasprice_emitted", count=emitted)
                            gasprice_countdown = GASPRICE_INTERVAL
                        except Exception:
                            log.exception("worlddata.gasprice_failed")

                    # Sleep in 10-second increments to stay responsive
                    sleep_step = 10
                    await asyncio.sleep(sleep_step)
                    crypto_countdown -= sleep_step
                    traffic_countdown -= sleep_step
                    airquality_countdown -= sleep_step
                    gasprice_countdown -= sleep_step

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worlddata.connection_lost_reconnecting")

        await asyncio.sleep(reconnect_delay)


def main() -> None:
    """Plugin entry point."""
    asyncio.run(run())
