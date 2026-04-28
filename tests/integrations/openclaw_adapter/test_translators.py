"""Unit tests for OpenClaw → CoreMind event translators."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from coremind_plugin_openclaw import translators
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp

from coremind.crypto.signatures import canonical_json, sign, verify
from coremind.plugin_api._generated import plugin_pb2

PLUGIN_ID = "coremind.plugin.openclaw_adapter"
PLUGIN_VERSION = "0.1.0"

GOLDEN_VECTOR_PATH = Path(__file__).parent / "golden_signature.json"


def test_translate_message_received_happy_path() -> None:
    raw = {
        "kind": "message.received",
        "channel": "telegram",
        "chat_id": "telegram:6394043863",
        "sender_id": "6394043863",
        "sender_name": "Guillaume",
        "text": "what's for dinner tonight?",
        "has_media": False,
        "timestamp": "2026-04-19T20:14:02Z",
    }
    event = translators.translate(
        raw,
        plugin_id=PLUGIN_ID,
        plugin_version=PLUGIN_VERSION,
    )
    assert event.source == PLUGIN_ID
    assert event.entity.type == "conversation"
    assert event.entity.entity_id == "telegram:6394043863"
    assert event.attribute == "message_received"
    value = MessageToDict(event.value)
    assert value["text_excerpt"] == "what's for dinner tonight?"
    text_value = raw["text"]
    assert isinstance(text_value, str)
    assert value["length_chars"] == len(text_value)
    assert value["truncated"] is False


def test_message_body_is_truncated() -> None:
    body = "x" * 500
    raw = {
        "kind": "message.received",
        "channel": "telegram",
        "chat_id": "chat1",
        "sender_id": "u1",
        "sender_name": "A",
        "text": body,
        "timestamp": "2026-04-19T20:14:02+00:00",
    }
    event = translators.translate(raw, plugin_id=PLUGIN_ID, plugin_version=PLUGIN_VERSION)
    value = MessageToDict(event.value)
    assert value["truncated"] is True
    assert len(value["text_excerpt"]) == translators.MESSAGE_EXCERPT_MAX_CHARS
    assert value["length_chars"] == 500


def test_translate_skill_invoked() -> None:
    raw = {
        "kind": "skill.invoked",
        "skill_name": "weather.lookup",
        "call_id": "call-42",
        "parameters": {"city": "Ottawa"},
        "timestamp": "2026-04-19T20:14:02Z",
    }
    event = translators.translate(raw, plugin_id=PLUGIN_ID, plugin_version=PLUGIN_VERSION)
    assert event.entity.type == "skill_run"
    assert event.attribute == "skill_invoked"
    value = MessageToDict(event.value)
    assert value["skill_name"] == "weather.lookup"
    assert value["parameters_keys"] == ["city"]


def test_translate_cron_executed() -> None:
    raw = {
        "kind": "cron.executed",
        "cron_id": "daily-briefing",
        "skill_name": "briefing.generate",
        "ok": True,
        "timestamp": "2026-04-19T07:00:00Z",
    }
    event = translators.translate(raw, plugin_id=PLUGIN_ID, plugin_version=PLUGIN_VERSION)
    assert event.entity.type == "cron_job"
    assert event.attribute == "cron_executed"


def test_translate_approval_responded() -> None:
    raw = {
        "kind": "approval.responded",
        "approval_id": "appr-1",
        "outcome": "APPROVED",
        "feedback": "go ahead",
        "timestamp": "2026-04-19T20:14:02Z",
    }
    event = translators.translate(raw, plugin_id=PLUGIN_ID, plugin_version=PLUGIN_VERSION)
    value = MessageToDict(event.value)
    assert value["outcome"] == "approved"
    assert value["feedback"] == "go ahead"


def test_unknown_kind_raises() -> None:
    with pytest.raises(translators.TranslationError):
        translators.translate(
            {"kind": "weird.kind", "timestamp": "2026-01-01T00:00:00Z"},
            plugin_id=PLUGIN_ID,
            plugin_version=PLUGIN_VERSION,
        )


def test_missing_kind_raises() -> None:
    with pytest.raises(translators.TranslationError):
        translators.translate({}, plugin_id=PLUGIN_ID, plugin_version=PLUGIN_VERSION)


def test_missing_required_key_raises() -> None:
    with pytest.raises(translators.TranslationError):
        translators.translate(
            {"kind": "message.received", "timestamp": "2026-01-01T00:00:00Z"},
            plugin_id=PLUGIN_ID,
            plugin_version=PLUGIN_VERSION,
        )


def test_sign_event_round_trip() -> None:
    key = Ed25519PrivateKey.generate()
    raw = {
        "kind": "message.received",
        "channel": "telegram",
        "chat_id": "chat1",
        "sender_id": "u1",
        "sender_name": "A",
        "text": "hi",
        "timestamp": "2026-04-19T20:14:02Z",
    }
    event = translators.translate(raw, plugin_id=PLUGIN_ID, plugin_version=PLUGIN_VERSION)
    signed = translators.sign_event(event, key)
    assert signed.signature
    event_dict = MessageToDict(signed, preserving_proto_field_name=True)
    sig_bytes = signed.signature
    event_dict.pop("signature", None)
    assert verify(canonical_json(event_dict), sig_bytes, key.public_key())


def test_golden_signature_vector_matches() -> None:
    """Canonical encoding + signature must be byte-stable across runtimes.

    This is the cross-language contract test: the TypeScript signer in
    ``openclaw_side/src/signer.ts`` MUST reproduce the same canonical
    payload and signature bytes from the same event. If this test fails,
    event ingest will also fail with ``UNAUTHENTICATED`` in production.
    """
    golden = json.loads(GOLDEN_VECTOR_PATH.read_text())
    seed = bytes.fromhex(golden["private_key_seed_hex"])
    priv = Ed25519PrivateKey.from_private_bytes(seed)

    # Reconstruct the exact event described by the vector.
    value = Value()
    ParseDict(golden["event"]["value"], value.struct_value)
    ts = Timestamp()
    ts.FromJsonString(golden["event"]["timestamp"])
    event = plugin_pb2.WorldEvent(
        id=golden["event"]["id"],
        timestamp=ts,
        source=golden["event"]["source"],
        source_version=golden["event"]["source_version"],
        signature=b"",
        entity=plugin_pb2.EntityRef(
            type=golden["event"]["entity"]["type"],
            entity_id=golden["event"]["entity"]["entity_id"],
        ),
        attribute=golden["event"]["attribute"],
        value=value,
        confidence=float(golden["event"]["confidence"]),
    )

    # Canonical payload bytes must match exactly.
    d = MessageToDict(event, preserving_proto_field_name=True)
    d.pop("signature", None)
    payload = canonical_json(d)
    assert payload.decode("utf-8") == golden["canonical_payload"], (
        "canonical JSON encoding drifted — TS and Py halves will disagree"
    )

    # Signature must match the frozen vector byte-for-byte.
    sig = sign(payload, priv)
    assert sig.hex() == golden["signature_hex"], (
        "signature bytes drifted — regenerate the golden vector only if "
        "the canonical encoding contract intentionally changed"
    )


def test_approval_outcome_wire_encoding() -> None:
    """ApprovalOutcome is serialised as an enum int on the wire.

    Guards against silent regressions in the proto loader config on the TS
    side. The Python-side dispatcher reads the enum int and converts to a
    lowercase string via ``ApprovalOutcome.Name`` — any drift here breaks
    the TS ``buildHandlers`` mapping in openclaw_extension.ts.
    """
    from coremind_plugin_openclaw._generated import adapter_pb2  # noqa: PLC0415

    # The generated enum is int-valued.
    assert isinstance(adapter_pb2.APPROVAL_OUTCOME_APPROVED, int)
    assert adapter_pb2.APPROVAL_OUTCOME_APPROVED == 1
    # Round-trip via ApprovalResult preserves the int.
    result = adapter_pb2.ApprovalResult(outcome=adapter_pb2.APPROVAL_OUTCOME_APPROVED)
    serialized = result.SerializeToString()
    parsed = adapter_pb2.ApprovalResult()
    parsed.ParseFromString(serialized)
    assert parsed.outcome == 1
    # String form matches the mapping used in action_dispatcher.py.
    name = adapter_pb2.ApprovalOutcome.Name(parsed.outcome)
    assert name == "APPROVAL_OUTCOME_APPROVED"
    assert name.removeprefix("APPROVAL_OUTCOME_").lower() == "approved"
