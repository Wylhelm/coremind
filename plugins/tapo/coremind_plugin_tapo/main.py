# Tapo Camera Sensor Plugin
# Captures snapshots from Tapo C225 via RTSP every 5 min and emits WorldEvents

import asyncio
import io
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

PLUGIN_ID: str = "coremind.plugin.tapo"
PLUGIN_VERSION: str = "0.1.0"
KEY_STORE_ID: str = "coremind_plugin_tapo"
DEFAULT_SOCKET_PATH: Path = Path.home() / ".coremind" / "run" / "plugin_host.sock"
DEFAULT_CONFIG_PATH: Path = Path(__file__).parent / "config.toml"
POLL_INTERVAL_SECONDS: int = 300

# Tapo defaults
TAPO_IP: str = os.environ.get("TAPO_IP", "10.0.0.131")
TAPO_USERNAME: str = os.environ.get("TAPO_USERNAME", "admin")
TAPO_PASSWORD: str = os.environ.get("TAPO_PASSWORD", "")
RTSP_PORT: int = 554
STREAM_PATH: str = "/stream1"

# Vision models via Ollama Pro (no extra API costs)
OLLAMA_HOST: str = os.environ.get("OLLAMA_API_BASE", "http://10.0.0.175:11434")
VISION_PRIMARY: str = "gemini-3-flash-preview:latest"  # Gemini via Ollama Pro
VISION_FALLBACK: str = "mistral-large-3:675b-cloud"     # Mistral via Ollama Pro
VISION_ENABLED: bool = True

CONFIDENCE: float = 0.85
CONFIDENCE_VISION: float = 0.75


def load_config() -> dict:
    """Load tapo plugin config from TOML if present."""
    if not DEFAULT_CONFIG_PATH.exists():
        return {}
    raw = tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    return raw.get("tapo", {})


def _make_timestamp(dt: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def capture_snapshot(output_path: str, username: str, password: str, ip: str, port: int, stream: str) -> bool:
    """Capture a single frame from the Tapo RTSP stream using ffmpeg."""
    rtsp_url = f"rtsp://{username}:{password}@{ip}:{port}{stream}"
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-vframes", "1",
        "-timeout", "15000000",  # 15s in microseconds
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            log.warning("tapo.capture_failed", stderr=result.stderr[:200])
            return False
        return Path(output_path).exists()
    except subprocess.TimeoutExpired:
        log.warning("tapo.capture_timeout")
        return False
    except Exception as exc:
        log.error("tapo.capture_error", error=str(exc))
        return False


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
        entity=plugin_pb2.EntityRef(type="camera", entity_id="tapo_living_room"),
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


async def analyze_scene(image_path: Path) -> dict[str, str | bool]:
    """Analyze a snapshot via Ollama — Gemini Flash (primary) or Mistral (fallback)."""
    try:
        from PIL import Image
        import ollama, json as _json

        img = Image.open(image_path)
        img.thumbnail((512, 512))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        img_bytes = buf.getvalue()

        host = OLLAMA_HOST.replace("http://", "").replace("https://", "").rstrip("/")
        if ":" in host:
            hp = host.split(":")
            client = ollama.Client(host=f"http://{hp[0]}:{int(hp[1])}")
        else:
            client = ollama.Client(host=f"http://{host}:11434")

        prompt = (
            "Describe this room in JSON: "
            "person_present (boolean), person_name (string — identify who you see. "
            "Guillaume is a man in his late 40s with short brown hair, often in a t-shirt. "
            "Aurélie is a young woman in her 20s with long dark hair. "
            "Julie is a woman in her 40s with brown hair. "
            "Jeff is a man in his 40s, bald or shaved head. "
            "If you cannot identify confidently, use 'unknown'), "
            "activity (string), "
            "pets_visible (boolean), pet_description (string — "
            "3 black cats: Poukie (medium), Timimi (larger, caramel hints), Minuit (small). "
            "Identify cats by size/position if visible). "
            "Example: {\"person_present\": true, \"person_name\": \"Guillaume\", "
            "\"activity\": \"working at desk\", \"pets_visible\": true, "
            "\"pet_description\": \"Timimi on the couch\"}"
        )

        # Try Gemini Flash first (faster, more reliable)
        for model in [VISION_PRIMARY, VISION_FALLBACK]:
            try:
                resp = client.chat(
                    model=model,
                    messages=[{"role": "user", "content": prompt, "images": [img_bytes]}],
                )
                text = resp["message"]["content"]
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    result = _json.loads(text[start:end])
                    log.info("tapo.vision_analysis", model=model, result=result)
                    return result
            except Exception:
                continue

        return {}
    except Exception as exc:
        log.warning("tapo.vision_failed", error=str(exc))
        return {}


async def match_face_with_immich(
    image_path: Path,
    faces_dir: Path = Path.home() / ".coremind" / "faces",
) -> str | None:
    """Match a person in the image against Immich reference faces.

    Uses Mistral vision to compare the snapshot against reference face
    photos extracted from Immich. Called as fallback when text-based
    identification returns 'unknown'.

    Returns the person name or None.
    """
    if not faces_dir.exists():
        return None

    face_files = sorted(faces_dir.glob("*.jpg"))
    if not face_files:
        return None

    try:
        from PIL import Image
        import ollama, json as _json

        host = OLLAMA_HOST.replace("http://", "").replace("https://", "").rstrip("/")
        if ":" in host:
            hp = host.split(":")
            client = ollama.Client(host=f"http://{hp[0]}:{int(hp[1])}")
        else:
            client = ollama.Client(host=f"http://{host}:11434")

        # Load snapshot
        snap = Image.open(image_path)
        snap.thumbnail((512, 512))
        buf = io.BytesIO()
        snap.save(buf, format="JPEG", quality=70)

        # Batch reference faces (max 7 per call = 1 snapshot + 7 refs = 8 total)
        batch_size = 7
        for i in range(0, len(face_files), batch_size):
            batch = face_files[i:i + batch_size]
            ref_imgs = []
            names = []
            for f in batch:
                img = Image.open(f)
                b = io.BytesIO()
                img.save(b, format="JPEG", quality=85)
                ref_imgs.append(b.getvalue())
                names.append(f.stem)

            content = (
                f"Reference faces: {', '.join(names)}. "
                "Who is in the room photo? Answer JSON: {\"person_name\": \"name or unknown\"}"
            )
            messages = [{
                "role": "user",
                "content": content,
                "images": [buf.getvalue()] + ref_imgs,
            }]

            resp = client.chat(model=VISION_MODEL, messages=messages)
            text = resp["message"]["content"]
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                result = _json.loads(text[start:end])
                name = result.get("person_name", "unknown")
                if name != "unknown" and name in names:
                    log.info("tapo.face_matched", name=name, batch=i)
                    return name

        return None
    except Exception as exc:
        log.warning("tapo.face_match_failed", error=str(exc))
        return None


async def run() -> None:
    cfg = load_config()
    username = cfg.get("username", TAPO_USERNAME)
    password = cfg.get("password", TAPO_PASSWORD)
    ip = cfg.get("ip", TAPO_IP)
    port = int(cfg.get("rtsp_port", RTSP_PORT))
    stream = cfg.get("stream_path", STREAM_PATH)
    interval = int(cfg.get("poll_interval_seconds", POLL_INTERVAL_SECONDS))

    if not password:
        raise RuntimeError("TAPO_PASSWORD environment variable is not set")

    private_key = ensure_plugin_keypair(KEY_STORE_ID)
    channel_addr = f"unix://{DEFAULT_SOCKET_PATH}"
    snapshot_dir = Path.home() / ".coremind" / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    RECONNECT_DELAY = 10  # seconds between reconnect attempts

    log.info("tapo.starting", plugin_id=PLUGIN_ID, ip=ip, interval=interval)

    # Outer loop: survive daemon restarts and connection loss
    while True:
        try:
            async with grpc.aio.insecure_channel(channel_addr) as channel:
                stub = plugin_pb2_grpc.CoreMindHostStub(channel)
                metadata = (("x-plugin-id", PLUGIN_ID),)
                log.info("tapo.connected", plugin_id=PLUGIN_ID)

                # Inner loop: normal operation
                while True:
                    try:
                        snap_path = snapshot_dir / f"tapo_{datetime.now(UTC):%Y%m%d_%H%M%S}.jpg"

                        success = capture_snapshot(
                            str(snap_path), username, password, ip, port, stream
                        )

                        # Emit snapshot_taken event
                        size_bytes = snap_path.stat().st_size if snap_path.exists() else 0
                        event = build_signed_event(
                            private_key,
                            attribute="snapshot_taken",
                            value=success,
                        )
                        try:
                            await stub.EmitEvent(event, metadata=metadata)
                        except grpc.RpcError as exc:
                            log.warning("tapo.emit_failed_reconnecting", error=exc.details(), exc_info=False)
                            break  # Reconnect

                        if success:
                            event_size = build_signed_event(
                                private_key,
                                attribute="snapshot_size_bytes",
                                value=float(size_bytes),
                                unit="bytes",
                            )
                            try:
                                await stub.EmitEvent(event_size, metadata=metadata)
                            except grpc.RpcError:
                                break  # Reconnect

                        log.info("tapo.snapshot_captured", success=success, size=size_bytes)

                        # Vision analysis: Gemini Flash (primary) → Mistral (fallback), both via Ollama
                        if success and VISION_ENABLED:
                            vision = await analyze_scene(snap_path)
                            if vision:
                                # If person is present but unidentified, try Immich face matching
                                if vision.get("person_present") and vision.get("person_name") == "unknown":
                                    matched_name = await match_face_with_immich(snap_path)
                                    if matched_name:
                                        vision["person_name"] = matched_name
                                        log.info("tapo.face_identified", name=matched_name)

                                for attr, val in vision.items():
                                    event_v = build_signed_event(
                                        private_key,
                                        attribute=attr,
                                        value=val,
                                        confidence=CONFIDENCE_VISION,
                                    )
                                    try:
                                        await stub.EmitEvent(event_v, metadata=metadata)
                                        log.info("tapo.vision_emitted", attribute=attr, value=val)
                                    except grpc.RpcError:
                                        break  # Reconnect on any emit failure

                    except grpc.RpcError as exc:
                        log.warning("tapo.rpc_error_reconnecting", error=exc.details(), exc_info=False)
                        break  # Inner loop → reconnect

                    await asyncio.sleep(interval)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tapo.connection_lost_reconnecting")

        await asyncio.sleep(RECONNECT_DELAY)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
