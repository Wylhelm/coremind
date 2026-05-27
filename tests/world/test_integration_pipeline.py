"""Integration tests for Phase 3E — embedding pipeline wired into L4/L5 loops."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coremind.config import DaemonConfig, EmbeddingPipelineConfig
from coremind.intention.loop import IntentionLoop, IntentionLoopConfig
from coremind.intention.prompts import render_prompt as render_intention_prompt
from coremind.intention.schemas import QuestionBatch
from coremind.reasoning.loop import ReasoningLoop
from coremind.reasoning.prompts import render_prompt as render_reasoning_prompt
from coremind.reasoning.schemas import ReasoningOutput, TokenUsage
from coremind.world.compressed_prompt import CompressedPrompt
from coremind.world.model import Entity, JsonValue, WorldSnapshot
from coremind.world.pipeline import WorldEncodingPipeline

_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


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


def _make_snapshot() -> WorldSnapshot:
    return WorldSnapshot(
        taken_at=_NOW,
        entities=[
            _make_entity("bureau", state="on"),
            _make_entity("salon", state="off"),
        ],
    )


def _make_compressed_prompt() -> CompressedPrompt:
    return CompressedPrompt(
        summary="2 entities, 1 changed",
        changes_text="~ bureau: state on → off",
        similar_states_text="1. 4h ago (sim=0.94): 2 entities, no changes",
        key_metrics_text="lights: 1 on / 1 off",
        full_fallback=None,
    )


def _make_mock_pipeline(compressed: CompressedPrompt | None = None) -> MagicMock:
    """Build a mock WorldEncodingPipeline returning a fixed CompressedPrompt."""
    mock = MagicMock(spec=WorldEncodingPipeline)
    mock.process = AsyncMock(return_value=compressed or _make_compressed_prompt())
    mock.fallback_active = False
    return mock


def _make_mock_llm() -> MagicMock:
    """Build a mock LLM that returns a minimal QuestionBatch."""
    mock = MagicMock()
    mock.complete_structured = AsyncMock(return_value=QuestionBatch(questions=[]))
    return mock


def _make_mock_snapshot_provider() -> MagicMock:
    mock = MagicMock()
    mock.snapshot = AsyncMock(return_value=_make_snapshot())
    return mock


def _make_mock_reasoning_feed() -> MagicMock:
    mock = MagicMock()
    mock.list_cycles = AsyncMock(return_value=[])
    return mock


def _make_mock_intent_store() -> MagicMock:
    mock = MagicMock()
    mock.recent = AsyncMock(return_value=[])
    mock.save = AsyncMock()
    return mock


def _make_mock_router() -> MagicMock:
    mock = MagicMock()
    mock.route = AsyncMock()
    return mock


# ---------------------------------------------------------------------------
# Tests: Prompt templates
# ---------------------------------------------------------------------------


class TestIntentionPromptV2:
    """Test that the v2 intention template renders with world_context."""

    def test_renders_with_world_context(self) -> None:
        compressed = _make_compressed_prompt()
        result = render_intention_prompt(
            "intention.user.v2",
            world_context=compressed.to_prompt_text(),
            local_time="14:30",
            local_timezone="America/Toronto",
            reasoning_summary="(none)",
            recent_intents_summary="(none)",
            patterns_summary="(none)",
            predictions_summary="(none)",
            conversations_summary="(none)",
            schema_json="{}",
            max_questions=5,
        )

        assert "## World State Summary" in result
        assert "2 entities, 1 changed" in result
        assert "## Changes Since Last Cycle" in result
        # Should NOT contain raw JSON snapshot
        assert "snapshot_json" not in result

    def test_v1_template_still_works(self) -> None:
        result = render_intention_prompt(
            "intention.user.v1",
            snapshot_json='[{"test": true}]',
            local_time="14:30",
            local_timezone="America/Toronto",
            reasoning_summary="(none)",
            recent_intents_summary="(none)",
            patterns_summary="(none)",
            predictions_summary="(none)",
            conversations_summary="(none)",
            schema_json="{}",
            max_questions=5,
        )

        assert '{"test": true}' in result


class TestReasoningPromptV3:
    """Test that the v3 reasoning template renders with world_context."""

    def test_renders_with_world_context(self) -> None:
        compressed = _make_compressed_prompt()
        result = render_reasoning_prompt(
            "reasoning.heavy.user.v3",
            world_context=compressed.to_prompt_text(),
            memory_excerpt="(no relevant memories)",
            narrative_context="User is at home.",
            schema_json="{}",
            about_user="A developer.",
            previous_questions="",
            user_name="Guillaume",
            language_name="French",
        )

        assert "## World State Summary" in result
        assert "## World State (compressed" in result
        assert "2 entities, 1 changed" in result
        # Should NOT contain raw JSON snapshot
        assert "snapshot_json" not in result

    def test_v2_template_still_works(self) -> None:
        result = render_reasoning_prompt(
            "reasoning.heavy.user.v2",
            snapshot_json='[{"test": true}]',
            memory_excerpt="",
            narrative_context="",
            schema_json="{}",
            about_user="",
            previous_questions="",
            user_name="Guillaume",
            language_name="French",
        )

        assert '{"test": true}' in result


# ---------------------------------------------------------------------------
# Tests: IntentionLoop with pipeline
# ---------------------------------------------------------------------------


class TestIntentionLoopPipeline:
    """Test that IntentionLoop uses the pipeline when available."""

    @pytest.mark.asyncio
    async def test_uses_compressed_prompt_when_pipeline_present(self) -> None:
        pipeline = _make_mock_pipeline()
        llm = _make_mock_llm()

        loop = IntentionLoop(
            snapshot_provider=_make_mock_snapshot_provider(),
            reasoning_feed=_make_mock_reasoning_feed(),
            intent_store=_make_mock_intent_store(),
            llm=llm,
            router=_make_mock_router(),
            pipeline=pipeline,
            config=IntentionLoopConfig(
                interval_seconds=60,
                startup_grace_seconds=0,
            ),
        )

        # Run cycle directly
        with patch.object(loop, "_daemon_started_at", _NOW - timedelta(hours=1)):
            await loop.run_cycle()

        # Pipeline was called with the snapshot
        pipeline.process.assert_awaited_once()

        # LLM was called — check the user prompt contains compressed content
        call_args = llm.complete_structured.call_args
        user_prompt = call_args.kwargs.get("user") or call_args[1].get("user", "")
        assert "## World State Summary" in user_prompt
        assert "2 entities, 1 changed" in user_prompt

    @pytest.mark.asyncio
    async def test_falls_back_to_v1_without_pipeline(self) -> None:
        llm = _make_mock_llm()

        loop = IntentionLoop(
            snapshot_provider=_make_mock_snapshot_provider(),
            reasoning_feed=_make_mock_reasoning_feed(),
            intent_store=_make_mock_intent_store(),
            llm=llm,
            router=_make_mock_router(),
            pipeline=None,
            config=IntentionLoopConfig(
                interval_seconds=60,
                startup_grace_seconds=0,
            ),
        )

        with patch.object(loop, "_daemon_started_at", _NOW - timedelta(hours=1)):
            await loop.run_cycle()

        # LLM was called — check the user prompt contains JSON snapshot
        call_args = llm.complete_structured.call_args
        user_prompt = call_args.kwargs.get("user") or call_args[1].get("user", "")
        assert "World snapshot (JSON)" in user_prompt or "json" in user_prompt.lower()


# ---------------------------------------------------------------------------
# Tests: ReasoningLoop with pipeline
# ---------------------------------------------------------------------------


class TestReasoningLoopPipeline:
    """Test that ReasoningLoop uses the pipeline when available."""

    @pytest.mark.asyncio
    async def test_uses_compressed_prompt_when_pipeline_present(self) -> None:
        pipeline = _make_mock_pipeline()
        llm = MagicMock()
        llm.complete_structured = AsyncMock(
            return_value=ReasoningOutput(
                cycle_id="test",
                timestamp=_NOW,
                model_used="test-model",
                patterns=[],
                anomalies=[],
                token_usage=TokenUsage(
                    prompt_tokens=100,
                    completion_tokens=50,
                    total_tokens=150,
                ),
            )
        )
        llm.config = MagicMock()
        llm.config.reasoning_heavy = MagicMock(model="test-model")

        persister = MagicMock()
        persister.persist_cycle = AsyncMock()

        loop = ReasoningLoop(
            snapshot_provider=_make_mock_snapshot_provider(),
            memory=None,
            llm=llm,
            persister=persister,
            pipeline=pipeline,
        )

        await loop.run_cycle()

        # Pipeline was called
        pipeline.process.assert_awaited_once()

        # LLM received compressed content
        call_args = llm.complete_structured.call_args
        user_prompt = call_args.kwargs.get("user") or call_args[1].get("user", "")
        assert "## World State Summary" in user_prompt

    @pytest.mark.asyncio
    async def test_falls_back_to_v2_without_pipeline(self) -> None:
        llm = MagicMock()
        llm.complete_structured = AsyncMock(
            return_value=ReasoningOutput(
                cycle_id="test",
                timestamp=_NOW,
                model_used="test-model",
                patterns=[],
                anomalies=[],
                token_usage=TokenUsage(
                    prompt_tokens=100,
                    completion_tokens=50,
                    total_tokens=150,
                ),
            )
        )
        llm.config = MagicMock()
        llm.config.reasoning_heavy = MagicMock(model="test-model")

        persister = MagicMock()
        persister.persist_cycle = AsyncMock()

        loop = ReasoningLoop(
            snapshot_provider=_make_mock_snapshot_provider(),
            memory=None,
            llm=llm,
            persister=persister,
            pipeline=None,
        )

        await loop.run_cycle()

        # LLM received full JSON snapshot
        call_args = llm.complete_structured.call_args
        user_prompt = call_args.kwargs.get("user") or call_args[1].get("user", "")
        assert "World Snapshot" in user_prompt or "json" in user_prompt.lower()


# ---- Configuration ----


class TestEmbeddingPipelineConfig:
    """Test that EmbeddingPipelineConfig loads correctly."""

    def test_default_disabled(self) -> None:
        config = DaemonConfig()
        assert config.embedding_pipeline.enabled is False
        assert config.embedding_pipeline.cache_size == 5000
        assert config.embedding_pipeline.qdrant_url == "http://localhost:6333"
        assert config.embedding_pipeline.collection_name == "snapshot_embeddings"
        assert config.embedding_pipeline.top_k_similar == 3

    def test_loads_from_dict(self) -> None:
        config = DaemonConfig.model_validate(
            {
                "embedding_pipeline": {
                    "enabled": True,
                    "cache_size": 2000,
                    "qdrant_url": "http://qdrant:6333",
                    "top_k_similar": 5,
                    "prune_keep_count": 500,
                }
            }
        )
        assert config.embedding_pipeline.enabled is True
        assert config.embedding_pipeline.cache_size == 2000
        assert config.embedding_pipeline.qdrant_url == "http://qdrant:6333"
        assert config.embedding_pipeline.top_k_similar == 5
        assert config.embedding_pipeline.prune_keep_count == 500

    def test_frozen(self) -> None:
        config = EmbeddingPipelineConfig()
        with pytest.raises(Exception):  # noqa: B017
            config.enabled = True  # type: ignore[misc]
