# Webcam Sensor Plugin
# Captures frames from USB webcam every 10 min and emits WorldEvents

import asyncio
import os
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

PLUGIN_ID: str = "coremind.plugin.webcam"
PLUGIN_VERSION: str = "0.1.0"
KEY_STORE_ID: str = "coremind_plugin_webcam"
DEFAULT_SOCKET_PATH: Path = Path.home() / ".coremind" / "run" / "plugin_host.sock"
DEFAULT_CONFIG_PATH: Path = Path(__file__).parent / "config.toml"
POLL_INTERVAL_SECONDS: int = 600

CONFIDENCE: float = 0.80


def load_config() -> dict:
    if not DEFAULT_CONFIG_PATH.exists():
        return {}
    raw = tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    return raw.get("webcam", {})


def _make_timestamp(dt: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def capture_frame(output_path: str, device: str = "/dev/video0") -> bool:
    """Capture a single frame from the webcam using ffmpeg."""
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-f", "video4linux2",
        "-s", "640x480",
        "-i", device,
        "-vframes", "1",
        "-timeout", "10",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            log.warning("webcam.capture_failed", stderr=result.stderr[:200])
            return False
        return Path(output_path).exists()
    except subprocess.TimeoutExpired:
        log.warning("webcam.capture_timeout")
        return False
    except Exception as exc:
        log.error("webcam.capture_error", error=str(exc))
        return False


def detect_motion(current_frame: Path, previous_frame: Path | None, threshold: float = 0.05) -> bool:
    """Simple motion detection by comparing file sizes (proxy for frame difference)."""
    if previous_frame is None or not previous_frame.exists():
        return False
    try:
        current_size = current_frame.stat().st_size
        prev_size = previous_frame.stat().st_size
        if prev_size == 0:
            return False
        return abs(current_size - prev_size) / prev_size > threshold
    except Exception:
        return False


def build_signed_event(
    private_key: Ed25519PrivateKey,
    *,
    entity_id: str = "webcam_desk",
    attribute: str,
    value: float | str | bool,
    unit: str | None = None,
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
        entity=plugin_pb2.EntityRef(type="camera", entity_id=entity_id),
        attribute=attribute,
        value=pb_value,
        confidence=CONFIDENCE,
    )
    if unit:
        unsigned.unit = unit

    unsigned_dict = MessageToDict(unsigned, preserving_proto_field_name=True)
    unsigned_dict.pop("signature", None)
    payload = canonical_json(unsigned_dict)
    unsigned.signature = sign(payload, private_key)
    return unsigned


async def run() -> None:
    cfg = load_config()
    device = cfg.get("device", "/dev/video0")
    interval = int(cfg.get("poll_interval_seconds", POLL_INTERVAL_SECONDS))

    private_key = ensure_plugin_keypair(KEY_STORE_ID)
    channel_addr = f"unix://{DEFAULT_SOCKET_PATH}"
    frame_dir = Path.home() / ".coremind" / "webcam_frames"
    frame_dir.mkdir(parents=True, exist_ok=True)

    log.info("webcam.starting", plugin_id=PLUGIN_ID, device=device, interval=interval)

    async with grpc.aio.insecure_channel(channel_addr) as channel:
        stub = plugin_pb2_grpc.CoreMindHostStub(channel)
        metadata = (("x-plugin-id", PLUGIN_ID),)
        previous_frame: Path | None = None

        while True:
            frame_path = frame_dir / f"webcam_{datetime.now(UTC):%Y%m%d_%H%M%S}.jpg"

            success = capture_frame(str(frame_path), device)

            event = build_signed_event(
                private_key,
                attribute="frame_captured",
                value=success,
            )
            try:
                await stub.EmitEvent(event, metadata=metadata)
            except grpc.RpcError as exc:
                log.error("webcam.emit_failed", error=exc.details(), exc_info=False)
                await asyncio.sleep(interval)
                continue

            if success:
                size = frame_path.stat().st_size if frame_path.exists() else 0
                size_event = build_signed_event(
                    private_key,
                    attribute="frame_size_bytes",
                    value=float(size),
                    unit="bytes",
                )
                try:
                    await stub.EmitEvent(size_event, metadata=metadata)
                except grpc.RpcError:
                    pass

                motion = detect_motion(frame_path, previous_frame)
                motion_event = build_signed_event(
                    private_key,
                    attribute="motion_detected",
                    value=motion,
                )
                try:
                    await stub.EmitEvent(motion_event, metadata=metadata)
                except grpc.RpcError:
                    pass

                previous_frame = frame_path

                log.info(
                    "webcam.frame_captured",
                    size=size,
                    motion=motion,
                )

            await asyncio.sleep(interval)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
