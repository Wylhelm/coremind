"""Concrete :class:`VectorStorePort` implementation backed by Qdrant.

Wraps the ``qdrant-client`` library to provide the vector store port
consumed by :class:`coremind.memory.semantic.SemanticMemory`.

All public methods are coroutines.  The underlying Qdrant client uses
blocking HTTP calls under the hood; ``asyncio.to_thread`` is used to
keep the event loop responsive.

.. note::
   Qdrant must be running on the configured URL before instantiating
   this store.  No auto-start mechanism exists — the daemon or the
   operator must ensure the service is available.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.http.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    PointStruct,
    VectorParams,
)

from coremind.errors import SemanticMemoryError
from coremind.memory.semantic import SearchHit

log = structlog.get_logger(__name__)

# Dimensionality default — matches the default in ``SemanticMemory``.
_DEFAULT_VECTOR_SIZE: int = 384


class QdrantVectorStore:
    """Qdrant-backed :class:`VectorStorePort` implementation.

    Args:
        url: Qdrant HTTP URL (e.g. ``"http://localhost:6333"``).
            gRPC is not required — the HTTP API is sufficient.
        vector_size: Default vector dimensionality for ``ensure_collection``
            when no explicit size is provided.  Defaults to 384
            (multilingual-e5-small).
        timeout_seconds: Per-request timeout for Qdrant HTTP calls.
    """

    def __init__(
        self,
        url: str = "http://localhost:6333",
        *,
        vector_size: int = _DEFAULT_VECTOR_SIZE,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._url = url
        self._default_size = vector_size
        self._timeout = timeout_seconds
        # QdrantClient is thread-safe; we create it lazily on first use.
        self._client: QdrantClient | None = None

    @property
    def _db(self) -> QdrantClient:
        """Lazy-initialise the Qdrant client."""
        if self._client is None:
            self._client = QdrantClient(url=self._url, timeout=int(self._timeout))
        return self._client

    # ------------------------------------------------------------------
    # VectorStorePort implementation
    # ------------------------------------------------------------------

    async def ensure_collection(self, name: str, vector_size: int) -> None:
        """Create the collection if it does not already exist.

        Idempotent — safe to call at every daemon startup.

        Args:
            name: Collection name.
            vector_size: Dimensionality of the stored vectors.

        Raises:
            SemanticMemoryError: If Qdrant is unreachable or the API call
                fails.
        """
        size = vector_size or self._default_size

        def _ensure() -> None:
            collections = self._db.get_collections()
            existing = {c.name for c in collections.collections}
            if name in existing:
                log.debug("qdrant.collection_exists", name=name, vector_size=size)
                return
            self._db.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=size, distance=Distance.COSINE),
            )
            log.info("qdrant.collection_created", name=name, vector_size=size)

        try:
            await asyncio.to_thread(_ensure)
        except UnexpectedResponse as exc:
            raise SemanticMemoryError(
                f"Qdrant collection creation failed for {name!r}: {exc}"
            ) from exc

    async def upsert(
        self,
        collection: str,
        point_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        """Insert or update a single point in the collection.

        Args:
            collection: Target collection name.
            point_id: Unique identifier for the point.
            vector: The embedding vector.
            payload: Arbitrary data stored alongside the vector.

        Raises:
            SemanticMemoryError: If the Qdrant call fails.
        """

        def _upsert() -> None:
            point = PointStruct(id=point_id, vector=vector, payload=payload)
            self._db.upsert(collection_name=collection, points=[point])

        try:
            await asyncio.to_thread(_upsert)
        except UnexpectedResponse as exc:
            raise SemanticMemoryError(
                f"Qdrant upsert failed in collection {collection!r} for point {point_id!r}: {exc}"
            ) from exc

    async def search(
        self,
        collection: str,
        vector: list[float],
        k: int,
        tag_filter: list[str] | None = None,
    ) -> list[SearchHit]:
        """Return the top-k nearest neighbours, optionally filtered by tags.

        Args:
            collection: Collection to search.
            vector: The query embedding.
            k: Maximum number of results.
            tag_filter: If given, only return hits whose ``tags`` payload
                field contains *all* listed values.

        Returns:
            Hits ordered by descending similarity score.

        Raises:
            SemanticMemoryError: If the Qdrant call fails.
        """
        query_filter: Filter | None = None
        if tag_filter:
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="tags",
                        match=MatchAny(any=list(tag_filter)),
                    )
                ]
            )

        def _search() -> list[SearchHit]:
            results = self._db.search(  # type: ignore[attr-defined]
                collection_name=collection,
                query_vector=vector,
                limit=k,
                query_filter=query_filter,
            )
            return [
                SearchHit(
                    id=str(r.id),
                    score=float(r.score),
                    payload=dict(r.payload or {}),
                )
                for r in results
            ]

        try:
            return await asyncio.to_thread(_search)
        except UnexpectedResponse as exc:
            raise SemanticMemoryError(
                f"Qdrant search failed in collection {collection!r}: {exc}"
            ) from exc

    async def delete(self, collection: str, point_id: str) -> bool:
        """Remove a point from the collection.

        Args:
            collection: Collection containing the point.
            point_id: Identifier of the point to remove.

        Returns:
            ``True`` if the point existed and was deleted; ``False`` if it
            was not found (idempotent).

        Raises:
            SemanticMemoryError: If the Qdrant call fails.
        """

        def _delete() -> bool:
            result = self._db.delete(
                collection_name=collection,
                points_selector=[point_id],
                wait=True,
            )
            # The Qdrant client returns an UpdateResult; we check the
            # status field — "completed" means the point existed.
            if hasattr(result, "status"):
                return str(result.status) == "completed"
            return True  # best-effort: assume deletion succeeded

        try:
            return await asyncio.to_thread(_delete)
        except UnexpectedResponse as exc:
            raise SemanticMemoryError(
                f"Qdrant delete failed in collection {collection!r} for point {point_id!r}: {exc}"
            ) from exc
