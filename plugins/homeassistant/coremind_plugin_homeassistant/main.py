"""Home Assistant integration plugin — emits state-change events to CoreMind.

Connects to a Home Assistant instance over the WebSocket API, authenticates,
subscribes to ``state_changed`` events, and filters them by configured entity
prefixes before signing and forwarding each observation to the CoreMind
daemon via gRPC.

Usage::

    python -m coremind_plugin_homeassistant
"""

from __future__ import annotations

import asyncio
import json
import os
import tomllib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import grpc
import grpc.aio
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import BaseModel, Field

from coremind.crypto.signatures import canonical_json, ensure_plugin_keypair, sign
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc

log = structlog.get_logger(__name__)

PLUGIN_ID: str = "coremind.plugin.homeassistant"
PLUGIN_VERSION: str = "0.1.0"
KEY_STORE_ID: str = "coremind_plugin_homeassistant"

DEFAULT_SOCKET_PATH: Path = Path.home() / ".coremind" / "run" / "plugin_host.sock"
DEFAULT_CONFIG_PATH: Path = Path(__file__).parent / "config.toml"

CONFIDENCE: float = 0.9


class HAConfig(BaseModel):
    """Plugin configuration."""

    base_url: str = "http://localhost:8123"
    access_token_env: str = "HA_TOKEN"  # noqa: S105 — env var name, not a secret
    entity_prefixes: list[str] = Field(
        default_factory=lambda: ["sensor.", "light.", "binary_sensor."]
    )


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> HAConfig:
    """Load plugin config from a TOML file.

    Args:
        path: Path to a TOML file with a ``[homeassistant]`` table.  If the
            file is absent, defaults are used.

    Returns:
        A validated :class:`HAConfig`.
    """
    if not path.exists():
        return HAConfig()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    section = raw.get("homeassistant", {})
    return HAConfig.model_validate(section)


def _ws_url(base_url: str) -> str:
    """Return the Home Assistant WebSocket URL for *base_url*."""
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :].rstrip("/") + "/api/websocket"
    return "ws://" + base_url.removeprefix("http://").rstrip("/") + "/api/websocket"


def _entity_type(ha_entity_id: str) -> str:
    """Map a Home Assistant entity_id to a CoreMind entity type.

    Examples::

        "sensor.living_room_temp"  → "ha_sensor"
        "light.kitchen"            → "ha_light"
        "binary_sensor.front_door" → "ha_binary_sensor"
    """
    domain, _, _ = ha_entity_id.partition(".")
    return f"ha_{domain or 'unknown'}"


def _make_timestamp(dt: datetime) -> Timestamp:
    """Convert a timezone-aware datetime to a protobuf Timestamp."""
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def build_signed_event(
    private_key: Ed25519PrivateKey,
    *,
    entity_id: str,
    attribute: str,
    value: float | int | str | bool | None,
    unit: str | None = None,
    confidence: float = CONFIDENCE,
) -> plugin_pb2.WorldEvent:
    """Build a signed WorldEvent proto for a single HA observation.

    Args:
        private_key: Plugin's ed25519 private key.
        entity_id: Home Assistant ``entity_id`` string.
        attribute: Attribute name (``state``, ``temperature``, ...).
        value: Observed value.
        unit: Optional unit of measurement.
        confidence: Confidence score, defaulting to :data:`CONFIDENCE`.

    Returns:
        A signed :class:`plugin_pb2.WorldEvent`.
    """
    event_id = uuid.uuid4().hex
    ts = _make_timestamp(datetime.now(UTC))

    pb_value = Value()
    if isinstance(value, bool):
        pb_value.bool_value = value
    elif isinstance(value, (int, float)):
        pb_value.number_value = float(value)
    elif value is None:
        pb_value.null_value = Value().null_value  # NullValue.NULL_VALUE
    else:
        pb_value.string_value = str(value)

    unsigned = plugin_pb2.WorldEvent(
        id=event_id,
        timestamp=ts,
        source=PLUGIN_ID,
        source_version=PLUGIN_VERSION,
        signature=b"",
        entity=plugin_pb2.EntityRef(type=_entity_type(entity_id), entity_id=entity_id),
        attribute=attribute,
        value=pb_value,
        confidence=confidence,
        unit=unit or "",
    )
    unsigned_dict = MessageToDict(unsigned, preserving_proto_field_name=True)
    unsigned_dict.pop("signature", None)
    payload = canonical_json(unsigned_dict)
    unsigned.signature = sign(payload, private_key)
    return unsigned


async def ha_state_changes(
    session: aiohttp.ClientSession,
    ws_url: str,
    token: str,
    entity_prefixes: list[str],
) -> AsyncIterator[dict[str, Any]]:
    """Yield filtered ``state_changed`` events from the HA WebSocket API.

    Handles authentication, subscription, and prefix filtering.  Each
    yielded dict is the ``event.data`` payload from HA (``entity_id``,
    ``new_state``, ``old_state``).

    Args:
        session: An active aiohttp session.
        ws_url: ``ws://…/api/websocket`` URL.
        token: Long-lived access token.
        entity_prefixes: Only entities whose ``entity_id`` starts with one of
            these prefixes are yielded.  Empty list = all entities.

    Raises:
        aiohttp.ClientError: On transport failures.
        RuntimeError: If HA auth is rejected.
    """
    async with session.ws_connect(ws_url, heartbeat=30) as ws:
        # 1) HA sends auth_required; reply with auth.
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": token})
        auth_result = await ws.receive_json()
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"HA auth failed: {auth_result}")

        # 2) Subscribe to state_changed events.
        await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
        sub_ack = await ws.receive_json()
        if not sub_ack.get("success", False):
            raise RuntimeError(f"HA subscription failed: {sub_ack}")

        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("type") != "event":
                continue
            event = data.get("event", {})
            payload = event.get("data", {})
            entity_id = payload.get("entity_id", "")
            if entity_prefixes and not any(entity_id.startswith(p) for p in entity_prefixes):
                continue
            yield payload


async def _emit_state_change(
    stub: plugin_pb2_grpc.CoreMindHostStub,
    private_key: Ed25519PrivateKey,
    payload: dict[str, Any],
) -> None:
    """Emit a WorldEvent for one HA ``state_changed`` payload.

    Currently emits one event for ``state`` and, when present in attributes,
    one each for ``temperature``, ``humidity``, and ``brightness``.
    """
    metadata = (("x-plugin-id", PLUGIN_ID),)
    entity_id = str(payload.get("entity_id", ""))
    new_state = payload.get("new_state") or {}
    state_value = new_state.get("state")
    attrs = new_state.get("attributes") or {}

    observations: list[tuple[str, float | int | str | bool | None, str | None]] = [
        ("state", state_value, None),
    ]
    for extra_attr in ("temperature", "humidity", "brightness"):
        if extra_attr in attrs:
            unit = attrs.get("unit_of_measurement")
            observations.append((extra_attr, attrs[extra_attr], unit))

    for attribute, value, unit in observations:
        event = build_signed_event(
            private_key,
            entity_id=entity_id,
            attribute=attribute,
            value=value,
            unit=unit,
        )
        try:
            await stub.EmitEvent(event, metadata=metadata)
        except grpc.RpcError as exc:
            log.error("homeassistant.emit_failed", entity=entity_id, error=exc.details())
            return
        log.info(
            "homeassistant.emitted",
            entity=entity_id,
            attribute=attribute,
            value=value,
        )


async def run(
    socket_path: Path = DEFAULT_SOCKET_PATH,
    config: HAConfig | None = None,
) -> None:
    """Main event loop: connect to HA, forward state changes to CoreMind.

    Args:
        socket_path: Path to the CoreMind plugin-host Unix socket.
        config: Plugin config.  Loaded from disk if omitted.
    """
    cfg = config or load_config()
    token = os.environ.get(cfg.access_token_env, "")
    if not token:
        raise RuntimeError(
            f"{cfg.access_token_env} is not set; cannot authenticate with Home Assistant"
        )

    private_key = ensure_plugin_keypair(KEY_STORE_ID)
    channel_addr = f"unix://{socket_path}"

    log.info(
        "homeassistant.starting",
        plugin_id=PLUGIN_ID,
        ha_url=cfg.base_url,
        prefixes=cfg.entity_prefixes,
    )

    async with (
        aiohttp.ClientSession() as session,
        grpc.aio.insecure_channel(channel_addr) as channel,
    ):
        stub = plugin_pb2_grpc.CoreMindHostStub(channel)  # type: ignore[no-untyped-call]
        ws_url = _ws_url(cfg.base_url)

        async for payload in ha_state_changes(session, ws_url, token, cfg.entity_prefixes):
            await _emit_state_change(stub, private_key, payload)


def main() -> None:
    """Synchronous entry point for the plugin process."""
    asyncio.run(run())
