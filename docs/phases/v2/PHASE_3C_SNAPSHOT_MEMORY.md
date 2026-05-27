# Phase 3C — Snapshot Memory (Qdrant)

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_3_EMBEDDING_WORLD.md](PHASE_3_EMBEDDING_WORLD.md)
**Prerequisites:** Phase 3B (uses `EmbeddingEncoder.VECTOR_DIM`)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Create `SnapshotMemory` — a Qdrant-backed store for snapshot embeddings with similarity search. Enables "this state looks like last Tuesday at 6pm" reasoning by retrieving the top-K most similar past world states.

---

## 2. Deliverables

| File | Purpose |
| --- | --- |
| `src/coremind/world/snapshot_memory.py` | `SnapshotMemory`, `SimilarSnapshot` |
| `tests/world/test_snapshot_memory.py` | Unit tests (mocked Qdrant client) |

---

## 3. Data Model

```python
class SimilarSnapshot(BaseModel):
    """A past snapshot returned by similarity search."""
    snapshot_id: str
    score: float              # cosine similarity 0..1
    summary: str
    entity_count: int
    timestamp: datetime
```

---

## 4. Implementation

```python
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    PointStruct,
    Range,
    VectorParams,
)


COLLECTION_NAME = "snapshot_embeddings"
VECTOR_DIM = 768


class SnapshotMemory:
    """Stores and retrieves snapshot embeddings via Qdrant."""

    def __init__(
        self,
        qdrant_url: str = "http://localhost:6333",
        collection: str = COLLECTION_NAME,
    ):
        self._client = QdrantClient(url=qdrant_url)
        self._collection = collection

    async def ensure_collection(self) -> None:
        """Create collection if it does not exist. Idempotent."""
        collections = self._client.get_collections().collections
        exists = any(c.name == self._collection for c in collections)
        if not exists:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            )

    async def store(
        self,
        snapshot_id: str,
        vector: list[float],
        summary: str,
        entity_count: int,
        timestamp: datetime,
    ) -> None:
        """Store a snapshot embedding. Upserts by snapshot_id."""
        self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=snapshot_id,
                    vector=vector,
                    payload={
                        "summary": summary,
                        "entity_count": entity_count,
                        "timestamp": timestamp.isoformat(),
                    },
                )
            ],
        )

    async def find_similar(
        self,
        vector: list[float],
        k: int = 5,
        exclude_recent_seconds: float = 600.0,
    ) -> list[SimilarSnapshot]:
        """Find top-K most similar past snapshots.

        Excludes snapshots newer than exclude_recent_seconds to avoid
        matching against the current or near-current state.
        """
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=exclude_recent_seconds)
        ).isoformat()

        results = self._client.search(
            collection_name=self._collection,
            query_vector=vector,
            limit=k,
            query_filter=Filter(
                must=[FieldCondition(key="timestamp", range=Range(lt=cutoff))]
            ),
        )

        return [
            SimilarSnapshot(
                snapshot_id=str(r.id),
                score=r.score,
                summary=r.payload["summary"],
                entity_count=r.payload["entity_count"],
                timestamp=datetime.fromisoformat(r.payload["timestamp"]),
            )
            for r in results
        ]

    async def count(self) -> int:
        """Return total number of stored embeddings."""
        return self._client.count(collection_name=self._collection).count

    async def prune(self, keep_count: int = 1000) -> int:
        """Keep only the most recent N embeddings. Returns count pruned.

        Strategy: get total count, if > keep_count, find the Nth newest
        timestamp and delete everything older.
        """
        total = await self.count()
        if total <= keep_count:
            return 0

        # Scroll all points sorted by timestamp desc, get the cutoff
        # Use scroll with limit=keep_count, order by timestamp desc
        # Then delete all with timestamp < cutoff
        points, _ = self._client.scroll(
            collection_name=self._collection,
            limit=keep_count,
            order_by="timestamp",  # newest first via index
        )

        if not points:
            return 0

        oldest_kept = points[-1].payload["timestamp"]
        # Delete all older than oldest_kept
        self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[FieldCondition(key="timestamp", range=Range(lt=oldest_kept))]
            ),
        )

        new_total = await self.count()
        return total - new_total
```

---

## 5. Tests

```python
# tests/world/test_snapshot_memory.py

@pytest.mark.asyncio
async def test_ensure_collection_creates_if_missing(mock_qdrant):
    memory = SnapshotMemory(qdrant_url="http://test")
    memory._client = mock_qdrant
    mock_qdrant.get_collections.return_value = MockCollections(collections=[])

    await memory.ensure_collection()

    mock_qdrant.create_collection.assert_called_once()

@pytest.mark.asyncio
async def test_ensure_collection_noop_if_exists(mock_qdrant):
    memory = SnapshotMemory(qdrant_url="http://test")
    memory._client = mock_qdrant
    mock_qdrant.get_collections.return_value = MockCollections(
        collections=[MockCollection(name="snapshot_embeddings")]
    )

    await memory.ensure_collection()

    mock_qdrant.create_collection.assert_not_called()

@pytest.mark.asyncio
async def test_store_upserts_point(mock_qdrant):
    memory = SnapshotMemory(qdrant_url="http://test")
    memory._client = mock_qdrant

    await memory.store(
        snapshot_id="snap-1",
        vector=[0.1] * 768,
        summary="48 entities, 2 changed",
        entity_count=48,
        timestamp=datetime(2026, 5, 27, 10, 0, tzinfo=UTC),
    )

    mock_qdrant.upsert.assert_called_once()
    call_args = mock_qdrant.upsert.call_args
    point = call_args.kwargs["points"][0]
    assert point.id == "snap-1"
    assert len(point.vector) == 768
    assert point.payload["entity_count"] == 48

@pytest.mark.asyncio
async def test_find_similar_excludes_recent(mock_qdrant):
    memory = SnapshotMemory(qdrant_url="http://test")
    memory._client = mock_qdrant
    mock_qdrant.search.return_value = [
        MockSearchResult(id="snap-old", score=0.92, payload={
            "summary": "47 entities",
            "entity_count": 47,
            "timestamp": "2026-05-26T10:00:00+00:00",
        })
    ]

    results = await memory.find_similar([0.1] * 768, k=3)

    assert len(results) == 1
    assert results[0].snapshot_id == "snap-old"
    assert results[0].score == 0.92
    # Verify filter was applied
    call_filter = mock_qdrant.search.call_args.kwargs["query_filter"]
    assert call_filter is not None

@pytest.mark.asyncio
async def test_prune_noop_when_under_limit(mock_qdrant):
    memory = SnapshotMemory(qdrant_url="http://test")
    memory._client = mock_qdrant
    mock_qdrant.count.return_value = MockCount(count=500)

    pruned = await memory.prune(keep_count=1000)

    assert pruned == 0
    mock_qdrant.delete.assert_not_called()
```

---

## 6. Configuration

Extends `EmbeddingConfig` from Phase 3B:

```python
class EmbeddingConfig(BaseModel):
    # ... fields from 3B ...
    qdrant_url: str = "http://localhost:6333"
    collection_name: str = "snapshot_embeddings"
    prune_keep_count: int = 1000
    prune_interval_seconds: float = 21600.0  # 6h
    exclude_recent_seconds: float = 600.0    # 10min
    top_k_similar: int = 3
```

---

## 7. Dependencies

Add to `pyproject.toml`:

- `qdrant-client >= 1.9`

---

## 8. Qdrant Collection Setup

On daemon startup, call `await snapshot_memory.ensure_collection()`. This is idempotent.

Pruning runs on a background timer every `prune_interval_seconds`.

---

## 9. Success Criteria

- [ ] `ensure_collection()` creates the Qdrant collection idempotently
- [ ] `store()` upserts points with correct payload schema
- [ ] `find_similar()` returns ranked results excluding recent snapshots
- [ ] `prune()` deletes old embeddings when count exceeds limit
- [ ] `count()` returns total stored embeddings
- [ ] All tests pass with mocked Qdrant client (no real Qdrant needed)
- [ ] `mypy --strict` passes
- [ ] `ruff check` passes
