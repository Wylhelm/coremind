"""End-to-end test: in-process OpenClaw half ↔ CoreMind half over gRPC.

Spins up a fake OpenClawHalf gRPC server and the real CoreMindHalfServer on
Unix sockets, then exercises the action-dispatch and event-ingest paths.

Marked as integration because it binds real sockets.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
import grpc.aio
import pytest
import pytest_asyncio
from coremind_plugin_openclaw._generated import adapter_pb2, adapter_pb2_grpc
from coremind_plugin_openclaw.action_dispatcher import ActionDispatcher, PermissionScope
from coremind_plugin_openclaw.openclaw_client import OpenClawGrpcClient
from coremind_plugin_openclaw.server import CoreMindHalfServer, DaemonForwarder
from coremind_plugin_openclaw.translators import sign_event, translate
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf import empty_pb2
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp

from coremind.crypto.signatures import canonical_json, sign
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc

pytestmark = pytest.mark.integration

PLUGIN_ID = "coremind.plugin.openclaw_adapter"
SCHEMA_DIR = Path("integrations/openclaw-adapter/coremind_side/coremind_plugin_openclaw/schemas")


# ---------------------------------------------------------------------------
# Fake OpenClawHalf gRPC server
# ---------------------------------------------------------------------------


class _FakeOpenClawServicer(adapter_pb2_grpc.OpenClawHalfServicer):
    def __init__(self) -> None:
        self.notify_calls: list[adapter_pb2.NotifyRequest] = []

    async def Notify(  # noqa: N802
        self, request: adapter_pb2.NotifyRequest, context: Any
    ) -> adapter_pb2.NotifyResult:
        self.notify_calls.append(request)
        return adapter_pb2.NotifyResult(delivered=True, message_id="m1", error="")

    async def HealthCheck(  # noqa: N802
        self, request: empty_pb2.Empty, context: Any
    ) -> adapter_pb2.Health:
        ts = Timestamp()
        ts.FromDatetime(datetime.now(UTC))
        return adapter_pb2.Health(state=adapter_pb2.HEALTH_STATE_OK, as_of=ts)


# ---------------------------------------------------------------------------
# Fake CoreMindHost (daemon)
# ---------------------------------------------------------------------------


class _FakeCoreMindHost(plugin_pb2_grpc.CoreMindHostServicer):
    def __init__(self) -> None:
        self.events: list[plugin_pb2.WorldEvent] = []

    async def EmitEvent(  # noqa: N802
        self, request: plugin_pb2.WorldEvent, context: Any
    ) -> empty_pb2.Empty:
        self.events.append(request)
        return empty_pb2.Empty()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def openclaw_server(tmp_path: Path) -> AsyncIterator[tuple[str, _FakeOpenClawServicer]]:
    socket = tmp_path / "openclaw.sock"
    address = f"unix://{socket}"
    servicer = _FakeOpenClawServicer()
    server = grpc.aio.server()
    adapter_pb2_grpc.add_OpenClawHalfServicer_to_server(servicer, server)  # type: ignore[no-untyped-call]
    server.add_insecure_port(address)
    await server.start()
    try:
        yield address, servicer
    finally:
        await server.stop(1.0)


@pytest_asyncio.fixture
async def coremind_host(tmp_path: Path) -> AsyncIterator[tuple[Path, _FakeCoreMindHost]]:
    socket = tmp_path / "host.sock"
    address = f"unix://{socket}"
    servicer = _FakeCoreMindHost()
    server = grpc.aio.server()
    plugin_pb2_grpc.add_CoreMindHostServicer_to_server(servicer, server)  # type: ignore[no-untyped-call]
    server.add_insecure_port(address)
    await server.start()
    try:
        yield socket, servicer
    finally:
        await server.stop(1.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_end_to_end(openclaw_server: tuple[str, _FakeOpenClawServicer]) -> None:
    """Dispatch a notify action; fake OpenClaw receives the RPC."""
    address, servicer = openclaw_server
    client = OpenClawGrpcClient(address)
    await client.connect()
    try:
        dispatcher = ActionDispatcher(client=client, scope=PermissionScope(), schema_dir=SCHEMA_DIR)
        out = await dispatcher.dispatch(
            "openclaw.notify",
            {"channel": "telegram", "target": "6394043863", "text": "hello"},
        )
        assert out["delivered"] is True
        assert len(servicer.notify_calls) == 1
        assert servicer.notify_calls[0].channel == "telegram"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_event_roundtrip(
    tmp_path: Path,
    coremind_host: tuple[Path, _FakeCoreMindHost],
) -> None:
    """Signed event → CoreMindHalf → daemon host, verified end-to-end."""
    host_socket, host_servicer = coremind_host
    key = Ed25519PrivateKey.generate()
    half_socket = tmp_path / "half.sock"

    forwarder = DaemonForwarder(
        host_socket=host_socket,
        plugin_id=PLUGIN_ID,
    )
    half = CoreMindHalfServer(
        f"unix://{half_socket}",
        plugin_public_key=key.public_key(),
        plugin_id=PLUGIN_ID,
        forwarder=forwarder,
    )
    await half.start()
    try:
        # Build and sign a message.received event.
        raw = {
            "kind": "message.received",
            "channel": "telegram",
            "chat_id": "telegram:6394043863",
            "sender_id": "6394043863",
            "sender_name": "Guillaume",
            "text": "what's for dinner tonight?",
            "timestamp": "2026-04-19T20:14:02Z",
        }
        event = translate(raw, plugin_id=PLUGIN_ID, plugin_version="0.1.0")
        sign_event(event, key)

        # Push through the CoreMindHalf gRPC boundary.
        channel = grpc.aio.insecure_channel(f"unix://{half_socket}")
        stub = adapter_pb2_grpc.CoreMindHalfStub(channel)  # type: ignore[no-untyped-call]
        try:
            await stub.IngestEvent(event)
        finally:
            await channel.close(grace=None)

        # The daemon host saw it.
        assert len(host_servicer.events) == 1
        forwarded = host_servicer.events[0]
        assert forwarded.entity.type == "conversation"
        assert forwarded.attribute == "message_received"
        # Signature bytes must still be present (forwarded as-is).
        assert forwarded.signature
    finally:
        await half.stop()
        await forwarder.close()


@pytest.mark.asyncio
async def test_ingest_rejects_bad_signature(tmp_path: Path) -> None:
    """CoreMindHalf.IngestEvent must reject a signature from a different key."""
    plugin_key = Ed25519PrivateKey.generate()
    attacker_key = Ed25519PrivateKey.generate()
    half_socket = tmp_path / "half.sock"

    forwarder = DaemonForwarder(
        host_socket=tmp_path / "noop.sock",  # never connected to
        plugin_id=PLUGIN_ID,
    )
    half = CoreMindHalfServer(
        f"unix://{half_socket}",
        plugin_public_key=plugin_key.public_key(),
        plugin_id=PLUGIN_ID,
        forwarder=forwarder,
    )
    await half.start()
    try:
        raw = {
            "kind": "message.received",
            "channel": "telegram",
            "chat_id": "c",
            "sender_id": "u",
            "sender_name": "A",
            "text": "hi",
            "timestamp": "2026-04-19T20:14:02Z",
        }
        event = translate(raw, plugin_id=PLUGIN_ID, plugin_version="0.1.0")
        # Sign with the ATTACKER's key.
        event_dict = MessageToDict(event, preserving_proto_field_name=True)
        event_dict.pop("signature", None)
        event.signature = sign(canonical_json(event_dict), attacker_key)

        channel = grpc.aio.insecure_channel(f"unix://{half_socket}")
        stub = adapter_pb2_grpc.CoreMindHalfStub(channel)  # type: ignore[no-untyped-call]
        try:
            with pytest.raises(grpc.RpcError) as excinfo:
                await stub.IngestEvent(event)
            assert excinfo.value.code() == grpc.StatusCode.UNAUTHENTICATED
        finally:
            await channel.close(grace=None)
    finally:
        await half.stop()
        await forwarder.close()


@pytest.mark.asyncio
async def test_forwarder_raises_unavailable_when_daemon_down(tmp_path: Path) -> None:
    """When the daemon socket is unreachable, forwarding surfaces an RpcError.

    Offline buffering lives on the OpenClaw-side producer now (see
    server.py's module docstring). The Python-side forwarder no longer
    double-buffers on disk; it drops the channel for reconnect and
    propagates the error so the caller can return ``UNAVAILABLE`` promptly.
    """
    forwarder = DaemonForwarder(
        host_socket=tmp_path / "does_not_exist.sock",
        plugin_id=PLUGIN_ID,
    )
    key = Ed25519PrivateKey.generate()
    event = translate(
        {
            "kind": "message.received",
            "channel": "telegram",
            "chat_id": "c",
            "sender_id": "u",
            "sender_name": "A",
            "text": "hi",
            "timestamp": "2026-04-19T20:14:02Z",
        },
        plugin_id=PLUGIN_ID,
        plugin_version="0.1.0",
    )
    sign_event(event, key)
    with pytest.raises(grpc.RpcError):
        await forwarder.forward(event)
    await forwarder.close()
