"""Unit tests for :mod:`coremind.world.snapshot_memory`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from coremind.world.snapshot_memory import (
    _DEFAULT_COLLECTION,
    SimilarSnapshot,
    SnapshotMemory,
)

# ---------------------------------------------------------------------------
# Lightweight mocks mirroring Qdrant response shapes
# ---------------------------------------------------------------------------


class _MockCollection:
    def __init__(self, name: str) -> None:
        self.name = name


class _MockCollections:
    def __init__(self, collections: list[_MockCollection]) -> None:
        self.collections = collections


class _MockCount:
    def __init__(self, count: int) -> None:
        self.count = count


class _MockSearchResult:
    def __init__(self, *, point_id: str, score: float, payload: dict[str, Any]) -> None:
        self.id = point_id
        self.score = score
        self.payload = payload


class _MockQueryResponse:
    """Mimics the Qdrant query_points response containing a .points list."""

    def __init__(self, points: list[_MockSearchResult]) -> None:
        self.points = points


class _MockScrollPoint:
    def __init__(self, *, point_id: str, payload: dict[str, Any]) -> None:
        self.id = point_id
        self.payload = payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(mock_client: MagicMock) -> SnapshotMemory:
    """Create a SnapshotMemory wired to a mock Qdrant client."""
    memory = SnapshotMemory(qdrant_url="http://test:6333")
    memory._client = mock_client
    return memory


def _vec(dim: int = 768) -> list[float]:
    """Return a dummy vector."""
    return [0.1] * dim


# ---------------------------------------------------------------------------
# ensure_collection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_collection_creates_if_missing() -> None:
    mock = MagicMock()
    mock.get_collections.return_value = _MockCollections(collections=[])
    memory = _make_memory(mock)

    await memory.ensure_collection()

    mock.create_collection.assert_called_once()
    call_kwargs = mock.create_collection.call_args.kwargs
    assert call_kwargs["collection_name"] == _DEFAULT_COLLECTION


@pytest.mark.asyncio
async def test_ensure_collection_creates_timestamp_index() -> None:
    mock = MagicMock()
    mock.get_collections.return_value = _MockCollections(collections=[])
    memory = _make_memory(mock)

    await memory.ensure_collection()

    mock.create_payload_index.assert_called_once()
    call_kwargs = mock.create_payload_index.call_args.kwargs
    assert call_kwargs["field_name"] == "timestamp"


@pytest.mark.asyncio
async def test_ensure_collection_noop_if_exists() -> None:
    mock = MagicMock()
    mock.get_collections.return_value = _MockCollections(
        collections=[_MockCollection(name=_DEFAULT_COLLECTION)]
    )
    memory = _make_memory(mock)

    await memory.ensure_collection()

    mock.create_collection.assert_not_called()
    mock.create_payload_index.assert_not_called()


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_upserts_point_with_correct_payload() -> None:
    mock = MagicMock()
    memory = _make_memory(mock)
    ts = datetime(2026, 5, 27, 10, 0, tzinfo=UTC)

    await memory.store(
        snapshot_id="snap-1",
        vector=_vec(),
        summary="48 entities, 2 changed",
        entity_count=48,
        timestamp=ts,
    )

    mock.upsert.assert_called_once()
    call_kwargs = mock.upsert.call_args.kwargs
    assert call_kwargs["collection_name"] == _DEFAULT_COLLECTION

    point = call_kwargs["points"][0]
    assert point.id == "snap-1"
    assert len(point.vector) == 768
    assert point.payload["entity_count"] == 48
    assert point.payload["summary"] == "48 entities, 2 changed"
    assert point.payload["timestamp"] == ts.isoformat()


# ---------------------------------------------------------------------------
# find_similar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_similar_excludes_recent_via_filter() -> None:
    mock = MagicMock()
    mock.query_points.return_value = _MockQueryResponse(
        points=[
            _MockSearchResult(
                point_id="snap-old",
                score=0.92,
                payload={
                    "summary": "47 entities",
                    "entity_count": 47,
                    "timestamp": "2026-05-26T10:00:00+00:00",
                },
            )
        ]
    )
    memory = _make_memory(mock)

    results = await memory.find_similar(_vec(), k=3)

    assert len(results) == 1
    assert results[0].snapshot_id == "snap-old"
    assert results[0].score == 0.92
    assert results[0].entity_count == 47

    # Verify a time-based filter was applied
    call_kwargs = mock.query_points.call_args.kwargs
    assert call_kwargs["query_filter"] is not None
    conditions = call_kwargs["query_filter"].must
    assert len(conditions) == 1
    assert conditions[0].key == "timestamp"
    assert conditions[0].range.lt is not None


@pytest.mark.asyncio
async def test_find_similar_returns_correct_model() -> None:
    mock = MagicMock()
    ts_str = "2026-05-25T08:30:00+00:00"
    mock.query_points.return_value = _MockQueryResponse(
        points=[
            _MockSearchResult(
                point_id="snap-a",
                score=0.95,
                payload={
                    "summary": "50 entities, 5 changed",
                    "entity_count": 50,
                    "timestamp": ts_str,
                },
            ),
            _MockSearchResult(
                point_id="snap-b",
                score=0.88,
                payload={
                    "summary": "48 entities, 1 changed",
                    "entity_count": 48,
                    "timestamp": "2026-05-24T14:00:00+00:00",
                },
            ),
        ]
    )
    memory = _make_memory(mock)

    results = await memory.find_similar(_vec(), k=5)

    assert len(results) == 2
    assert all(isinstance(r, SimilarSnapshot) for r in results)
    assert results[0].score > results[1].score
    assert results[0].timestamp == datetime.fromisoformat(ts_str)


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_returns_total() -> None:
    mock = MagicMock()
    mock.count.return_value = _MockCount(count=42)
    memory = _make_memory(mock)

    result = await memory.count()

    assert result == 42
    mock.count.assert_called_once_with(collection_name=_DEFAULT_COLLECTION)


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_noop_when_under_limit() -> None:
    mock = MagicMock()
    mock.count.return_value = _MockCount(count=500)
    memory = _make_memory(mock)

    pruned = await memory.prune(keep_count=1000)

    assert pruned == 0
    mock.scroll.assert_not_called()
    mock.delete.assert_not_called()


@pytest.mark.asyncio
async def test_prune_deletes_old_when_over_limit() -> None:
    mock = MagicMock()
    # First count() call returns over-limit; second (after deletion) returns trimmed
    mock.count.side_effect = [
        _MockCount(count=1200),
        _MockCount(count=1000),
    ]

    oldest_kept_ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    mock.scroll.return_value = (
        [
            _MockScrollPoint(
                point_id="snap-newest",
                payload={"timestamp": datetime.now(UTC).isoformat()},
            ),
            _MockScrollPoint(
                point_id="snap-oldest-kept",
                payload={"timestamp": oldest_kept_ts},
            ),
        ],
        None,  # next_page_offset
    )
    memory = _make_memory(mock)

    pruned = await memory.prune(keep_count=1000)

    assert pruned == 200
    mock.scroll.assert_called_once()
    mock.delete.assert_called_once()

    # Verify the deletion filter uses the cutoff timestamp
    delete_kwargs = mock.delete.call_args.kwargs
    filter_obj = delete_kwargs["points_selector"]
    assert filter_obj.must[0].key == "timestamp"
    assert filter_obj.must[0].range.lt is not None


@pytest.mark.asyncio
async def test_prune_returns_zero_when_scroll_empty() -> None:
    mock = MagicMock()
    mock.count.return_value = _MockCount(count=1500)
    mock.scroll.return_value = ([], None)
    memory = _make_memory(mock)

    pruned = await memory.prune(keep_count=1000)

    assert pruned == 0
    mock.delete.assert_not_called()
