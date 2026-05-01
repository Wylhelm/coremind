"""Firefly III finance sensor plugin — queries account balances, budgets, and
recent transactions, emitting each as a signed WorldEvent to CoreMind.

Polls every 30 minutes (configurable). Requires FIREFLY_URL and
FIREFLY_TOKEN environment variables.

Usage::

    FIREFLY_URL="http://localhost:8080" FIREFLY_TOKEN="..." python -m coremind_plugin_firefly
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
import requests
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp

from coremind.crypto.signatures import canonical_json, ensure_plugin_keypair, sign
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc

log = structlog.get_logger(__name__)

PLUGIN_ID = "coremind.plugin.firefly"
PLUGIN_VERSION = "0.1.0"
KEY_STORE_ID = "coremind_plugin_firefly"
DEFAULT_SOCKET_PATH = Path.home() / ".coremind" / "run" / "plugin_host.sock"
POLL_INTERVAL = int(os.environ.get("FIREFLY_POLL_SECONDS", "1800"))

FIREFLY_URL = os.environ.get("FIREFLY_URL", "http://localhost:8080")
FIREFLY_TOKEN = os.environ.get("FIREFLY_TOKEN", "")

CONFIDENCE = 0.95


def _ff_get(path: str) -> dict[str, Any]:
    """GET *path* from the Firefly III API, returning the JSON body."""
    try:
        r = requests.get(
            f"{FIREFLY_URL}{path}",
            headers={
                "Authorization": f"Bearer {FIREFLY_TOKEN}",
                "Accept": "application/vnd.api+json",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("firefly.api_error", path=path, error=str(exc))
        return {}


def _make_timestamp(dt: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def build_signed_event(
    private_key: Ed25519PrivateKey,
    entity_type: str,
    entity_id: str,
    attribute: str,
    value: float,
    unit: str | None = None,
) -> plugin_pb2.WorldEvent:
    event_id = uuid.uuid4().hex
    ts = _make_timestamp(datetime.now(UTC))
    unsigned = plugin_pb2.WorldEvent(
        id=event_id,
        timestamp=ts,
        source=PLUGIN_ID,
        source_version=PLUGIN_VERSION,
        signature=b"",
        entity=plugin_pb2.EntityRef(type=entity_type, entity_id=entity_id),
        attribute=attribute,
        value=Value(number_value=value),
        confidence=CONFIDENCE,
    )
    if unit:
        unsigned.unit = unit
    d = MessageToDict(unsigned, preserving_proto_field_name=True)
    d.pop("signature", None)
    unsigned.signature = sign(canonical_json(d), private_key)
    return unsigned


async def _emit_all(
    stub: plugin_pb2_grpc.CoreMindHostStub,
    private_key: Ed25519PrivateKey,
) -> int:
    """Query Firefly III and emit one event per account balance. Returns count emitted."""
    metadata = (("x-plugin-id", PLUGIN_ID),)
    emitted = 0

    # Account balances (asset accounts only)
    data = _ff_get("/api/v1/accounts?type=asset&limit=30")
    for acc in data.get("data", []):
        attrs = acc.get("attributes", {})
        name = attrs.get("name", "?").lstrip("/")
        balance = float(attrs.get("current_balance", 0) or 0)
        currency = attrs.get("currency_code", "CAD")
        event = build_signed_event(private_key, "account", name, "balance", balance, currency)
        try:
            await stub.EmitEvent(event, metadata=metadata)
            emitted += 1
            log.info("firefly.emitted", entity=name, attribute="balance", value=balance)
        except grpc.RpcError as exc:
            log.error("firefly.emit_failed", entity=name, error=exc.details())
            return emitted

    # Budget status + alerts
    data = _ff_get("/api/v1/budgets?limit=10")
    for bud in data.get("data", []):
        attrs = bud.get("attributes", {})
        name = attrs.get("name", "?")
        spent_list = attrs.get("spent", [])
        if spent_list and isinstance(spent_list, list):
            spent_amt = float(spent_list[0].get("sum", 0) or 0)
            bud_amt = float(spent_list[0].get("budget_limit", 0) or 0)
            # Emit spent amount
            event = build_signed_event(private_key, "budget", name, "spent", spent_amt, "CAD")
            try:
                await stub.EmitEvent(event, metadata=metadata)
                emitted += 1
                log.info("firefly.emitted", entity=name, attribute="spent", value=spent_amt)
            except grpc.RpcError as exc:
                log.error("firefly.emit_failed", entity=name, error=exc.details())
            # Alert if budget > 80% spent
            if bud_amt > 0 and spent_amt / bud_amt >= 0.8:
                pct = (spent_amt / bud_amt) * 100
                alert = build_signed_event(private_key, "budget", name, "budget_alert", pct, "%")
                try:
                    await stub.EmitEvent(alert, metadata=metadata)
                    emitted += 1
                    log.warning("firefly.budget_alert", budget=name, pct=f"{pct:.0f}%")
                except grpc.RpcError:
                    pass
        break  # One budget only for now

    return emitted


async def run() -> None:
    private_key = ensure_plugin_keypair(KEY_STORE_ID)
    channel_addr = f"unix://{DEFAULT_SOCKET_PATH}"
    log.info("firefly.starting", plugin_id=PLUGIN_ID, interval=POLL_INTERVAL)

    async with grpc.aio.insecure_channel(channel_addr) as channel:
        stub = plugin_pb2_grpc.CoreMindHostStub(channel)
        while True:
            n = await _emit_all(stub, private_key)
            log.info("firefly.cycle_done", emitted=n)
            await asyncio.sleep(POLL_INTERVAL)


def main() -> None:
    asyncio.run(run())
