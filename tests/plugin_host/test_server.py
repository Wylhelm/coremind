"""Tests for the plugin host gRPC server (src/coremind/plugin_host/server.py)."""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf import empty_pb2, struct_pb2
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp

from coremind.core.event_bus import EventBus
from coremind.crypto.signatures import canonical_json, sign
from coremind.plugin_api._generated import plugin_pb2
from coremind.plugin_host.registry import PluginRegistry
from coremind.plugin_host.server import (
    PluginHostServer,
    _CoreMindHostServicer,
    _proto_event_to_record,
    _proto_value_to_python,
)
from coremind.world.model import WorldEventRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLUGIN_ID = "coremind.plugin.test"
_PLUGIN_VERSION = "1.0.0"


def _make_context(plugin_id: str = _PLUGIN_ID) -> MagicMock:
    """Return a mock gRPC servicer context with async abort and x-plugin-id metadata."""
    ctx = MagicMock()
    ctx.abort = AsyncMock(side_effect=Exception("aborted"))
    ctx.invocation_metadata = MagicMock(return_value=[("x-plugin-id", plugin_id)])
    return ctx


def _make_no_auth_context() -> MagicMock:
    """Return a mock context without x-plugin-id metadata (unauthenticated)."""
    ctx = MagicMock()
    ctx.abort = AsyncMock(side_effect=Exception("aborted"))
    ctx.invocation_metadata = MagicMock(return_value=[])
    return ctx


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def event_bus() -> EventBus:
    """Return a fresh EventBus."""
    return EventBus()


@pytest.fixture()
def registry(private_key: Ed25519PrivateKey) -> PluginRegistry:
    """Return a fresh PluginRegistry with one registered plugin (with its key)."""
    reg = PluginRegistry()
    manifest = plugin_pb2.PluginManifest(
        plugin_id=_PLUGIN_ID,
        version=_PLUGIN_VERSION,
        display_name="Test Plugin",
        kind=plugin_pb2.PLUGIN_KIND_SENSOR,
        provides_entities=["host"],
        emits_attributes=["cpu_percent"],
        required_permissions=["secrets:api_token"],
    )
    reg.register(manifest, private_key.public_key())
    return reg


def _no_secrets(name: str) -> str | None:
    """Secrets resolver that always returns None."""
    return None


def _fixed_secrets(name: str) -> str | None:
    """Secrets resolver that returns a fixed value for 'api_token'."""
    if name == "api_token":
        return "test-secret-value"
    return None


@pytest.fixture()
def servicer(
    registry: PluginRegistry,
    event_bus: EventBus,
    private_key: Ed25519PrivateKey,
) -> _CoreMindHostServicer:
    """Return a servicer backed by a registry that holds *private_key*'s public key."""
    return _CoreMindHostServicer(
        registry=registry,
        event_bus=event_bus,
        secrets_resolver=_no_secrets,
    )


def _make_signed_proto_event(
    private_key: Ed25519PrivateKey,
    event_id: str | None = None,
    source: str = _PLUGIN_ID,
    attribute: str = "cpu_percent",
    value: float = 42.0,
) -> plugin_pb2.WorldEvent:
    """Build a WorldEvent with a valid ed25519 signature over its canonical form."""
    ts = Timestamp()
    ts.FromDatetime(datetime.now(UTC))
    event_id = event_id or uuid.uuid4().hex

    # Build the event without a signature first to compute the canonical payload.
    unsigned = plugin_pb2.WorldEvent(
        id=event_id,
        timestamp=ts,
        source=source,
        source_version=_PLUGIN_VERSION,
        signature=b"",
        entity=plugin_pb2.EntityRef(type="host", entity_id="testhost"),
        attribute=attribute,
        value=struct_pb2.Value(number_value=value),
        confidence=0.9,
    )
    unsigned_dict = MessageToDict(unsigned, preserving_proto_field_name=True)
    unsigned_dict.pop("signature", None)
    payload = canonical_json(unsigned_dict)
    sig = sign(payload, private_key)

    return plugin_pb2.WorldEvent(
        id=event_id,
        timestamp=ts,
        source=source,
        source_version=_PLUGIN_VERSION,
        signature=sig,
        entity=plugin_pb2.EntityRef(type="host", entity_id="testhost"),
        attribute=attribute,
        value=struct_pb2.Value(number_value=value),
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# _proto_value_to_python
# ---------------------------------------------------------------------------


def test_proto_value_null() -> None:
    """Null protobuf Value converts to None."""
    v = struct_pb2.Value(null_value=struct_pb2.NullValue.NULL_VALUE)
    assert _proto_value_to_python(v) is None


def test_proto_value_number() -> None:
    """Number protobuf Value converts to float."""
    v = struct_pb2.Value(number_value=3.14)
    assert _proto_value_to_python(v) == pytest.approx(3.14)


def test_proto_value_string() -> None:
    """String protobuf Value converts to str."""
    v = struct_pb2.Value(string_value="hello")
    assert _proto_value_to_python(v) == "hello"


def test_proto_value_bool() -> None:
    """Boolean protobuf Value converts to bool."""
    v = struct_pb2.Value(bool_value=True)
    assert _proto_value_to_python(v) is True


# ---------------------------------------------------------------------------
# _proto_event_to_record
# ---------------------------------------------------------------------------


def test_proto_event_to_record_basic(private_key: Ed25519PrivateKey) -> None:
    """Proto WorldEvent is correctly converted to a WorldEventRecord."""
    proto = _make_signed_proto_event(private_key)
    record = _proto_event_to_record(proto)
    assert isinstance(record, WorldEventRecord)
    assert record.id == proto.id
    assert record.source == _PLUGIN_ID
    assert record.attribute == "cpu_percent"
    assert record.entity.type == "host"
    assert record.entity.id == "testhost"
    assert record.confidence == pytest.approx(0.9)
    assert record.signature is not None
    # signature must be base64-encoded
    base64.b64decode(record.signature)


def test_proto_event_to_record_empty_signature() -> None:
    """When signature bytes are empty, record.signature is None."""
    ts = Timestamp()
    ts.FromDatetime(datetime.now(UTC))
    proto = plugin_pb2.WorldEvent(
        id=uuid.uuid4().hex,
        timestamp=ts,
        source=_PLUGIN_ID,
        source_version=_PLUGIN_VERSION,
        signature=b"",
        entity=plugin_pb2.EntityRef(type="host", entity_id="h1"),
        attribute="cpu_percent",
        value=struct_pb2.Value(number_value=10.0),
        confidence=1.0,
    )
    record = _proto_event_to_record(proto)
    assert record.signature is None


def test_proto_event_timestamp_is_utc(private_key: Ed25519PrivateKey) -> None:
    """The converted timestamp is timezone-aware (UTC)."""
    proto = _make_signed_proto_event(private_key)
    record = _proto_event_to_record(proto)
    assert record.timestamp.tzinfo is UTC


# ---------------------------------------------------------------------------
# RequestSecret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_secret_not_found(registry: PluginRegistry, event_bus: EventBus) -> None:
    """RequestSecret with an unknown secret name aborts with NOT_FOUND."""
    svc = _CoreMindHostServicer(registry, event_bus, _no_secrets)
    req = plugin_pb2.SecretRequest(secret_name="api_token")  # noqa: S106 — declared in permissions
    ctx = _make_context()

    with pytest.raises(Exception, match="aborted"):
        await svc.RequestSecret(req, ctx)

    ctx.abort.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_secret_empty_name_aborts(
    registry: PluginRegistry, event_bus: EventBus
) -> None:
    """RequestSecret with empty secret_name aborts with INVALID_ARGUMENT."""
    svc = _CoreMindHostServicer(registry, event_bus, _no_secrets)
    req = plugin_pb2.SecretRequest(secret_name="")
    ctx = _make_context()

    with pytest.raises(Exception, match="aborted"):
        await svc.RequestSecret(req, ctx)

    ctx.abort.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_secret_no_plugin_id_aborts(
    registry: PluginRegistry, event_bus: EventBus
) -> None:
    """RequestSecret without x-plugin-id metadata aborts with UNAUTHENTICATED."""
    svc = _CoreMindHostServicer(registry, event_bus, _no_secrets)
    req = plugin_pb2.SecretRequest(secret_name="api_token")  # noqa: S106
    ctx = _make_no_auth_context()

    with pytest.raises(Exception, match="aborted"):
        await svc.RequestSecret(req, ctx)

    ctx.abort.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_secret_unregistered_plugin_aborts(
    registry: PluginRegistry, event_bus: EventBus
) -> None:
    """RequestSecret from an unregistered plugin_id aborts with UNAUTHENTICATED."""
    svc = _CoreMindHostServicer(registry, event_bus, _fixed_secrets)
    req = plugin_pb2.SecretRequest(secret_name="api_token")  # noqa: S106
    ctx = _make_context(plugin_id="coremind.plugin.unknown")

    with pytest.raises(Exception, match="aborted"):
        await svc.RequestSecret(req, ctx)

    ctx.abort.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_secret_undeclared_permission_aborts(
    event_bus: EventBus,
) -> None:
    """RequestSecret for a secret not in required_permissions aborts with PERMISSION_DENIED."""
    reg = PluginRegistry()
    priv = Ed25519PrivateKey.generate()
    manifest = plugin_pb2.PluginManifest(
        plugin_id=_PLUGIN_ID,
        version=_PLUGIN_VERSION,
        display_name="Test Plugin",
        kind=plugin_pb2.PLUGIN_KIND_SENSOR,
        provides_entities=["host"],
        emits_attributes=["cpu_percent"],
        required_permissions=[],  # no secrets declared
    )
    reg.register(manifest, priv.public_key())
    svc = _CoreMindHostServicer(reg, event_bus, _fixed_secrets)
    req = plugin_pb2.SecretRequest(secret_name="api_token")  # noqa: S106
    ctx = _make_context()

    with pytest.raises(Exception, match="aborted"):
        await svc.RequestSecret(req, ctx)

    ctx.abort.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_secret_found(registry: PluginRegistry, event_bus: EventBus) -> None:
    """RequestSecret returns the plaintext value when the secret exists and is declared."""
    svc = _CoreMindHostServicer(registry, event_bus, _fixed_secrets)
    req = plugin_pb2.SecretRequest(secret_name="api_token")  # noqa: S106
    ctx = _make_context()

    result = await svc.RequestSecret(req, ctx)
    assert result.secret_value == "test-secret-value"  # noqa: S105


# ---------------------------------------------------------------------------
# EmitEvent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_event_publishes_to_bus(
    servicer: _CoreMindHostServicer,
    event_bus: EventBus,
    private_key: Ed25519PrivateKey,
) -> None:
    """EmitEvent causes the converted record to appear on the EventBus."""
    records: list[WorldEventRecord] = []
    subscription = event_bus.subscribe()

    async def _consume() -> None:
        async for record in subscription:
            records.append(record)
            return

    consumer_task = asyncio.create_task(_consume())
    proto = _make_signed_proto_event(private_key)
    ctx = _make_context()
    result = await servicer.EmitEvent(proto, ctx)
    assert isinstance(result, empty_pb2.Empty)
    await asyncio.wait_for(consumer_task, timeout=1.0)
    assert len(records) == 1
    assert records[0].id == proto.id


@pytest.mark.asyncio
async def test_emit_event_increments_count(
    servicer: _CoreMindHostServicer,
    registry: PluginRegistry,
    private_key: Ed25519PrivateKey,
) -> None:
    """EmitEvent increments the plugin's event_count in the registry."""
    proto = _make_signed_proto_event(private_key)
    ctx = _make_context()
    await servicer.EmitEvent(proto, ctx)
    info = registry.get_info(_PLUGIN_ID)
    assert info is not None
    assert info.event_count == 1


@pytest.mark.asyncio
async def test_emit_event_missing_id_aborts(servicer: _CoreMindHostServicer) -> None:
    """EmitEvent with empty id aborts with INVALID_ARGUMENT."""
    ts = Timestamp()
    ts.FromDatetime(datetime.now(UTC))
    proto = plugin_pb2.WorldEvent(
        id="",
        timestamp=ts,
        source=_PLUGIN_ID,
        source_version=_PLUGIN_VERSION,
        entity=plugin_pb2.EntityRef(type="host", entity_id="h1"),
        attribute="cpu_percent",
        value=struct_pb2.Value(number_value=1.0),
        confidence=1.0,
    )
    ctx = _make_context()
    with pytest.raises(Exception, match="aborted"):
        await servicer.EmitEvent(proto, ctx)


@pytest.mark.asyncio
async def test_emit_event_missing_source_aborts(servicer: _CoreMindHostServicer) -> None:
    """EmitEvent with empty source aborts with INVALID_ARGUMENT."""
    ts = Timestamp()
    ts.FromDatetime(datetime.now(UTC))
    proto = plugin_pb2.WorldEvent(
        id=uuid.uuid4().hex,
        timestamp=ts,
        source="",
        source_version=_PLUGIN_VERSION,
        entity=plugin_pb2.EntityRef(type="host", entity_id="h1"),
        attribute="cpu_percent",
        value=struct_pb2.Value(number_value=1.0),
        confidence=1.0,
    )
    ctx = _make_context()
    with pytest.raises(Exception, match="aborted"):
        await servicer.EmitEvent(proto, ctx)


@pytest.mark.asyncio
async def test_emit_event_missing_attribute_aborts(servicer: _CoreMindHostServicer) -> None:
    """EmitEvent with empty attribute aborts with INVALID_ARGUMENT."""
    ts = Timestamp()
    ts.FromDatetime(datetime.now(UTC))
    proto = plugin_pb2.WorldEvent(
        id=uuid.uuid4().hex,
        timestamp=ts,
        source=_PLUGIN_ID,
        source_version=_PLUGIN_VERSION,
        entity=plugin_pb2.EntityRef(type="host", entity_id="h1"),
        attribute="",
        value=struct_pb2.Value(number_value=1.0),
        confidence=1.0,
    )
    ctx = _make_context()
    with pytest.raises(Exception, match="aborted"):
        await servicer.EmitEvent(proto, ctx)


@pytest.mark.asyncio
async def test_emit_event_unregistered_source_aborts(
    servicer: _CoreMindHostServicer,
) -> None:
    """EmitEvent from an unregistered source plugin aborts with UNAUTHENTICATED."""
    ts = Timestamp()
    ts.FromDatetime(datetime.now(UTC))
    priv = Ed25519PrivateKey.generate()
    proto = plugin_pb2.WorldEvent(
        id=uuid.uuid4().hex,
        timestamp=ts,
        source="coremind.plugin.unknown",
        source_version="1.0.0",
        signature=priv.sign(b"x"),
        entity=plugin_pb2.EntityRef(type="host", entity_id="h1"),
        attribute="cpu_percent",
        value=struct_pb2.Value(number_value=1.0),
        confidence=1.0,
    )
    ctx = _make_context()
    with pytest.raises(Exception, match="aborted"):
        await servicer.EmitEvent(proto, ctx)


@pytest.mark.asyncio
async def test_emit_event_bad_signature_aborts(
    servicer: _CoreMindHostServicer,
) -> None:
    """EmitEvent with an invalid signature aborts with UNAUTHENTICATED."""
    ts = Timestamp()
    ts.FromDatetime(datetime.now(UTC))
    wrong_key = Ed25519PrivateKey.generate()
    sig = wrong_key.sign(b"wrong")
    proto = plugin_pb2.WorldEvent(
        id=uuid.uuid4().hex,
        timestamp=ts,
        source=_PLUGIN_ID,
        source_version=_PLUGIN_VERSION,
        signature=sig,
        entity=plugin_pb2.EntityRef(type="host", entity_id="h1"),
        attribute="cpu_percent",
        value=struct_pb2.Value(number_value=1.0),
        confidence=1.0,
    )
    ctx = _make_context()
    with pytest.raises(Exception, match="aborted"):
        await servicer.EmitEvent(proto, ctx)


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_rpc_returns_empty(servicer: _CoreMindHostServicer) -> None:
    """Log RPC returns Empty and does not raise."""
    ts = Timestamp()
    ts.FromDatetime(datetime.now(UTC))
    entry = plugin_pb2.LogEntry(
        timestamp=ts,
        level=plugin_pb2.LOG_LEVEL_INFO,
        message="hello from plugin",
    )
    ctx = _make_context()
    result = await servicer.Log(entry, ctx)
    assert isinstance(result, empty_pb2.Empty)


@pytest.mark.asyncio
async def test_log_rpc_all_levels_do_not_raise(servicer: _CoreMindHostServicer) -> None:
    """Log RPC accepts all defined log levels without raising."""
    ts = Timestamp()
    ts.FromDatetime(datetime.now(UTC))
    for level in (
        plugin_pb2.LOG_LEVEL_DEBUG,
        plugin_pb2.LOG_LEVEL_INFO,
        plugin_pb2.LOG_LEVEL_WARNING,
        plugin_pb2.LOG_LEVEL_ERROR,
        plugin_pb2.LOG_LEVEL_CRITICAL,
    ):
        entry = plugin_pb2.LogEntry(timestamp=ts, level=level, message="msg")
        ctx = _make_context()
        result = await servicer.Log(entry, ctx)
        assert isinstance(result, empty_pb2.Empty)


# ---------------------------------------------------------------------------
# PluginHostServer lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_host_server_start_creates_socket(
    tmp_path: Path,
    registry: PluginRegistry,
    event_bus: EventBus,
) -> None:
    """PluginHostServer.start() creates a Unix socket at the configured path."""
    socket_path = tmp_path / "run" / "plugin_host.sock"
    server = PluginHostServer(
        socket_path=socket_path,
        registry=registry,
        event_bus=event_bus,
        secrets_resolver=_no_secrets,
    )
    await server.start()
    try:
        assert socket_path.exists()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_plugin_host_server_stop_removes_server(
    tmp_path: Path,
    registry: PluginRegistry,
    event_bus: EventBus,
) -> None:
    """PluginHostServer.stop() without start() does not raise."""
    socket_path = tmp_path / "run" / "plugin_host.sock"
    server = PluginHostServer(
        socket_path=socket_path,
        registry=registry,
        event_bus=event_bus,
        secrets_resolver=_no_secrets,
    )
    await server.stop()  # must not raise


@pytest.mark.asyncio
async def test_plugin_host_server_removes_stale_socket(
    tmp_path: Path,
    registry: PluginRegistry,
    event_bus: EventBus,
) -> None:
    """PluginHostServer.start() removes a pre-existing stale socket file."""
    socket_path = tmp_path / "run" / "plugin_host.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.write_bytes(b"stale")

    server = PluginHostServer(
        socket_path=socket_path,
        registry=registry,
        event_bus=event_bus,
        secrets_resolver=_no_secrets,
    )
    await server.start()
    try:
        assert socket_path.exists()
    finally:
        await server.stop()
