"""OpenClaw event → CoreMind WorldEvent translation.

The OpenClaw extension emits adapter-level events as JSON-serialisable dicts.
This module converts those dicts into signed :class:`plugin_pb2.WorldEvent`
protos that can be forwarded to the CoreMind daemon via ``CoreMindHost.EmitEvent``.

All translators are pure functions: they do not sign events. Signing is the
responsibility of the caller (see :func:`sign_event`).

Message bodies longer than :data:`MESSAGE_EXCERPT_MAX_CHARS` are truncated —
full bodies remain in OpenClaw's mem0 store and are only pulled into CoreMind
via an explicit Mem0Query.

Canonical signature encoding — contract shared with the OpenClaw side
--------------------------------------------------------------------

The signature covers the **JCS (RFC 8785) canonical JSON** form of
``MessageToDict(event, preserving_proto_field_name=True)`` with the
``signature`` field removed. Both halves MUST produce the same bytes, or
every ingested event will fail verification. Rules:

* Field names use the **proto snake_case** form (``preserve_proto_field_name``),
  e.g. ``source_version``, ``entity_id``, ``text_excerpt``.
* ``google.protobuf.Timestamp`` is emitted as its canonical RFC 3339 string
  with nanosecond precision, e.g. ``"2026-04-19T20:14:02Z"`` /
  ``"2026-04-19T20:14:02.123456789Z"``. (``MessageToDict`` default.)
* ``bytes`` fields are emitted as unpadded base64 strings. (``MessageToDict``
  default; the only ``bytes`` field in WorldEvent is ``signature``, and
  it is stripped before canonicalisation.)
* ``int64`` / ``uint64`` / ``fixed64`` fields are emitted as **JSON strings**
  (``MessageToDict`` default).
* Floats are emitted with full precision (JCS specifies ECMAScript number
  encoding).
* JCS sorts object keys lexicographically and strips insignificant whitespace.

The reference TypeScript implementation of :func:`sign_event` lives at
``openclaw_side/src/signer.ts``. The file
``tests/integrations/openclaw_adapter/golden_signature.json`` holds a frozen
vector that both halves must reproduce byte-for-byte.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp

from coremind.crypto.signatures import canonical_json, sign
from coremind.plugin_api._generated import plugin_pb2

MESSAGE_EXCERPT_MAX_CHARS: int = 200
"""Maximum body length carried in message_received events.

Longer bodies are truncated; the full body stays in OpenClaw mem0.
"""

DEFAULT_CONFIDENCE: float = 1.0
"""Events coming from OpenClaw are direct observations — confidence = 1.0."""


class TranslationError(ValueError):
    """Raised when an OpenClaw event cannot be translated to a WorldEvent."""


def _parse_iso8601(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, returning a timezone-aware datetime."""
    # ``datetime.fromisoformat`` accepts the "+00:00" and "Z" forms on 3.11+.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise TranslationError(f"timestamp {value!r} is not timezone-aware")
    return parsed.astimezone(UTC)


def _make_timestamp(dt: datetime) -> Timestamp:
    """Build a protobuf Timestamp from a timezone-aware datetime."""
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def _event_id() -> str:
    """Return a new opaque hex event identifier."""
    return uuid.uuid4().hex


def _dict_value(raw: dict[str, Any]) -> Value:
    """Build a :class:`Value` whose oneof is ``struct_value`` from a dict."""
    v = Value()
    ParseDict(raw, v.struct_value)
    return v


def _truncate(text: str, limit: int = MESSAGE_EXCERPT_MAX_CHARS) -> tuple[str, bool]:
    """Return ``(maybe_truncated, was_truncated)``."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


# ---------------------------------------------------------------------------
# Individual translators
# ---------------------------------------------------------------------------


def translate_message_received(
    event: dict[str, Any],
    *,
    plugin_id: str,
    plugin_version: str,
) -> plugin_pb2.WorldEvent:
    """Translate an OpenClaw ``message.received`` event into a WorldEvent."""
    required = ("channel", "chat_id", "sender_id", "text", "timestamp")
    for key in required:
        if key not in event:
            raise TranslationError(f"message.received event missing key {key!r}")

    body: str = event["text"]
    excerpt, truncated = _truncate(body)
    value = _dict_value(
        {
            "from": {
                "id": str(event["sender_id"]),
                "display_name": str(event.get("sender_name", "")),
            },
            "text_excerpt": excerpt,
            "length_chars": len(body),
            "has_media": bool(event.get("has_media", False)),
            "truncated": truncated,
        },
    )

    return plugin_pb2.WorldEvent(
        id=_event_id(),
        timestamp=_make_timestamp(_parse_iso8601(event["timestamp"])),
        source=plugin_id,
        source_version=plugin_version,
        signature=b"",
        entity=plugin_pb2.EntityRef(
            type="conversation",
            entity_id=str(event["chat_id"]),
        ),
        attribute="message_received",
        value=value,
        confidence=DEFAULT_CONFIDENCE,
    )


def translate_skill_invoked(
    event: dict[str, Any],
    *,
    plugin_id: str,
    plugin_version: str,
) -> plugin_pb2.WorldEvent:
    """Translate an OpenClaw ``skill.invoked`` event into a WorldEvent."""
    required = ("skill_name", "call_id", "timestamp")
    for key in required:
        if key not in event:
            raise TranslationError(f"skill.invoked event missing key {key!r}")

    value = _dict_value(
        {
            "skill_name": str(event["skill_name"]),
            "call_id": str(event["call_id"]),
            "invoker": str(event.get("invoker", "openclaw")),
            "parameters_keys": sorted(map(str, event.get("parameters", {}).keys())),
        },
    )

    return plugin_pb2.WorldEvent(
        id=_event_id(),
        timestamp=_make_timestamp(_parse_iso8601(event["timestamp"])),
        source=plugin_id,
        source_version=plugin_version,
        signature=b"",
        entity=plugin_pb2.EntityRef(type="skill_run", entity_id=str(event["call_id"])),
        attribute="skill_invoked",
        value=value,
        confidence=DEFAULT_CONFIDENCE,
    )


def translate_cron_executed(
    event: dict[str, Any],
    *,
    plugin_id: str,
    plugin_version: str,
) -> plugin_pb2.WorldEvent:
    """Translate an OpenClaw ``cron.executed`` event into a WorldEvent."""
    required = ("cron_id", "skill_name", "timestamp")
    for key in required:
        if key not in event:
            raise TranslationError(f"cron.executed event missing key {key!r}")

    value = _dict_value(
        {
            "cron_id": str(event["cron_id"]),
            "skill_name": str(event["skill_name"]),
            "ok": bool(event.get("ok", True)),
        },
    )
    return plugin_pb2.WorldEvent(
        id=_event_id(),
        timestamp=_make_timestamp(_parse_iso8601(event["timestamp"])),
        source=plugin_id,
        source_version=plugin_version,
        signature=b"",
        entity=plugin_pb2.EntityRef(type="cron_job", entity_id=str(event["cron_id"])),
        attribute="cron_executed",
        value=value,
        confidence=DEFAULT_CONFIDENCE,
    )


def translate_approval_event(
    event: dict[str, Any],
    *,
    plugin_id: str,
    plugin_version: str,
) -> plugin_pb2.WorldEvent:
    """Translate an OpenClaw ``approval.responded`` event into a WorldEvent."""
    required = ("approval_id", "outcome", "timestamp")
    for key in required:
        if key not in event:
            raise TranslationError(f"approval.responded event missing key {key!r}")

    value = _dict_value(
        {
            "approval_id": str(event["approval_id"]),
            "outcome": str(event["outcome"]).lower(),
            "feedback": str(event.get("feedback", "")),
        },
    )
    return plugin_pb2.WorldEvent(
        id=_event_id(),
        timestamp=_make_timestamp(_parse_iso8601(event["timestamp"])),
        source=plugin_id,
        source_version=plugin_version,
        signature=b"",
        entity=plugin_pb2.EntityRef(type="approval", entity_id=str(event["approval_id"])),
        attribute="approval_responded",
        value=value,
        confidence=DEFAULT_CONFIDENCE,
    )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

type Translator = Callable[..., plugin_pb2.WorldEvent]

TRANSLATORS: dict[str, Translator] = {
    "message.received": translate_message_received,
    "skill.invoked": translate_skill_invoked,
    "cron.executed": translate_cron_executed,
    "approval.responded": translate_approval_event,
}


def translate(
    event: dict[str, Any],
    *,
    plugin_id: str,
    plugin_version: str,
) -> plugin_pb2.WorldEvent:
    """Dispatch *event* to the translator matching its ``kind`` field.

    Args:
        event: OpenClaw event dict.
        plugin_id: Plugin identifier to stamp on the resulting WorldEvent.
        plugin_version: Plugin version to stamp on the resulting WorldEvent.

    Returns:
        An unsigned :class:`plugin_pb2.WorldEvent` ready for signing.

    Raises:
        TranslationError: If ``event['kind']`` is unknown or required fields
            are missing.
    """
    kind = event.get("kind")
    if not isinstance(kind, str):
        raise TranslationError("event is missing required string field 'kind'")
    translator = TRANSLATORS.get(kind)
    if translator is None:
        raise TranslationError(f"no translator registered for event kind {kind!r}")
    return translator(event, plugin_id=plugin_id, plugin_version=plugin_version)


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign_event(
    event: plugin_pb2.WorldEvent,
    private_key: Ed25519PrivateKey,
) -> plugin_pb2.WorldEvent:
    """Attach an ed25519 signature to *event* in place and return it.

    The signed payload is ``MessageToDict(event, preserving_proto_field_name=True)``
    minus the ``signature`` field, JCS-canonicalised. This is the same contract
    the CoreMind daemon applies in ``CoreMindHost.EmitEvent``.
    """
    unsigned_dict = MessageToDict(event, preserving_proto_field_name=True)
    unsigned_dict.pop("signature", None)
    payload = canonical_json(unsigned_dict)
    event.signature = sign(payload, private_key)
    return event
