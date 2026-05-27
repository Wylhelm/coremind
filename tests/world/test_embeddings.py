"""Unit tests for src/coremind/world/embeddings.py — EmbeddingEncoder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from coremind.world.embeddings import EmbeddingEncoder, EncoderError, EncoderStats
from coremind.world.model import Entity, JsonValue, WorldSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIM = 768


def _make_embedder(
    *,
    return_vector: list[float] | None = None,
    raise_error: bool = False,
) -> AsyncMock:
    """Build a mock embedder satisfying the Embedder protocol."""
    mock = AsyncMock()
    if raise_error:
        mock.embed.side_effect = RuntimeError("connection refused")
    else:
        vec = return_vector if return_vector is not None else [0.1] * DIM
        mock.embed.return_value = vec
    return mock


def _make_entity(
    entity_id: str,
    *,
    state: str = "on",
    updated_at: datetime | None = None,
    extra_props: dict[str, str] | None = None,
) -> Entity:
    """Build a minimal Entity for testing."""
    now = updated_at or datetime.now(UTC)
    props: dict[str, JsonValue] = {"state": state}
    if extra_props:
        props.update(extra_props)
    return Entity(
        type="light",
        display_name=entity_id,
        created_at=now,
        updated_at=now,
        properties=props,
        source_plugins=["test"],
    )


def _make_snapshot(entity_ids: list[str]) -> WorldSnapshot:
    """Build a WorldSnapshot with simple entities."""
    return WorldSnapshot(
        taken_at=datetime.now(UTC),
        entities=[_make_entity(eid) for eid in entity_ids],
    )


def _make_empty_snapshot() -> WorldSnapshot:
    """Build an empty WorldSnapshot."""
    return WorldSnapshot(taken_at=datetime.now(UTC))


# ---------------------------------------------------------------------------
# EncoderStats tests
# ---------------------------------------------------------------------------


class TestEncoderStats:
    def test_cache_hit_rate_empty(self) -> None:
        stats = EncoderStats()
        assert stats.cache_hit_rate == 0.0

    def test_cache_hit_rate_computed(self) -> None:
        stats = EncoderStats(cache_hits=80, cache_misses=20)
        assert abs(stats.cache_hit_rate - 0.8) < 1e-6

    def test_avg_encode_ms_empty(self) -> None:
        stats = EncoderStats()
        assert stats.avg_encode_ms == 0.0

    def test_avg_encode_ms_computed(self) -> None:
        stats = EncoderStats(cache_misses=4, total_encode_ms=100.0)
        assert abs(stats.avg_encode_ms - 25.0) < 1e-6


# ---------------------------------------------------------------------------
# EmbeddingEncoder tests
# ---------------------------------------------------------------------------


class TestEncodeText:
    @pytest.mark.asyncio
    async def test_returns_vector_from_embedder(self) -> None:
        mock = _make_embedder(return_vector=[0.5] * DIM)
        encoder = EmbeddingEncoder(mock, dimension=DIM)

        vector = await encoder.encode_text("hello world")

        assert len(vector) == DIM
        assert vector == [0.5] * DIM

    @pytest.mark.asyncio
    async def test_caches_by_content_hash(self) -> None:
        mock = _make_embedder()
        encoder = EmbeddingEncoder(mock, dimension=DIM)

        v1 = await encoder.encode_text("hello")
        v2 = await encoder.encode_text("hello")

        assert v1 == v2
        assert encoder.stats.cache_hits == 1
        assert encoder.stats.cache_misses == 1
        assert mock.embed.call_count == 1

    @pytest.mark.asyncio
    async def test_different_inputs_not_cached(self) -> None:
        mock = _make_embedder()
        encoder = EmbeddingEncoder(mock, dimension=DIM)

        await encoder.encode_text("hello")
        await encoder.encode_text("world")

        assert encoder.stats.cache_misses == 2
        assert mock.embed.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_encoder_error_on_failure(self) -> None:
        mock = _make_embedder(raise_error=True)
        encoder = EmbeddingEncoder(mock, dimension=DIM)

        with pytest.raises(EncoderError, match="Embedding failed"):
            await encoder.encode_text("hello")

        assert encoder.stats.errors == 1

    @pytest.mark.asyncio
    async def test_cache_eviction_when_full(self) -> None:
        mock = _make_embedder()
        encoder = EmbeddingEncoder(mock, dimension=DIM, cache_size=2)

        await encoder.encode_text("a")
        await encoder.encode_text("b")
        await encoder.encode_text("c")  # evicts "a"

        assert len(encoder._cache) == 2

        # "a" is evicted — re-encoding hits the embedder again
        await encoder.encode_text("a")
        assert encoder.stats.cache_misses == 4


class TestEncodeEntity:
    @pytest.mark.asyncio
    async def test_encodes_entity_via_text(self) -> None:
        mock = _make_embedder(return_vector=[0.2] * DIM)
        encoder = EmbeddingEncoder(mock, dimension=DIM)
        entity = _make_entity("bureau")

        vector = await encoder.encode_entity(entity)

        assert vector == [0.2] * DIM
        mock.embed.assert_called_once()
        # Verify the text passed to embed is deterministic
        call_text = mock.embed.call_args[0][0]
        assert "light:bureau" in call_text

    @pytest.mark.asyncio
    async def test_entity_to_text_deterministic(self) -> None:
        encoder = EmbeddingEncoder(_make_embedder(), dimension=DIM)
        entity = _make_entity("bureau", extra_props={"brightness": "200", "color": "warm"})

        text = encoder._entity_to_text(entity)

        # Properties are sorted alphabetically
        assert text.index("brightness") < text.index("color")
        assert text.index("color") < text.index("state")
        assert "light:bureau" in text


class TestEncodeSnapshot:
    @pytest.mark.asyncio
    async def test_empty_snapshot_returns_zero_vector(self) -> None:
        mock = _make_embedder()
        encoder = EmbeddingEncoder(mock, dimension=DIM)

        vector = await encoder.encode_snapshot(_make_empty_snapshot())

        assert vector == [0.0] * DIM
        mock.embed.assert_not_called()

    @pytest.mark.asyncio
    async def test_weighted_average_uniform_vectors(self) -> None:
        mock = _make_embedder(return_vector=[1.0] * DIM)
        encoder = EmbeddingEncoder(mock, dimension=DIM)
        snapshot = _make_snapshot(["a", "b", "c"])

        vector = await encoder.encode_snapshot(snapshot)

        # All entity vectors are [1.0]*DIM so weighted avg is also [1.0]*DIM
        assert all(abs(v - 1.0) < 1e-6 for v in vector)

    @pytest.mark.asyncio
    async def test_encodes_all_entities(self) -> None:
        mock = _make_embedder()
        encoder = EmbeddingEncoder(mock, dimension=DIM)
        snapshot = _make_snapshot(["a", "b", "c"])

        await encoder.encode_snapshot(snapshot)

        assert mock.embed.call_count == 3


class TestComputeWeight:
    def test_recent_entity_has_higher_weight(self) -> None:
        encoder = EmbeddingEncoder(_make_embedder(), dimension=DIM)
        fresh = _make_entity("e1", updated_at=datetime.now(UTC))
        stale = _make_entity("e2", updated_at=datetime.now(UTC) - timedelta(hours=20))

        assert encoder._compute_weight(fresh) > encoder._compute_weight(stale)

    def test_complex_entity_has_higher_weight(self) -> None:
        encoder = EmbeddingEncoder(_make_embedder(), dimension=DIM)
        simple = _make_entity("e1")
        complex_e = _make_entity("e2", extra_props={f"p{i}": str(i) for i in range(8)})

        assert encoder._compute_weight(complex_e) > encoder._compute_weight(simple)

    def test_weight_never_zero(self) -> None:
        encoder = EmbeddingEncoder(_make_embedder(), dimension=DIM)
        # Very old entity
        old = _make_entity("e", updated_at=datetime.now(UTC) - timedelta(hours=48))

        assert encoder._compute_weight(old) > 0.0
