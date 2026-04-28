"""CoreMind system statistics sensor plugin — main module.

Connects to the CoreMind daemon's ``CoreMindHost`` Unix socket and emits
signed ``WorldEvent`` messages for CPU usage, memory usage, and system uptime
every 30 seconds.

Usage::

    python -m coremind_plugin_systemstats
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path

import grpc
import grpc.aio
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp

from coremind.crypto.signatures import canonical_json, ensure_plugin_keypair, sign
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc
from coremind_plugin_systemstats.collector import (
    collect_cpu_percent,
    collect_memory_percent,
    collect_uptime_seconds,
)

log = structlog.get_logger(__name__)

PLUGIN_ID: str = "coremind.plugin.systemstats"
PLUGIN_VERSION: str = "0.1.0"

# Key store identifier: must match [a-zA-Z0-9_-]+ for filesystem path safety.
KEY_STORE_ID: str = "coremind_plugin_systemstats"

EMIT_INTERVAL_SECONDS: int = 30
DEFAULT_SOCKET_PATH: Path = Path.home() / ".coremind" / "run" / "plugin_host.sock"

# 0.95 reflects a direct sensor reading with minor measurement jitter.
CONFIDENCE: float = 0.95


def _make_timestamp(dt: datetime) -> Timestamp:
    """Convert a timezone-aware datetime to a protobuf Timestamp.

    Args:
        dt: A timezone-aware datetime instance.

    Returns:
        A populated ``google.protobuf.Timestamp`` message.
    """
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def build_signed_event(
    private_key: Ed25519PrivateKey,
    attribute: str,
    value: float | int,
    hostname: str,
) -> plugin_pb2.WorldEvent:
    """Build a signed WorldEvent proto for a numeric host observation.

    Constructs the event payload without a signature, serialises it to RFC 8785
    (JCS) canonical JSON, signs the bytes with *private_key*, then returns a
    new message with the signature attached.

    The signing payload is identical to what :class:`PluginHostServer` verifies:
    ``MessageToDict(event, preserving_proto_field_name=True)`` minus the
    ``signature`` field, JCS-canonicalised.

    Args:
        private_key: Plugin's ed25519 private key.
        attribute: Attribute name (e.g. ``"cpu_percent"``).
        value: Observed numeric value.
        hostname: Local hostname used as the entity ``entity_id``.

    Returns:
        A :class:`plugin_pb2.WorldEvent` with a valid ed25519 signature.
    """
    event_id = uuid.uuid4().hex
    ts = _make_timestamp(datetime.now(UTC))

    # Build unsigned event; signature=b"" so MessageToDict omits the field.
    unsigned = plugin_pb2.WorldEvent(
        id=event_id,
        timestamp=ts,
        source=PLUGIN_ID,
        source_version=PLUGIN_VERSION,
        signature=b"",
        entity=plugin_pb2.EntityRef(type="host", entity_id=hostname),
        attribute=attribute,
        value=Value(number_value=float(value)),
        confidence=CONFIDENCE,
    )
    unsigned_dict = MessageToDict(unsigned, preserving_proto_field_name=True)
    unsigned_dict.pop("signature", None)
    payload = canonical_json(unsigned_dict)
    sig = sign(payload, private_key)

    # Mutate in place — avoids duplicating the field list and prevents drift.
    unsigned.signature = sig
    return unsigned


async def _emit_stats(
    stub: plugin_pb2_grpc.CoreMindHostStub,
    private_key: Ed25519PrivateKey,
    hostname: str,
) -> None:
    """Collect system statistics and emit one full cycle of WorldEvents.

    Gathers CPU percent (blocking), memory percent, and uptime, then calls
    ``EmitEvent`` on the daemon stub for each metric.

    Args:
        stub: gRPC stub connected to the daemon's CoreMindHost service.
        private_key: Plugin's ed25519 private key for signing.
        hostname: Local hostname identifying the observed host entity.
    """
    metadata = (("x-plugin-id", PLUGIN_ID),)
    loop = asyncio.get_event_loop()

    # cpu_percent blocks for 1 second; run in executor to avoid blocking the loop.
    cpu = await loop.run_in_executor(None, collect_cpu_percent)
    mem = collect_memory_percent()  # No executor — sub-ms /proc read.
    uptime = collect_uptime_seconds()  # No executor — sub-ms /proc read.

    observations: tuple[tuple[str, float | int], ...] = (
        ("cpu_percent", cpu),
        ("memory_percent", mem),
        ("uptime_seconds", uptime),
    )
    for attribute, obs_value in observations:
        event = build_signed_event(private_key, attribute, obs_value, hostname)
        await stub.EmitEvent(event, metadata=metadata)
        log.info("systemstats.emitted", attribute=attribute, value=obs_value)


async def run(
    socket_path: Path = DEFAULT_SOCKET_PATH,
    interval_seconds: int = EMIT_INTERVAL_SECONDS,
) -> None:
    """Connect to the CoreMind daemon and emit system statistics on a loop.

    Blocks indefinitely until cancelled.  On each iteration the plugin:

    1. Collects CPU%, memory%, and uptime from the host.
    2. Signs each observation with its ed25519 private key.
    3. Calls ``CoreMindHost.EmitEvent`` on the daemon's Unix socket.

    Transient RPC errors are logged and retried on the next interval.

    Args:
        socket_path: Filesystem path to the daemon's CoreMindHost Unix socket.
        interval_seconds: Seconds between emission cycles.
    """
    private_key = ensure_plugin_keypair(KEY_STORE_ID)
    hostname = socket.gethostname()
    channel_addr = f"unix://{socket_path}"

    log.info(
        "systemstats.starting",
        plugin_id=PLUGIN_ID,
        socket=str(socket_path),
        interval=interval_seconds,
    )

    async with grpc.aio.insecure_channel(channel_addr) as channel:
        stub = plugin_pb2_grpc.CoreMindHostStub(channel)  # type: ignore[no-untyped-call]  # generated gRPC stub
        log.info("systemstats.connected", hostname=hostname)

        while True:
            try:
                await _emit_stats(stub, private_key, hostname)
            except grpc.RpcError as exc:
                log.error(
                    "systemstats.rpc_error",
                    code=exc.code().name,
                    details=exc.details(),
                )
            except Exception:
                log.exception("systemstats.unexpected_error")
                raise
            await asyncio.sleep(interval_seconds)


def main() -> None:
    """Synchronous entry point for the plugin process."""
    asyncio.run(run())
