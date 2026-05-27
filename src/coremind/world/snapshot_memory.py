"""Qdrant-backed snapshot embedding store for the World Model (L2).

Stores embedding vectors of :class:`~coremind.world.model.WorldSnapshot`
instances and supports similarity search to find past states resembling
the current one.  Enables "this pattern looks like last Tuesday at 6pm"
reasoning by retrieving the top-K most similar historical snapshots.

All public methods are coroutines.  The underlying Qdrant client uses
blocking HTTP calls; ``asyncio.to_thread`` keeps the event loop responsive.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.http.models import (
    DatetimeRange,
    Direction,
    Distance,
    FieldCondition,
    Filter,
    OrderBy,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from coremind.errors import SemanticMemoryError

log = structlog.get_logger(__name__)

_DEFAULT_COLLECTION = "snapshot_embeddings"
_DEFAULT_VECTOR_SIZE = 768


class SimilarSnapshot(BaseModel):
    """A past snapshot returned by similarity search."""

    snapshot_id: str
    score: float = Field(ge=0.0, le=1.0)
    summary: str
    entity_count: int
    timestamp: datetime


class SnapshotMemory:
    """Stores and retrieves snapshot embeddings via Qdrant.

    Args:
        qdrant_url: Qdrant HTTP URL (e.g. ``"http://localhost:6333"``).
        collection: Name of the Qdrant collection to use.
        vector_size: Dimensionality of stored vectors (must match encoder).
        timeout_seconds: Per-request timeout for Qdrant HTTP calls.
    """

    def __init__(
        self,
        qdrant_url: str = "http://localhost:6333",
        *,
        collection: str = _DEFAULT_COLLECTION,
        vector_size: int = _DEFAULT_VECTOR_SIZE,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._url = qdrant_url
        self._collection = collection
        self._vector_size = vector_size
        self._timeout = timeout_seconds
        self._client: QdrantClient | None = None

    @property
    def _db(self) -> QdrantClient:
        """Lazy-initialise the Qdrant client."""
        if self._client is None:
            self._client = QdrantClient(url=self._url, timeout=int(self._timeout))
        return self._client

    async def ensure_collection(self) -> None:
        """Create the collection and payload index if they do not exist.

        Idempotent — safe to call at every daemon startup.

        Raises:
            SemanticMemoryError: If Qdrant is unreachable or the call fails.
        """

        def _ensure() -> None:
            collections = self._db.get_collections()
            existing = {c.name for c in collections.collections}
            if self._collection in existing:
                log.debug(
                    "snapshot_memory.collection_exists",
                    collection=self._collection,
                )
                return
            self._db.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
            )
            self._db.create_payload_index(
                collection_name=self._collection,
                field_name="timestamp",
                field_schema=PayloadSchemaType.DATETIME,
            )
            log.info(
                "snapshot_memory.collection_created",
                collection=self._collection,
                vector_size=self._vector_size,
            )

        try:
            await asyncio.to_thread(_ensure)
        except UnexpectedResponse as exc:
            raise SemanticMemoryError(f"Snapshot memory collection creation failed: {exc}") from exc

    async def store(
        self,
        snapshot_id: str,
        vector: list[float],
        summary: str,
        entity_count: int,
        timestamp: datetime,
    ) -> None:
        """Store a snapshot embedding. Upserts by snapshot_id.

        Args:
            snapshot_id: Unique identifier for the snapshot.
            vector: The embedding vector (must match configured dimension).
            summary: Human-readable snapshot summary.
            entity_count: Number of entities in the snapshot.
            timestamp: When the snapshot was taken (must be tz-aware).

        Raises:
            SemanticMemoryError: If the Qdrant upsert fails.
        """

        def _store() -> None:
            point = PointStruct(
                id=snapshot_id,
                vector=vector,
                payload={
                    "summary": summary,
                    "entity_count": entity_count,
                    "timestamp": timestamp.isoformat(),
                },
            )
            self._db.upsert(collection_name=self._collection, points=[point])

        try:
            await asyncio.to_thread(_store)
        except UnexpectedResponse as exc:
            raise SemanticMemoryError(
                f"Snapshot memory upsert failed for {snapshot_id!r}: {exc}"
            ) from exc

    async def find_similar(
        self,
        vector: list[float],
        k: int = 5,
        exclude_recent_seconds: float = 600.0,
    ) -> list[SimilarSnapshot]:
        """Find top-K most similar past snapshots.

        Excludes snapshots newer than *exclude_recent_seconds* to avoid
        matching against the current or near-current state.

        Args:
            vector: Query embedding vector.
            k: Maximum number of results.
            exclude_recent_seconds: Ignore snapshots newer than this.

        Returns:
            Similar snapshots ordered by descending similarity score.

        Raises:
            SemanticMemoryError: If the Qdrant search fails.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=exclude_recent_seconds)

        def _search() -> list[SimilarSnapshot]:
            results = self._db.search(  # type: ignore[attr-defined]
                collection_name=self._collection,
                query_vector=vector,
                limit=k,
                query_filter=Filter(
                    must=[
                        FieldCondition(
                            key="timestamp",
                            range=DatetimeRange(lt=cutoff),
                        )
                    ]
                ),
            )
            return [
                SimilarSnapshot(
                    snapshot_id=str(r.id),
                    score=float(r.score),
                    summary=r.payload["summary"],
                    entity_count=r.payload["entity_count"],
                    timestamp=datetime.fromisoformat(r.payload["timestamp"]),
                )
                for r in results
            ]

        try:
            return await asyncio.to_thread(_search)
        except UnexpectedResponse as exc:
            raise SemanticMemoryError(f"Snapshot memory search failed: {exc}") from exc

    async def count(self) -> int:
        """Return total number of stored snapshot embeddings.

        Raises:
            SemanticMemoryError: If the Qdrant call fails.
        """

        def _count() -> int:
            result = self._db.count(collection_name=self._collection)
            return int(result.count)

        try:
            return await asyncio.to_thread(_count)
        except UnexpectedResponse as exc:
            raise SemanticMemoryError(f"Snapshot memory count failed: {exc}") from exc

    async def prune(self, keep_count: int = 1000) -> int:
        """Delete oldest embeddings when the collection exceeds *keep_count*.

        Strategy: scroll points ordered by timestamp descending, find the
        timestamp of the Nth newest point, and delete everything older.

        Args:
            keep_count: Maximum number of embeddings to retain.

        Returns:
            Number of points pruned.

        Raises:
            SemanticMemoryError: If the Qdrant call fails.
        """
        total = await self.count()
        if total <= keep_count:
            return 0

        def _prune() -> int:
            # Scroll the newest `keep_count` points by timestamp descending
            points, _ = self._db.scroll(
                collection_name=self._collection,
                limit=keep_count,
                order_by=OrderBy(key="timestamp", direction=Direction.DESC),
            )

            if not points:
                return 0

            # The oldest point in the kept set defines the cutoff
            payload = points[-1].payload or {}
            oldest_kept_ts = payload["timestamp"]

            # Delete all points older than the cutoff
            self._db.delete(
                collection_name=self._collection,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="timestamp",
                            range=DatetimeRange(
                                lt=datetime.fromisoformat(oldest_kept_ts),
                            ),
                        )
                    ]
                ),
            )

            # Compute how many were removed
            new_count = self._db.count(
                collection_name=self._collection,
            ).count
            return total - new_count

        try:
            return await asyncio.to_thread(_prune)
        except UnexpectedResponse as exc:
            raise SemanticMemoryError(f"Snapshot memory prune failed: {exc}") from exc
