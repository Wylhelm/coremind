# Google Workspace (gog) Sensor Plugin
# Scans Gmail unread emails and Google Calendar events via the gog CLI.
# Emits WorldEvents so the LLM can reason about email and calendar context.

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import tomllib
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

log = structlog.get_logger(__name__)

PLUGIN_ID: str = "coremind.plugin.gog"
PLUGIN_VERSION: str = "0.1.0"
KEY_STORE_ID: str = "coremind_plugin_gog"
DEFAULT_SOCKET_PATH: Path = Path.home() / ".coremind" / "run" / "plugin_host.sock"
DEFAULT_CONFIG_PATH: Path = Path(__file__).parent / "config.toml"

GMAIL_POLL_SECONDS: int = 300
CALENDAR_POLL_SECONDS: int = 900
MAX_UNREAD: int = 10
MAX_CAL_EVENTS: int = 10
CONFIDENCE: float = 0.95


def load_config() -> dict:
    """Load gog plugin config from TOML if present."""
    if not DEFAULT_CONFIG_PATH.exists():
        return {}
    raw = tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    return raw.get("gog", {})


def _make_timestamp(dt: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def build_signed_event(
    private_key: Ed25519PrivateKey,
    *,
    attribute: str,
    value: float | str | bool,
    unit: str | None = None,
    confidence: float = CONFIDENCE,
) -> plugin_pb2.WorldEvent:
    event_id = uuid.uuid4().hex
    ts = _make_timestamp(datetime.now(UTC))

    pb_value = Value()
    if isinstance(value, bool):
        pb_value.bool_value = value
    elif isinstance(value, (int, float)):
        pb_value.number_value = float(value)
    else:
        pb_value.string_value = str(value)

    unsigned = plugin_pb2.WorldEvent(
        id=event_id,
        timestamp=ts,
        source=PLUGIN_ID,
        source_version=PLUGIN_VERSION,
        signature=b"",
        entity=plugin_pb2.EntityRef(type="entity", entity_id="gog:workspace"),
        attribute=attribute,
        value=pb_value,
        confidence=confidence,
    )
    if unit:
        unsigned.unit = unit

    unsigned_dict = MessageToDict(unsigned, preserving_proto_field_name=True)
    unsigned_dict.pop("signature", None)
    payload = canonical_json(unsigned_dict)
    unsigned.signature = sign(payload, private_key)
    return unsigned


async def poll_gmail(
    stub: plugin_pb2_grpc.CoreMindHostStub,
    private_key: Ed25519PrivateKey,
    metadata: tuple,
    max_results: int,
) -> None:
    """Poll gog for unread emails and emit events."""
    cmd = ["gog", "gmail", "search", "--json", f"--max={max_results}", "is:unread"]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if cp.returncode != 0:
            await _emit(stub, private_key, metadata, "gmail_error", cp.stderr[:200])
            return
        data = json.loads(cp.stdout)
        threads = data.get("threads", [])
        unread_count = len(threads)
        await _emit(stub, private_key, metadata, "gmail_unread_count", float(unread_count))

        for t in threads[:max_results]:
            subject = t.get("subject", "(sans objet)")
            sender = t.get("from", "inconnu")
            date = t.get("date", "")
            thread_id = t.get("id", "")[:20]
            summary = f"{sender}: {subject} ({date})"
            await _emit(
                stub,
                private_key,
                metadata,
                f"gmail_thread_{thread_id}",
                summary,
            )
        log.info("gog.gmail_polled", unread=unread_count)
    except subprocess.TimeoutExpired:
        await _emit(stub, private_key, metadata, "gmail_error", "timeout")
    except json.JSONDecodeError:
        pass  # No output from gog (no unread emails or auth issue)
    except Exception as exc:
        log.warning("gog.gmail_failed", error=str(exc))


async def poll_calendar(
    stub: plugin_pb2_grpc.CoreMindHostStub,
    private_key: Ed25519PrivateKey,
    metadata: tuple,
    max_results: int,
) -> None:
    """Poll gog for upcoming calendar events and emit events."""
    cmd = ["gog", "calendar", "events", "--json", f"--max={max_results}"]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if cp.returncode != 0:
            await _emit(stub, private_key, metadata, "calendar_error", cp.stderr[:200])
            return
        data = json.loads(cp.stdout)
        events = data.get("events", [])
        event_count = len(events)
        await _emit(stub, private_key, metadata, "calendar_upcoming_count", float(event_count))

        for ev in events[:max_results]:
            summary = ev.get("summary", "Sans titre")
            start = ev.get("start", "")
            end = ev.get("end", "")
            description = ev.get("description", "")[:200]
            location = ev.get("location", "")
            event_str = f"{summary} | {start} → {end}"
            if location:
                event_str += f" @ {location}"
            if description:
                event_str += f" — {description[:100]}"
            event_id = ev.get("id", uuid.uuid4().hex)[:20]
            await _emit(
                stub,
                private_key,
                metadata,
                f"calendar_event_{event_id}",
                event_str,
            )
        log.info("gog.calendar_polled", events=event_count)
    except subprocess.TimeoutExpired:
        await _emit(stub, private_key, metadata, "calendar_error", "timeout")
    except json.JSONDecodeError:
        pass
    except Exception as exc:
        log.warning("gog.calendar_failed", error=str(exc))


async def _emit(
    stub: plugin_pb2_grpc.CoreMindHostStub,
    private_key: Ed25519PrivateKey,
    metadata: tuple,
    attribute: str,
    value: str | float | bool,
) -> None:
    event = build_signed_event(private_key, attribute=attribute, value=value)
    with contextlib.suppress(grpc.RpcError):
        await stub.EmitEvent(event, metadata=metadata)


async def run() -> None:
    cfg = load_config()
    gmail_interval = int(cfg.get("gmail_poll_interval_seconds", GMAIL_POLL_SECONDS))
    calendar_interval = int(cfg.get("calendar_poll_interval_seconds", CALENDAR_POLL_SECONDS))
    max_unread = int(cfg.get("max_unread_emails", MAX_UNREAD))
    max_cal = int(cfg.get("max_calendar_events", MAX_CAL_EVENTS))

    private_key = ensure_plugin_keypair(KEY_STORE_ID)
    channel_addr = f"unix://{DEFAULT_SOCKET_PATH}"
    metadata = (("x-plugin-id", PLUGIN_ID),)

    last_gmail = 0.0
    last_cal = 0.0
    reconnect_delay = 10

    log.info(
        "gog.starting",
        plugin_id=PLUGIN_ID,
        gmail_interval=gmail_interval,
        calendar_interval=calendar_interval,
    )

    while True:
        try:
            async with grpc.aio.insecure_channel(channel_addr) as channel:
                stub = plugin_pb2_grpc.CoreMindHostStub(channel)
                log.info("gog.connected", plugin_id=PLUGIN_ID)

                while True:
                    try:
                        now = asyncio.get_event_loop().time()

                        if now - last_gmail >= gmail_interval:
                            await poll_gmail(stub, private_key, metadata, max_unread)
                            last_gmail = now

                        if now - last_cal >= calendar_interval:
                            await poll_calendar(stub, private_key, metadata, max_cal)
                            last_cal = now

                    except grpc.RpcError as exc:
                        log.warning("gog.rpc_error_reconnecting", error=exc.details())
                        break

                    await asyncio.sleep(30)  # Check intervals every 30s

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("gog.connection_lost_reconnecting")

        await asyncio.sleep(reconnect_delay)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
