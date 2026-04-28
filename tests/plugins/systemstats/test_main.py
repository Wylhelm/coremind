"""Tests for coremind_plugin_systemstats.main — signing and event construction."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import coremind_plugin_systemstats.main as main_module
import pytest
from coremind_plugin_systemstats.main import (
    CONFIDENCE,
    PLUGIN_ID,
    PLUGIN_VERSION,
    _emit_stats,
    build_signed_event,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from google.protobuf.json_format import MessageToDict

from coremind.crypto.signatures import canonical_json, verify

# ---------------------------------------------------------------------------
# build_signed_event — signature validity
# ---------------------------------------------------------------------------


def test_build_signed_event_signature_verifies(private_key: Ed25519PrivateKey) -> None:
    """build_signed_event produces a signature that verifies against the same payload."""
    event = build_signed_event(private_key, "cpu_percent", 42.5, "testhost")

    # Reproduce the canonical payload exactly as the daemon does.
    event_dict = MessageToDict(event, preserving_proto_field_name=True)
    event_dict.pop("signature", None)
    payload = canonical_json(event_dict)

    assert verify(payload, event.signature, private_key.public_key()) is True


def test_build_signed_event_tampered_value_fails_verification(
    private_key: Ed25519PrivateKey,
) -> None:
    """A signature produced for one value does not verify for a different value."""
    event_a = build_signed_event(private_key, "cpu_percent", 10.0, "testhost")
    event_b = build_signed_event(private_key, "cpu_percent", 90.0, "testhost")

    # Cross-verify: event_a's signature must not verify event_b's payload.
    event_dict = MessageToDict(event_b, preserving_proto_field_name=True)
    event_dict.pop("signature", None)
    payload = canonical_json(event_dict)

    assert verify(payload, event_a.signature, private_key.public_key()) is False


# ---------------------------------------------------------------------------
# build_signed_event — field correctness
# ---------------------------------------------------------------------------


def test_build_signed_event_source_equals_plugin_id(private_key: Ed25519PrivateKey) -> None:
    """build_signed_event sets source to the canonical plugin ID."""
    event = build_signed_event(private_key, "memory_percent", 75.0, "myhost")

    assert event.source == PLUGIN_ID


def test_build_signed_event_source_version(private_key: Ed25519PrivateKey) -> None:
    """build_signed_event sets source_version to the declared plugin version."""
    event = build_signed_event(private_key, "cpu_percent", 50.0, "myhost")

    assert event.source_version == PLUGIN_VERSION


def test_build_signed_event_entity_type_and_id(private_key: Ed25519PrivateKey) -> None:
    """build_signed_event creates a host entity with the supplied hostname."""
    event = build_signed_event(private_key, "uptime_seconds", 3600, "myhost")

    assert event.entity.type == "host"
    assert event.entity.entity_id == "myhost"


def test_build_signed_event_attribute(private_key: Ed25519PrivateKey) -> None:
    """build_signed_event sets the attribute field correctly."""
    event = build_signed_event(private_key, "uptime_seconds", 3600, "myhost")

    assert event.attribute == "uptime_seconds"


def test_build_signed_event_numeric_value(private_key: Ed25519PrivateKey) -> None:
    """build_signed_event encodes the observed value as a number_value."""
    event = build_signed_event(private_key, "cpu_percent", 55.5, "myhost")

    assert event.value.number_value == pytest.approx(55.5)


def test_build_signed_event_confidence(private_key: Ed25519PrivateKey) -> None:
    """build_signed_event sets confidence to the module constant."""
    event = build_signed_event(private_key, "cpu_percent", 20.0, "myhost")

    assert event.confidence == pytest.approx(CONFIDENCE)


def test_build_signed_event_each_call_gets_unique_id(private_key: Ed25519PrivateKey) -> None:
    """Two calls to build_signed_event produce events with distinct IDs."""
    event_a = build_signed_event(private_key, "cpu_percent", 30.0, "host")
    event_b = build_signed_event(private_key, "cpu_percent", 30.0, "host")

    assert event_a.id != event_b.id


# ---------------------------------------------------------------------------
# _emit_stats — gRPC emission loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_stats_calls_emit_event_three_times(private_key: Ed25519PrivateKey) -> None:
    """_emit_stats calls EmitEvent exactly once for each metric (cpu, memory, uptime)."""
    stub = AsyncMock()

    with (
        patch.object(main_module, "collect_cpu_percent", return_value=50.0),
        patch.object(main_module, "collect_memory_percent", return_value=70.0),
        patch.object(main_module, "collect_uptime_seconds", return_value=3600),
    ):
        await _emit_stats(stub, private_key, "testhost")

    assert stub.EmitEvent.call_count == 3


@pytest.mark.asyncio
async def test_emit_stats_emits_expected_attributes(private_key: Ed25519PrivateKey) -> None:
    """_emit_stats emits events for cpu_percent, memory_percent, and uptime_seconds in order."""
    stub = AsyncMock()

    with (
        patch.object(main_module, "collect_cpu_percent", return_value=10.0),
        patch.object(main_module, "collect_memory_percent", return_value=20.0),
        patch.object(main_module, "collect_uptime_seconds", return_value=99),
    ):
        await _emit_stats(stub, private_key, "testhost")

    emitted_attributes = [call.args[0].attribute for call in stub.EmitEvent.call_args_list]
    assert emitted_attributes == ["cpu_percent", "memory_percent", "uptime_seconds"]


@pytest.mark.asyncio
async def test_emit_stats_attaches_plugin_id_metadata(private_key: Ed25519PrivateKey) -> None:
    """_emit_stats passes x-plugin-id metadata to every EmitEvent call."""
    stub = AsyncMock()

    with (
        patch.object(main_module, "collect_cpu_percent", return_value=0.0),
        patch.object(main_module, "collect_memory_percent", return_value=0.0),
        patch.object(main_module, "collect_uptime_seconds", return_value=0),
    ):
        await _emit_stats(stub, private_key, "testhost")

    for call in stub.EmitEvent.call_args_list:
        metadata = dict(call.kwargs.get("metadata", ()))
        assert metadata.get("x-plugin-id") == PLUGIN_ID


@pytest.mark.asyncio
async def test_emit_stats_events_have_valid_signatures(private_key: Ed25519PrivateKey) -> None:
    """Each WorldEvent emitted by _emit_stats carries a verifiable ed25519 signature."""
    stub = AsyncMock()

    with (
        patch.object(main_module, "collect_cpu_percent", return_value=33.0),
        patch.object(main_module, "collect_memory_percent", return_value=55.0),
        patch.object(main_module, "collect_uptime_seconds", return_value=1234),
    ):
        await _emit_stats(stub, private_key, "testhost")

    public_key = private_key.public_key()
    for call in stub.EmitEvent.call_args_list:
        event = call.args[0]
        event_dict = MessageToDict(event, preserving_proto_field_name=True)
        event_dict.pop("signature", None)
        payload = canonical_json(event_dict)
        assert verify(payload, event.signature, public_key) is True
