"""Unit tests for src/coremind/world/pipeline.py — WorldEncodingPipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from coremind.world.compressed_prompt import CompressedPromptBuilder
from coremind.world.differ import SnapshotDiffer
from coremind.world.embeddings import EmbeddingEncoder, EncoderError
from coremind.world.model import Entity, JsonValue, WorldSnapshot
from coremind.world.pipeline import WorldEncodingPipeline
from coremind.world.snapshot_memory import SnapshotMemory

_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
_DIM = 768


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entity(
    display_name: str,
    entity_type: str = "light",
    **properties: JsonValue,
) -> Entity:
    return Entity(
        type=entity_type,
        display_name=display_name,
        created_at=_NOW,
        updated_at=_NOW,
        properties=properties,
        source_plugins=["test"],
    )


def _make_snapshot(entity_names: list[str]) -> WorldSnapshot:
    return WorldSnapshot(
        taken_at=_NOW,
        entities=[_make_entity(name, state="on") for name in entity_names],
    )


def _make_encoder(*, fail: bool = False) -> MagicMock:
    """Build a mock EmbeddingEncoder."""
    mock = MagicMock(spec=EmbeddingEncoder)
    if fail:
        mock.encode_snapshot = AsyncMock(side_effect=EncoderError("unavailable"))
    else:
        mock.encode_snapshot = AsyncMock(return_value=[0.1] * _DIM)
    return mock


def _make_memory() -> MagicMock:
    """Build a mock SnapshotMemory."""
    mock = MagicMock(spec=SnapshotMemory)
    mock.store = AsyncMock()
    mock.find_similar = AsyncMock(return_value=[])
    return mock


def _make_pipeline(
    *,
    encoder: MagicMock | None = None,
    fail_encoder: bool = False,
) -> WorldEncodingPipeline:
    """Build a pipeline with mocked dependencies."""
    enc = encoder or _make_encoder(fail=fail_encoder)
    memory = _make_memory()
    differ = SnapshotDiffer()
    builder = CompressedPromptBuilder(memory, top_k=3)
    return WorldEncodingPipeline(
        encoder=enc,
        differ=differ,
        memory=memory,
        prompt_builder=builder,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPipelineNormalPath:
    @pytest.mark.asyncio
    async def test_produces_prompt_without_fallback(self) -> None:
        pipeline = _make_pipeline()

        result = await pipeline.process(_make_snapshot(["a", "b"]))

        assert result.full_fallback is None
        assert not pipeline.fallback_active

    @pytest.mark.asyncio
    async def test_stores_embedding_in_memory(self) -> None:
        pipeline = _make_pipeline()

        await pipeline.process(_make_snapshot(["a"]))

        pipeline._memory.store.assert_awaited_once()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_tracks_previous_snapshot(self) -> None:
        pipeline = _make_pipeline()

        await pipeline.process(_make_snapshot(["a"]))

        assert pipeline._previous_snapshot is not None
        assert len(pipeline._previous_snapshot.entities) == 1

    @pytest.mark.asyncio
    async def test_second_call_diffs_against_first(self) -> None:
        pipeline = _make_pipeline()

        await pipeline.process(_make_snapshot(["a"]))
        result = await pipeline.process(_make_snapshot(["a", "b"]))

        # Second snapshot added "b"
        assert "1 added" in result.summary


class TestPipelineFallback:
    @pytest.mark.asyncio
    async def test_fallback_on_encoder_error(self) -> None:
        pipeline = _make_pipeline(fail_encoder=True)

        result = await pipeline.process(_make_snapshot(["a", "b"]))

        assert result.full_fallback is not None
        assert pipeline.fallback_active
        assert "embedding service unavailable" in result.summary

    @pytest.mark.asyncio
    async def test_fallback_contains_entity_json(self) -> None:
        pipeline = _make_pipeline(fail_encoder=True)

        result = await pipeline.process(_make_snapshot(["bureau"]))

        assert result.full_fallback is not None
        assert "bureau" in result.full_fallback

    @pytest.mark.asyncio
    async def test_still_tracks_previous_in_fallback(self) -> None:
        pipeline = _make_pipeline(fail_encoder=True)

        await pipeline.process(_make_snapshot(["a"]))

        assert pipeline._previous_snapshot is not None


class TestPipelineRecovery:
    @pytest.mark.asyncio
    async def test_recovers_after_encoder_returns(self) -> None:
        encoder = _make_encoder(fail=True)
        pipeline = _make_pipeline(encoder=encoder)

        # First call: fallback
        r1 = await pipeline.process(_make_snapshot(["a"]))
        assert pipeline.fallback_active
        assert r1.full_fallback is not None

        # Encoder recovers
        encoder.encode_snapshot = AsyncMock(return_value=[0.1] * _DIM)

        # Second call: normal
        r2 = await pipeline.process(_make_snapshot(["a", "b"]))
        assert not pipeline.fallback_active
        assert r2.full_fallback is None


class TestPipelineTokenBudget:
    @pytest.mark.asyncio
    async def test_realistic_snapshot_under_3000_tokens(self) -> None:
        """A 48-entity snapshot with 3 changes should produce <3000 tokens."""
        entities_prev = [
            _make_entity(f"entity_{i}", entity_type="light", state="off") for i in range(48)
        ]
        entities_curr = [
            _make_entity(f"entity_{i}", entity_type="light", state="off") for i in range(48)
        ]
        # Change 3 entities
        for i in range(3):
            entities_curr[i] = _make_entity(
                f"entity_{i}", entity_type="light", state="on", brightness="100"
            )

        pipeline = _make_pipeline()

        # First call to establish baseline
        await pipeline.process(WorldSnapshot(taken_at=_NOW, entities=entities_prev))

        # Second call with 3 changes
        result = await pipeline.process(WorldSnapshot(taken_at=_NOW, entities=entities_curr))

        assert result.estimated_tokens < 3000
