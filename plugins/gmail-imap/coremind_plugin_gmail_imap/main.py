"""Gmail (IMAP IDLE) integration plugin — emits new-message events to CoreMind.

Connects to an IMAP server, logs in, selects a folder, and uses IMAP IDLE
to stream new-message notifications.  For each new message the plugin
fetches the envelope (From, Subject, has-attachment flag) and emits one
signed ``WorldEvent`` per message with entity type ``email``.

Bodies are **not** stored in L2 — they belong in L3 semantic memory.  A
future ingest step may consume the emitted ``email_id`` attribute to fetch
and index the body separately.

Usage::

    python -m coremind_plugin_gmail_imap
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import os
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aioimaplib
import grpc
import grpc.aio
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import BaseModel

from coremind.crypto.signatures import canonical_json, ensure_plugin_keypair, sign
from coremind.plugin_api._generated import plugin_pb2, plugin_pb2_grpc

log = structlog.get_logger(__name__)

PLUGIN_ID: str = "coremind.plugin.gmail-imap"
PLUGIN_VERSION: str = "0.1.0"
KEY_STORE_ID: str = "coremind_plugin_gmail_imap"

DEFAULT_SOCKET_PATH: Path = Path.home() / ".coremind" / "run" / "plugin_host.sock"
DEFAULT_CONFIG_PATH: Path = Path(__file__).parent / "config.toml"

CONFIDENCE: float = 0.99  # IMAP-reported envelopes are exact
IDLE_TIMEOUT_SECONDS: int = 29 * 60  # refresh IDLE before servers time out at 30m


class ImapConfig(BaseModel):
    """Plugin configuration."""

    host: str = "imap.gmail.com"
    port: int = 993
    username: str = ""
    password_env: str = "GMAIL_IMAP_PASSWORD"  # noqa: S105 — env var name, not a password
    folder: str = "INBOX"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ImapConfig:
    """Load plugin config from a TOML file.

    Args:
        path: Path to a TOML file with an ``[imap]`` table.

    Returns:
        A validated :class:`ImapConfig`.
    """
    if not path.exists():
        return ImapConfig()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return ImapConfig.model_validate(raw.get("imap", {}))


def _make_timestamp(dt: datetime) -> Timestamp:
    """Convert a timezone-aware datetime to a protobuf Timestamp."""
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def _str_value(s: str) -> Value:
    """Wrap a string into a protobuf ``Value``."""
    v = Value()
    v.string_value = s
    return v


def _bool_value(b: bool) -> Value:
    """Wrap a bool into a protobuf ``Value``."""
    v = Value()
    v.bool_value = b
    return v


def build_signed_event(
    private_key: Ed25519PrivateKey,
    *,
    email_id: str,
    attribute: str,
    value: Value,
) -> plugin_pb2.WorldEvent:
    """Build a signed WorldEvent proto for a single email observation.

    Args:
        private_key: Plugin's ed25519 private key.
        email_id: Stable per-message identifier (Message-Id header or UID).
        attribute: Attribute name (``subject``, ``sender``, ``has_attachment``).
        value: Observation value as a protobuf ``Value``.

    Returns:
        A signed :class:`plugin_pb2.WorldEvent`.
    """
    event_id = uuid.uuid4().hex
    ts = _make_timestamp(datetime.now(UTC))

    unsigned = plugin_pb2.WorldEvent(
        id=event_id,
        timestamp=ts,
        source=PLUGIN_ID,
        source_version=PLUGIN_VERSION,
        signature=b"",
        entity=plugin_pb2.EntityRef(type="email", entity_id=email_id),
        attribute=attribute,
        value=value,
        confidence=CONFIDENCE,
    )
    unsigned_dict = MessageToDict(unsigned, preserving_proto_field_name=True)
    unsigned_dict.pop("signature", None)
    payload = canonical_json(unsigned_dict)
    unsigned.signature = sign(payload, private_key)
    return unsigned


def parse_headers(raw_headers: bytes) -> tuple[str, str, str, bool]:
    """Parse an RFC 822 header block into the fields we emit.

    Args:
        raw_headers: The raw bytes from an IMAP ``FETCH (BODY.PEEK[HEADER])``.

    Returns:
        Tuple ``(message_id, subject, sender, has_attachment_hint)``.
    """
    msg = email.message_from_bytes(raw_headers, policy=email.policy.default)
    message_id = str(msg.get("Message-ID") or "").strip("<>")
    subject = str(msg.get("Subject") or "")
    sender = str(msg.get("From") or "")
    content_type = str(msg.get("Content-Type") or "")
    has_attachment = "multipart/mixed" in content_type.lower()
    return message_id, subject, sender, has_attachment


async def _fetch_and_emit(
    imap: aioimaplib.IMAP4_SSL,
    stub: plugin_pb2_grpc.CoreMindHostStub,
    private_key: Ed25519PrivateKey,
    uid: str,
) -> None:
    """Fetch headers for *uid* and emit WorldEvents for the message.

    Args:
        imap: An authenticated, folder-selected IMAP client.
        stub: gRPC stub connected to the daemon.
        private_key: Plugin's ed25519 private key.
        uid: Server-assigned message UID.
    """
    status, data = await imap.uid("fetch", uid, "(BODY.PEEK[HEADER])")
    if status != "OK" or not data:
        log.warning("gmail_imap.fetch_failed", uid=uid, status=status)
        return

    raw_headers = b""
    for item in data:
        if isinstance(item, (bytes, bytearray)) and b"\r\n" in item:
            raw_headers = bytes(item)
            break
    if not raw_headers:
        log.warning("gmail_imap.empty_headers", uid=uid)
        return

    message_id, subject, sender, has_attachment = parse_headers(raw_headers)
    email_id = message_id or f"uid:{uid}"
    metadata = (("x-plugin-id", PLUGIN_ID),)

    observations: list[tuple[str, Value]] = [
        ("subject", _str_value(subject)),
        ("sender", _str_value(sender)),
        ("has_attachment", _bool_value(has_attachment)),
    ]
    for attribute, value in observations:
        event = build_signed_event(
            private_key,
            email_id=email_id,
            attribute=attribute,
            value=value,
        )
        try:
            await stub.EmitEvent(event, metadata=metadata)
        except grpc.RpcError as exc:
            log.error(
                "gmail_imap.emit_failed",
                uid=uid,
                attribute=attribute,
                error=exc.details(),
            )
            return
    log.info("gmail_imap.message_emitted", uid=uid, email_id=email_id, sender=sender)


async def run(
    socket_path: Path = DEFAULT_SOCKET_PATH,
    config: ImapConfig | None = None,
) -> None:
    """Main loop: log in, select folder, IDLE for new messages, emit events.

    Args:
        socket_path: Path to the CoreMind plugin-host Unix socket.
        config: Plugin config.  Loaded from disk when omitted.

    Raises:
        RuntimeError: On IMAP auth failure or missing password env var.
    """
    cfg = config or load_config()
    password = os.environ.get(cfg.password_env, "")
    if not password or not cfg.username:
        raise RuntimeError(f"IMAP credentials missing (username or {cfg.password_env})")

    private_key = ensure_plugin_keypair(KEY_STORE_ID)
    channel_addr = f"unix://{socket_path}"

    log.info(
        "gmail_imap.starting",
        plugin_id=PLUGIN_ID,
        host=cfg.host,
        folder=cfg.folder,
    )

    imap = aioimaplib.IMAP4_SSL(host=cfg.host, port=cfg.port)
    await imap.wait_hello_from_server()
    login_status, _ = await imap.login(cfg.username, password)
    if login_status != "OK":
        raise RuntimeError(f"IMAP login failed: {login_status}")
    await imap.select(cfg.folder)

    async with grpc.aio.insecure_channel(channel_addr) as channel:
        stub = plugin_pb2_grpc.CoreMindHostStub(channel)  # type: ignore[no-untyped-call]
        last_uid = await _current_highest_uid(imap)

        while True:
            idle_task = await imap.idle_start(timeout=IDLE_TIMEOUT_SECONDS)
            # Block until server pushes an EXISTS line or IDLE times out.
            await imap.wait_server_push()
            imap.idle_done()
            try:
                await asyncio.wait_for(idle_task, timeout=5)
            except TimeoutError:
                log.warning("gmail_imap.idle_task_timeout")

            # Fetch all UIDs greater than last_uid.
            status, data = await imap.uid("search", f"UID {last_uid + 1}:*")
            if status != "OK" or not data:
                continue
            new_uids = [b for b in data[0].split() if b]
            for uid_bytes in new_uids:
                uid = uid_bytes.decode()
                try:
                    uid_int = int(uid)
                except ValueError:
                    continue
                if uid_int <= last_uid:
                    continue
                await _fetch_and_emit(imap, stub, private_key, uid)
                last_uid = uid_int


async def _current_highest_uid(imap: aioimaplib.IMAP4_SSL) -> int:
    """Return the highest message UID currently in the selected folder.

    New-message detection starts from this baseline so we do not re-emit
    existing messages at startup.

    Args:
        imap: An authenticated, folder-selected IMAP client.

    Returns:
        The highest UID, or 0 if the folder is empty.
    """
    status, data = await imap.uid("search", "ALL")
    if status != "OK" or not data or not data[0]:
        return 0
    uids = [int(u) for u in data[0].split() if u.isdigit()]
    return max(uids) if uids else 0


def main() -> None:
    """Synchronous entry point for the plugin process."""
    asyncio.run(run())
