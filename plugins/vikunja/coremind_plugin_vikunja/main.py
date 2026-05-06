"""Vikunja task manager sensor plugin — counts tasks by project and emits metrics.

Usage: VIKUNJA_URL="http://localhost:3456" VIKUNJA_TOKEN="..." python -m coremind_plugin_vikunja
"""

from __future__ import annotations
import asyncio, os, uuid
from datetime import UTC, datetime
from pathlib import Path

import grpc, grpc.aio, requests, structlog
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from coremind.crypto.signatures import canonical_json, ensure_plugin_keypair, sign
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc

log = structlog.get_logger(__name__)
PLUGIN_ID = "coremind.plugin.vikunja"
PLUGIN_VERSION = "0.1.0"
KEY_STORE_ID = "coremind_plugin_vikunja"
DEFAULT_SOCKET_PATH = Path.home() / ".coremind" / "run" / "plugin_host.sock"
POLL_INTERVAL = int(os.environ.get("VIKUNJA_POLL_SECONDS", "600"))
VIKUNJA_URL = os.environ.get("VIKUNJA_URL", "http://localhost:3456")
VIKUNJA_TOKEN = os.environ.get("VIKUNJA_TOKEN", "")
CONFIDENCE = 0.95


def _get(path):
    try:
        r = requests.get(
            f"{VIKUNJA_URL}/api/v1{path}",
            headers={"Authorization": f"Bearer {VIKUNJA_TOKEN}"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except:
        return []


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
    RECONNECT_DELAY = 10

    log.info("vikunja.starting", plugin_id=PLUGIN_ID)

    # Outer loop: survive daemon restarts and connection loss
    while True:
        try:
            async with grpc.aio.insecure_channel(ch) as channel:
                stub = plugin_pb2_grpc.CoreMindHostStub(channel)
                meta = (("x-plugin-id", PLUGIN_ID),)
                log.info("vikunja.connected", plugin_id=PLUGIN_ID)

                while True:
                    try:
                        # Count tasks by project
                        projects = _get("/projects")
                        total_open = 0
                        total_done = 0
                        total_overdue = 0
                        for proj in projects:
                            pid = proj.get("id")
                            tasks = _get(f"/projects/{pid}/tasks")
                            if not tasks:
                                continue
                            open_count = sum(1 for t in tasks if not t.get("done", False))
                            done_count = sum(1 for t in tasks if t.get("done", False))
                            overdue = sum(
                                1
                                for t in tasks
                                if not t.get("done")
                                and t.get("due_date", "0") < datetime.now(UTC).strftime("%Y-%m-%d")
                            )
                            total_open += open_count
                            total_done += done_count
                            total_overdue += overdue
                            pname = proj.get("title", "?")
                            await stub.EmitEvent(
                                build_signed_event(key, "project", pname, "open_tasks", open_count),
                                metadata=meta,
                            )
                        # Global stats
                        await stub.EmitEvent(
                            build_signed_event(key, "task_manager", "vikunja", "open_tasks", total_open),
                            metadata=meta,
                        )
                        await stub.EmitEvent(
                            build_signed_event(key, "task_manager", "vikunja", "completed_tasks", total_done),
                            metadata=meta,
                        )
                        await stub.EmitEvent(
                            build_signed_event(key, "task_manager", "vikunja", "overdue_tasks", total_overdue),
                            metadata=meta,
                        )
                        log.info("vikunja.cycle_done", open=total_open, done=total_done, overdue=total_overdue)
                    except grpc.RpcError as exc:
                        log.warning("vikunja.rpc_error_reconnecting", error=exc.details(), exc_info=False)
                        break
                    except Exception as e:
                        log.warning("vikunja.error", error=str(e))

                    await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("vikunja.connection_lost_reconnecting")

        await asyncio.sleep(RECONNECT_DELAY)


def main():
    asyncio.run(run())
