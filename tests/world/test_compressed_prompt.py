"""Unit tests for src/coremind/world/compressed_prompt.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coremind.world.compressed_prompt import CompressedPrompt, CompressedPromptBuilder
from coremind.world.differ import SnapshotDiff
from coremind.world.model import Entity, JsonValue, WorldSnapshot
from coremind.world.snapshot_memory import SimilarSnapshot

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


def _make_snapshot(entities: list[Entity] | None = None) -> WorldSnapshot:
    return WorldSnapshot(taken_at=_NOW, entities=entities or [])


def _make_memory(similar: list[SimilarSnapshot] | None = None) -> MagicMock:
    """Build a mock SnapshotMemory that returns given similar snapshots."""
    mock = MagicMock()
    mock.find_similar = AsyncMock(return_value=similar or [])
    return mock


# ---------------------------------------------------------------------------
# CompressedPrompt tests
# ---------------------------------------------------------------------------


class TestCompressedPromptToText:
    def test_includes_all_sections_when_populated(self) -> None:
        prompt = CompressedPrompt(
            summary="48 entities, 3 changed",
            changes_text="~ light.bureau: state: off → on",
            similar_states_text="- 4h ago (similarity 0.94): 48 entities",
            key_metrics_text="Total entities: 48",
        )
        text = prompt.to_prompt_text()

        assert "## World State Summary" in text
        assert "## Changes Since Last Cycle" in text
        assert "## Similar Past States" in text
        assert "## Key Metrics" in text
        assert "light.bureau" in text

    def test_omits_empty_similar_states(self) -> None:
        prompt = CompressedPrompt(
            summary="48 entities, no changes",
            changes_text="No changes since last cycle.",
            similar_states_text="",
            key_metrics_text="Total entities: 48",
        )
        text = prompt.to_prompt_text()

        assert "## Similar Past States" not in text
        assert "## Key Metrics" in text

    def test_omits_empty_metrics(self) -> None:
        prompt = CompressedPrompt(
            summary="48 entities, no changes",
            changes_text="No changes since last cycle.",
            similar_states_text="- 2d ago (similarity 0.80): ok",
            key_metrics_text="",
        )
        text = prompt.to_prompt_text()

        assert "## Key Metrics" not in text
        assert "## Similar Past States" in text

    def test_omits_both_empty_sections(self) -> None:
        prompt = CompressedPrompt(
            summary="10 entities",
            changes_text="No changes since last cycle.",
            similar_states_text="",
            key_metrics_text="",
        )
        text = prompt.to_prompt_text()

        assert "## Similar Past States" not in text
        assert "## Key Metrics" not in text


class TestEstimatedTokens:
    def test_reasonable_range(self) -> None:
        prompt = CompressedPrompt(
            summary="x" * 100,
            changes_text="y" * 200,
            similar_states_text="z" * 100,
            key_metrics_text="w" * 100,
        )
        # ~500 chars of content + section headers ≈ 600+ chars → 150+ tokens
        assert prompt.estimated_tokens > 100
        assert prompt.estimated_tokens < 500

    def test_empty_prompt_minimal_tokens(self) -> None:
        prompt = CompressedPrompt(
            summary="ok",
            changes_text="none",
            similar_states_text="",
            key_metrics_text="",
        )
        assert prompt.estimated_tokens < 30


# ---------------------------------------------------------------------------
# CompressedPromptBuilder tests
# ---------------------------------------------------------------------------


class TestBuilderFormatChanges:
    @pytest.mark.asyncio
    async def test_formats_added_entities(self) -> None:
        memory = _make_memory()
        builder = CompressedPromptBuilder(memory, top_k=3)
        diff = SnapshotDiff(
            added=[_make_entity("bureau", state="on")],
            total_current=10,
        )
        snapshot = _make_snapshot([_make_entity("bureau", state="on")])

        prompt = await builder.build(snapshot, diff, [0.1] * _DIM)

        assert "+ bureau" in prompt.changes_text
        assert "(light)" in prompt.changes_text

    @pytest.mark.asyncio
    async def test_formats_removed_entities(self) -> None:
        memory = _make_memory()
        builder = CompressedPromptBuilder(memory, top_k=3)
        diff = SnapshotDiff(
            removed=[_make_entity("old_light")],
            total_current=10,
        )
        snapshot = _make_snapshot()

        prompt = await builder.build(snapshot, diff, [0.1] * _DIM)

        assert "- old_light (removed)" in prompt.changes_text

    @pytest.mark.asyncio
    async def test_formats_changed_entities(self) -> None:
        memory = _make_memory()
        builder = CompressedPromptBuilder(memory, top_k=3)
        old = _make_entity("bureau", state="off", brightness="50")
        new = _make_entity("bureau", state="on", brightness="50")
        diff = SnapshotDiff(
            changed=[(old, new)],
            total_current=10,
        )
        snapshot = _make_snapshot([new])

        prompt = await builder.build(snapshot, diff, [0.1] * _DIM)

        assert "~ bureau" in prompt.changes_text
        assert "state: off → on" in prompt.changes_text
        # Unchanged property should not appear
        assert "brightness" not in prompt.changes_text

    @pytest.mark.asyncio
    async def test_no_changes_message(self) -> None:
        memory = _make_memory()
        builder = CompressedPromptBuilder(memory, top_k=3)
        diff = SnapshotDiff(unchanged_count=5, total_current=5)
        snapshot = _make_snapshot()

        prompt = await builder.build(snapshot, diff, [0.1] * _DIM)

        assert "No changes since last cycle." in prompt.changes_text

    @pytest.mark.asyncio
    async def test_ignores_noisy_properties_in_diff(self) -> None:
        memory = _make_memory()
        builder = CompressedPromptBuilder(memory, top_k=3)
        old = _make_entity("bureau", state="on", last_changed="t1")
        new = _make_entity("bureau", state="on", last_changed="t2")
        diff = SnapshotDiff(
            changed=[(old, new)],
            total_current=5,
        )
        snapshot = _make_snapshot([new])

        prompt = await builder.build(snapshot, diff, [0.1] * _DIM)

        # last_changed is in IGNORED_PROPERTIES → shows "(no semantic change)"
        assert "last_changed" not in prompt.changes_text


class TestBuilderSimilarStates:
    @pytest.mark.asyncio
    async def test_formats_similar_with_age_and_score(self) -> None:
        similar = [
            SimilarSnapshot(
                snapshot_id="abc",
                score=0.94,
                summary="48 entities, 2 changed",
                entity_count=48,
                timestamp=_NOW - timedelta(hours=4),
            ),
        ]
        memory = _make_memory(similar)
        builder = CompressedPromptBuilder(memory, top_k=3)
        diff = SnapshotDiff(total_current=48)
        snapshot = _make_snapshot()

        with patch(
            "coremind.world.compressed_prompt.datetime",
        ) as mock_dt:
            mock_dt.now.return_value = _NOW
            prompt = await builder.build(snapshot, diff, [0.1] * _DIM)

        assert "4h ago" in prompt.similar_states_text
        assert "0.94" in prompt.similar_states_text
        assert "48 entities" in prompt.similar_states_text

    @pytest.mark.asyncio
    async def test_empty_when_no_similar(self) -> None:
        memory = _make_memory([])
        builder = CompressedPromptBuilder(memory, top_k=3)
        diff = SnapshotDiff(total_current=10)
        snapshot = _make_snapshot()

        prompt = await builder.build(snapshot, diff, [0.1] * _DIM)

        assert prompt.similar_states_text == ""


class TestBuilderMetrics:
    @pytest.mark.asyncio
    async def test_computes_type_counts(self) -> None:
        entities = [
            _make_entity("bureau", entity_type="light"),
            _make_entity("salon", entity_type="light"),
            _make_entity("temp", entity_type="sensor"),
        ]
        memory = _make_memory()
        builder = CompressedPromptBuilder(memory, top_k=3)
        diff = SnapshotDiff(total_current=3)
        snapshot = _make_snapshot(entities)

        prompt = await builder.build(snapshot, diff, [0.1] * _DIM)

        assert "Total entities: 3" in prompt.key_metrics_text
        assert "- light: 2" in prompt.key_metrics_text
        assert "- sensor: 1" in prompt.key_metrics_text
