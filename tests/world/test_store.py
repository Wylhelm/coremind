"""Tests for src/coremind/world/store.py."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from coremind.crypto.signatures import sign
from coremind.errors import SignatureError, StoreError
from coremind.world.model import EntityRef, WorldEventRecord, WorldSnapshot
from coremind.world.store import (
    WorldStore,
    _event_signing_payload,
    _flatten_query_result,
    _parse_dt,
    _parse_entity_ref,
    _verify_event_signature,
)

# Fixtures


@pytest.fixture()
def signed_event(private_key: Ed25519PrivateKey) -> WorldEventRecord:
    """Build a fully signed WorldEventRecord for testing."""
    event = WorldEventRecord(
        id="evt-001",
        timestamp=datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC),
        source="plugin-systemstats",
        source_version="0.1.0",
        signature=None,  # will be filled below
        entity=EntityRef(type="host", id="myhost"),
        attribute="cpu_percent",
        value=42.0,
        confidence=1.0,
    )
    payload = _event_signing_payload(event)
    raw_sig = sign(payload, private_key)
    return event.model_copy(update={"signature": base64.b64encode(raw_sig).decode()})


@pytest.fixture()
def signed_event_with_unit(private_key: Ed25519PrivateKey) -> WorldEventRecord:
    """Build a fully signed WorldEventRecord that includes a unit field."""
    event = WorldEventRecord(
        id="evt-002",
        timestamp=datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC),
        source="plugin-systemstats",
        source_version="0.2.0",
        signature=None,
        entity=EntityRef(type="host", id="myhost"),
        attribute="temperature",
        value=72.3,
        confidence=0.9,
        unit="celsius",
    )
    payload = _event_signing_payload(event)
    raw_sig = sign(payload, private_key)
    return event.model_copy(update={"signature": base64.b64encode(raw_sig).decode()})


@pytest.fixture()
def key_resolver(public_key: Ed25519PublicKey) -> MagicMock:
    """Return a resolver that maps 'plugin-systemstats' → public_key."""
    return MagicMock(return_value=public_key)


@pytest.fixture()
def store(key_resolver: MagicMock) -> WorldStore:
    """WorldStore instance with a mock key resolver (not connected)."""
    return WorldStore(
        url="ws://127.0.0.1:8000/rpc",
        username="root",
        password="root",  # noqa: S106
        key_resolver=key_resolver,
    )


# _event_signing_payload


def test_signing_payload_is_deterministic(signed_event: WorldEventRecord) -> None:
    payload_a = _event_signing_payload(signed_event)
    payload_b = _event_signing_payload(signed_event)

    assert payload_a == payload_b


def test_signing_payload_excludes_signature_field(signed_event: WorldEventRecord) -> None:
    payload = _event_signing_payload(signed_event)
    parsed = payload.decode()

    assert "signature" not in parsed


def test_signing_payload_includes_unit_when_present() -> None:
    event = WorldEventRecord(
        id="evt-unit",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        source="plugin-x",
        source_version="1.0.0",
        signature=None,
        entity=EntityRef(type="host", id="box"),
        attribute="temp",
        value=72.3,
        confidence=0.9,
        unit="celsius",
    )
    payload = _event_signing_payload(event).decode()

    assert "celsius" in payload


# _verify_event_signature


def test_verify_accepts_valid_signature(
    signed_event: WorldEventRecord,
    key_resolver: MagicMock,
) -> None:
    # Should not raise
    _verify_event_signature(signed_event, key_resolver)


def test_verify_rejects_missing_signature(
    key_resolver: MagicMock,
) -> None:
    event = WorldEventRecord(
        id="evt-nosig",
        timestamp=datetime(2026, 4, 19, tzinfo=UTC),
        source="plugin-systemstats",
        source_version="0.1.0",
        signature=None,
        entity=EntityRef(type="host", id="myhost"),
        attribute="cpu_percent",
        value=10.0,
        confidence=1.0,
    )

    with pytest.raises(SignatureError, match="no signature"):
        _verify_event_signature(event, key_resolver)


def test_verify_rejects_unknown_plugin(
    signed_event: WorldEventRecord,
) -> None:
    unknown_resolver = MagicMock(return_value=None)

    with pytest.raises(SignatureError, match="unknown plugin"):
        _verify_event_signature(signed_event, unknown_resolver)


def test_verify_rejects_tampered_payload(
    signed_event: WorldEventRecord,
    key_resolver: MagicMock,
) -> None:
    tampered = signed_event.model_copy(update={"value": 99.9})

    with pytest.raises(SignatureError, match="verification failed"):
        _verify_event_signature(tampered, key_resolver)


def test_verify_rejects_tampered_signature(
    signed_event: WorldEventRecord,
    key_resolver: MagicMock,
) -> None:
    # Flip the first byte of the base64-decoded signature
    raw = base64.b64decode(signed_event.signature)  # type: ignore[arg-type]
    corrupted = bytes([raw[0] ^ 0xFF]) + raw[1:]
    bad_event = signed_event.model_copy(update={"signature": base64.b64encode(corrupted).decode()})

    with pytest.raises(SignatureError, match="verification failed"):
        _verify_event_signature(bad_event, key_resolver)


def test_verify_rejects_malformed_base64(
    signed_event: WorldEventRecord,
    key_resolver: MagicMock,
) -> None:
    bad_event = signed_event.model_copy(update={"signature": "not!!valid//base64=="})

    with pytest.raises(SignatureError, match="malformed signature"):
        _verify_event_signature(bad_event, key_resolver)


# WorldStore.apply_event — unit (mocked DB)


@pytest.mark.asyncio
async def test_apply_event_raises_signature_error_on_bad_sig(
    store: WorldStore,
    signed_event: WorldEventRecord,
) -> None:
    tampered = signed_event.model_copy(update={"value": 99.9})

    with pytest.raises(SignatureError):
        await store.apply_event(tampered)


@pytest.mark.asyncio
async def test_apply_event_calls_db_query_twice_on_success(
    store: WorldStore,
    signed_event: WorldEventRecord,
) -> None:
    mock_db = AsyncMock()
    store._db = mock_db  # inject mock connection

    await store.apply_event(signed_event)

    assert mock_db.query.call_count == 2  # entity upsert + event insert


@pytest.mark.asyncio
async def test_apply_event_raises_store_error_on_db_failure(
    store: WorldStore,
    signed_event: WorldEventRecord,
) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = RuntimeError("db is down")
    store._db = mock_db

    with pytest.raises(StoreError, match="failed to upsert entity"):
        await store.apply_event(signed_event)


@pytest.mark.asyncio
async def test_apply_event_idempotent_second_call_also_succeeds(
    store: WorldStore,
    signed_event: WorldEventRecord,
) -> None:
    """Re-applying the same event is a no-op: both calls succeed and use UPSERT."""
    mock_db = AsyncMock()
    store._db = mock_db

    await store.apply_event(signed_event)
    await store.apply_event(signed_event)

    assert mock_db.query.call_count == 4  # 2 queries x 2 calls
    # Both the entity and event queries use UPSERT — idempotent on repeat calls.
    for call in mock_db.query.call_args_list:
        sql: str = call.args[0]
        assert "UPSERT" in sql, f"Expected UPSERT for idempotency, got: {sql[:80]}"


# WorldStore.snapshot — unit (mocked DB)


@pytest.mark.asyncio
async def test_snapshot_returns_world_snapshot_type(store: WorldStore) -> None:
    mock_db = AsyncMock()
    mock_db.query.return_value = []
    store._db = mock_db

    result = await store.snapshot()

    assert isinstance(result, WorldSnapshot)


@pytest.mark.asyncio
async def test_snapshot_raises_store_error_on_db_failure(store: WorldStore) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = RuntimeError("db is down")
    store._db = mock_db

    with pytest.raises(StoreError, match="failed to fetch world snapshot"):
        await store.snapshot()


# WorldStore.recent_events — unit (mocked DB)


@pytest.mark.asyncio
async def test_recent_events_returns_list(store: WorldStore) -> None:
    mock_db = AsyncMock()
    mock_db.query.return_value = []
    store._db = mock_db

    result = await store.recent_events(since=datetime(2026, 1, 1, tzinfo=UTC))

    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_recent_events_raises_store_error_on_db_failure(store: WorldStore) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = RuntimeError("connection lost")
    store._db = mock_db

    with pytest.raises(StoreError, match="failed to query recent events"):
        await store.recent_events(since=datetime(2026, 1, 1, tzinfo=UTC))


# WorldStore.connect — unit (mocked AsyncSurreal)


@pytest.mark.asyncio
async def test_connect_raises_store_error_on_failure(store: WorldStore) -> None:
    with patch("coremind.world.store.AsyncSurreal") as mock_cls:
        mock_conn = AsyncMock()
        mock_conn.connect.side_effect = ConnectionRefusedError("refused")
        mock_cls.return_value = mock_conn

        with pytest.raises(StoreError, match="failed to connect"):
            await store.connect()


# _parse_dt


def test_parse_dt_coerces_naive_datetime_to_utc() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001 — intentionally naive to test coercion

    result = _parse_dt(naive)

    assert result.tzinfo is UTC
    assert result == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# WorldStore.apply_event — source_version and unit persistence


@pytest.mark.asyncio
async def test_apply_event_persists_source_version_and_unit(
    store: WorldStore,
    signed_event_with_unit: WorldEventRecord,
) -> None:
    mock_db = AsyncMock()
    store._db = mock_db

    await store.apply_event(signed_event_with_unit)

    # Second call is the event UPSERT; first is the entity upsert
    event_params = mock_db.query.call_args_list[1].args[1]
    assert event_params["source_version"] == "0.2.0"
    assert event_params["unit"] == "celsius"


# WorldStore.snapshot — row parsing (exercising _parse_* helpers)


@pytest.mark.asyncio
async def test_snapshot_parses_entity_rows_correctly(store: WorldStore) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = [
        [
            [
                {
                    "type": "host",
                    "display_name": "myhost",
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "updated_at": datetime(2026, 4, 19, tzinfo=UTC),
                    "properties": {"cpu_percent": 42.0},
                    "source_plugins": ["plugin-systemstats"],
                }
            ]
        ],
        [],  # relationship rows
        [],  # event rows
    ]
    store._db = mock_db

    result = await store.snapshot()

    assert len(result.entities) == 1
    assert result.entities[0].type == "host"
    assert result.entities[0].properties["cpu_percent"] == 42.0


@pytest.mark.asyncio
async def test_snapshot_parses_relationship_rows_correctly(store: WorldStore) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = [
        [],  # entity rows
        [
            [
                {
                    "type": "runs_on",
                    "from": {"type": "process", "id": "nginx"},
                    "to": {"type": "host", "id": "myhost"},
                    "weight": 0.8,
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "last_reinforced": datetime(2026, 4, 19, tzinfo=UTC),
                }
            ]
        ],
        [],  # event rows
    ]
    store._db = mock_db

    result = await store.snapshot()

    assert len(result.relationships) == 1
    assert result.relationships[0].type == "runs_on"
    assert result.relationships[0].weight == 0.8


@pytest.mark.asyncio
async def test_snapshot_parses_event_source_version_and_unit(store: WorldStore) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = [
        [],  # entity rows
        [],  # relationship rows
        [
            [
                {
                    "id": "event:evt-001",
                    "timestamp": datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC),
                    "source": "plugin-systemstats",
                    "source_version": "0.1.0",
                    "entity": {"type": "host", "id": "myhost"},
                    "attribute": "temperature",
                    "value": 72.3,
                    "confidence": 0.9,
                    "signature": "abc123",
                    "unit": "celsius",
                }
            ]
        ],
    ]
    store._db = mock_db

    result = await store.snapshot()

    assert len(result.recent_events) == 1
    assert result.recent_events[0].source_version == "0.1.0"
    assert result.recent_events[0].unit == "celsius"


@pytest.mark.asyncio
async def test_snapshot_uses_unknown_fallback_when_source_version_missing(
    store: WorldStore,
) -> None:
    mock_db = AsyncMock()
    mock_db.query.side_effect = [
        [],  # entity rows
        [],  # relationship rows
        [
            [
                {
                    "id": "event:evt-004",
                    "timestamp": datetime(2026, 4, 19, tzinfo=UTC),
                    "source": "plugin-legacy",
                    "entity": {"type": "host", "id": "box"},
                    "attribute": "up",
                    "value": True,
                    "confidence": 1.0,
                }
            ]
        ],
    ]
    store._db = mock_db

    result = await store.snapshot()

    assert result.recent_events[0].source_version == "unknown"
    assert result.recent_events[0].unit is None


# WorldStore.recent_events — row parsing (exercising _parse_event_rows)


@pytest.mark.asyncio
async def test_recent_events_parses_source_version_and_unit(store: WorldStore) -> None:
    mock_db = AsyncMock()
    mock_db.query.return_value = [
        [
            {
                "id": "event:evt-003",
                "timestamp": datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC),
                "source": "plugin-systemstats",
                "source_version": "0.1.0",
                "entity": {"type": "host", "id": "myhost"},
                "attribute": "mem_used_gb",
                "value": 8.0,
                "confidence": 1.0,
                "signature": "abc123",
                "unit": "gigabytes",
            }
        ]
    ]
    store._db = mock_db

    result = await store.recent_events(since=datetime(2026, 1, 1, tzinfo=UTC))

    assert len(result) == 1
    assert result[0].source_version == "0.1.0"
    assert result[0].unit == "gigabytes"


# _flatten_query_result


def test_flatten_returns_flat_list_from_nested() -> None:
    nested = [[{"a": 1}, {"b": 2}], [{"c": 3}]]
    result = _flatten_query_result(nested)
    assert result == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_flatten_handles_empty_input() -> None:
    assert _flatten_query_result([]) == []


def test_flatten_handles_non_list_input() -> None:
    assert _flatten_query_result(None) == []


# _parse_dt


def test_parse_dt_accepts_aware_datetime() -> None:
    dt = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    assert _parse_dt(dt) == dt


def test_parse_dt_adds_utc_to_naive_datetime() -> None:
    naive = datetime(2026, 4, 19, 12, 0)  # noqa: DTZ001 — intentionally testing naive input
    result = _parse_dt(naive)
    assert result.tzinfo is not None


def test_parse_dt_parses_iso_string() -> None:
    result = _parse_dt("2026-04-19T12:00:00+00:00")
    assert result.year == 2026


def test_parse_dt_raises_on_invalid() -> None:
    with pytest.raises((ValueError, TypeError)):
        _parse_dt(12345)


# _parse_entity_ref


def test_parse_entity_ref_from_dict() -> None:
    ref = _parse_entity_ref({"type": "host", "id": "myhost"})
    assert ref.type == "host"
    assert ref.id == "myhost"


def test_parse_entity_ref_from_colon_string() -> None:
    ref = _parse_entity_ref("host:myhost")
    assert ref.type == "host"
    assert ref.id == "myhost"


def test_parse_entity_ref_raises_on_invalid() -> None:
    with pytest.raises((ValueError, KeyError)):
        _parse_entity_ref(42)
