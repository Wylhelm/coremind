"""Semantic memory layer backed by Qdrant vector database (L3, semantic layer).

Stores, retrieves, and manages forgetting of text-based memories across three
namespaced collections: facts, preferences, and documents.  All writes are
identified by an opaque ``memory_id`` that encodes the target collection so
``forget`` can route without a reverse-lookup table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Protocol, cast

import structlog
from pydantic import BaseModel, Field

from coremind.errors import SemanticMemoryError
from coremind.world.model import JsonValue

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Collection name constants
# ---------------------------------------------------------------------------

COLLECTION_FACTS = "semantic_facts"
COLLECTION_PREFERENCES = "semantic_preferences"
COLLECTION_DOCUMENTS = "semantic_documents"

_ALL_COLLECTIONS: tuple[str, ...] = (
    COLLECTION_FACTS,
    COLLECTION_PREFERENCES,
    COLLECTION_DOCUMENTS,
)

_ID_SEPARATOR = "::"

# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


class Memory(BaseModel):
    """A semantic memory retrieved from the vector store.

    Produced by :meth:`SemanticMemory.recall` and carries the similarity
    score alongside the original text, tags, and caller-supplied metadata.
    """

    id: str
    text: str
    tags: list[str]
    metadata: dict[str, JsonValue]
    score: float = Field(ge=0.0, le=1.0)
    collection: str
    created_at: datetime


class SearchHit(BaseModel):
    """Raw result returned by the :class:`VectorStorePort`.

    The ``payload`` dict contains all fields stored alongside the vector at
    upsert time; ``SemanticMemory`` reconstructs :class:`Memory` from it.
    """

    id: str
    score: float
    payload: dict[str, JsonValue]


# ---------------------------------------------------------------------------
# Port protocols (injectable dependencies)
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    """Port for producing vector embeddings from text.

    Satisfied by the sentence-transformers embedder introduced in Task 2.4.
    Use a stub in unit tests.
    """

    async def embed(self, text: str) -> list[float]:
        """Produce a fixed-length embedding vector for text.

        Args:
            text: The natural-language string to embed.

        Returns:
            A dense float vector of the model's output dimensionality.
        """
        ...


class VectorStorePort(Protocol):
    """Port for a Qdrant-compatible vector store.

    Decouples ``SemanticMemory`` from the concrete ``qdrant-client`` so that
    unit tests can use an in-process fake without spinning up a container.
    """

    async def ensure_collection(self, name: str, vector_size: int) -> None:
        """Create the collection if it does not already exist.

        Args:
            name: Collection name.
            vector_size: Dimensionality of the stored vectors.
        """
        ...

    async def upsert(
        self,
        collection: str,
        point_id: str,
        vector: list[float],
        payload: dict[str, JsonValue],
    ) -> None:
        """Insert or update a single point in the collection.

        Args:
            collection: Target collection name.
            point_id: Unique identifier for the point (our ``memory_id``).
            vector: The embedding vector.
            payload: Arbitrary data stored alongside the vector.
        """
        ...

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
        """
        ...

    async def delete(self, collection: str, point_id: str) -> bool:
        """Remove a point from the collection.

        Args:
            collection: Collection containing the point.
            point_id: Identifier of the point to remove.

        Returns:
            ``True`` if the point existed and was deleted; ``False`` if it
            was not found (idempotent).
        """
        ...


class AuditLogger(Protocol):
    """Port for writing signed audit log entries.

    Satisfied by the action-layer journal introduced in Phase 3.  Pass
    ``None`` in contexts where auditing is not yet wired up (e.g. tests).
    """

    async def log(self, action: str, payload: dict[str, object]) -> None:
        """Write an audit entry.

        Args:
            action: A dot-separated identifier for the action
                (e.g. ``"semantic_memory.forget"``).
            payload: Structured data describing the action.
        """
        ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_memory_id(collection: str) -> str:
    """Generate a new unique memory identifier that encodes the collection.

    Args:
        collection: The target collection name.

    Returns:
        A string of the form ``"{collection}::{uuid4}"``.
    """
    return f"{collection}{_ID_SEPARATOR}{uuid.uuid4()}"


def _parse_collection(memory_id: str) -> str:
    """Extract and validate the collection name from a memory_id.

    Args:
        memory_id: An identifier previously returned by ``remember``.

    Returns:
        The collection name encoded in the id.

    Raises:
        SemanticMemoryError: If the format is invalid or the collection is
            not one of the known collections.
    """
    if _ID_SEPARATOR not in memory_id:
        raise SemanticMemoryError(
            f"Invalid memory_id format (missing '{_ID_SEPARATOR}' separator): {memory_id!r}"
        )
    collection, _ = memory_id.split(_ID_SEPARATOR, 1)
    if collection not in _ALL_COLLECTIONS:
        raise SemanticMemoryError(
            f"Unknown collection encoded in memory_id: {collection!r}. "
            f"Valid collections: {_ALL_COLLECTIONS}"
        )
    return collection


_RESERVED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"memory_id", "text", "tags", "collection", "created_at"}
)


def _hit_to_memory(hit: SearchHit, collection: str) -> Memory:
    """Convert a raw :class:`SearchHit` into a :class:`Memory` domain model.

    Args:
        hit: The raw search result from the vector store.
        collection: The collection the hit originated from.

    Returns:
        A fully-populated ``Memory`` instance.
    """
    payload = hit.payload
    if "created_at" not in payload:
        raise SemanticMemoryError(
            f"Payload for hit {hit.id!r} is missing required field 'created_at'"
        )
    created_at = datetime.fromisoformat(str(payload["created_at"]))
    metadata = {k: v for k, v in payload.items() if k not in _RESERVED_PAYLOAD_KEYS}
    return Memory(
        id=str(payload.get("memory_id", hit.id)),
        text=str(payload.get("text", "")),
        tags=list(payload.get("tags") or []),  # type: ignore[arg-type]
        metadata=metadata,
        score=hit.score,
        collection=collection,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# SemanticMemory
# ---------------------------------------------------------------------------


class SemanticMemory:
    """Qdrant-backed semantic memory for the CoreMind cognitive daemon (L3).

    Memories are stored across three namespaced Qdrant collections and
    retrieved via vector similarity.  Forgetting is idempotent and produces
    a signed audit entry when an :class:`AuditLogger` is provided.

    All collections must be initialised before first use by calling
    :meth:`initialise`.
    """

    def __init__(
        self,
        store: VectorStorePort,
        embedder: Embedder,
        audit: AuditLogger | None = None,
        *,
        vector_size: int = 384,
    ) -> None:
        """Initialise SemanticMemory.

        Args:
            store: Port to the Qdrant (or compatible) vector database.
            embedder: Port to the text embedding model.
            audit: Optional audit logger for signed forgetting events.
                When ``None``, forget operations are still executed but not
                journalled.
            vector_size: Dimensionality of embedding vectors.  Must match the
                embedder's output size.  Defaults to 384 (multilingual-e5-small).
        """
        self._store = store
        self._embedder = embedder
        self._audit = audit
        self._vector_size = vector_size

    async def initialise(self) -> None:
        """Ensure all three collections exist in the vector store.

        Idempotent — safe to call at every daemon startup.  Must be awaited
        before any call to :meth:`remember`, :meth:`recall`, or
        :meth:`forget`.

        Raises:
            SemanticMemoryError: If the embedder probe fails or its output
                dimension does not match ``vector_size``.
        """
        for name in _ALL_COLLECTIONS:
            await self._store.ensure_collection(name, self._vector_size)
        try:
            probe = await self._embedder.embed("")
        except Exception as exc:
            raise SemanticMemoryError("Embedder probe failed during initialisation") from exc
        if len(probe) != self._vector_size:
            raise SemanticMemoryError(
                f"Embedder output dimension {len(probe)} does not match "
                f"configured vector_size={self._vector_size}"
            )
        log.info("semantic_memory.initialised", collections=list(_ALL_COLLECTIONS))

    async def remember(
        self,
        text: str,
        tags: list[str],
        metadata: dict[str, JsonValue],
        collection: str = COLLECTION_FACTS,
    ) -> str:
        """Embed and store a text memory.

        Args:
            text: The natural-language content to embed and store.
            tags: Free-form labels for filtering at recall time.
            metadata: Arbitrary key/value pairs persisted alongside the vector.
                Keys must not collide with the reserved set
                ``{memory_id, text, tags, collection, created_at}``.
            collection: Target collection.  Defaults to
                :data:`COLLECTION_FACTS`.

        Returns:
            An opaque memory identifier suitable for passing to
            :meth:`forget`.

        Raises:
            ValueError: If *collection* is not one of the three known
                collection names.
            SemanticMemoryError: If the embedding model or the vector store
                raises an error.
        """
        if collection not in _ALL_COLLECTIONS:
            raise ValueError(
                f"Unknown collection {collection!r}. Valid options: {_ALL_COLLECTIONS}"
            )

        collision = frozenset(metadata) & _RESERVED_PAYLOAD_KEYS
        if collision:
            raise SemanticMemoryError(
                f"Metadata keys collide with reserved payload fields: {sorted(collision)}"
            )

        memory_id = _make_memory_id(collection)
        now = datetime.now(UTC)

        try:
            vector = await self._embedder.embed(text)
        except Exception as exc:
            raise SemanticMemoryError(
                f"Embedding failed for new memory in collection {collection!r}"
            ) from exc

        payload: dict[str, JsonValue] = {
            "memory_id": memory_id,
            "text": text,
            "tags": cast(list[JsonValue], tags),
            "collection": collection,
            "created_at": now.isoformat(),
            **metadata,
        }

        try:
            await self._store.upsert(
                collection=collection,
                point_id=memory_id,
                vector=vector,
                payload=payload,
            )
        except Exception as exc:
            raise SemanticMemoryError(f"Storage failed for memory {memory_id!r}") from exc

        log.info(
            "semantic_memory.remembered",
            memory_id=memory_id,
            collection=collection,
            tags=tags,
        )
        return memory_id

    async def recall(
        self,
        query: str,
        k: int = 10,
        tags: list[str] | None = None,
        collection: str | None = None,
    ) -> list[Memory]:
        """Return the top-k memories most similar to *query*.

        When *collection* is ``None`` all three collections are searched and
        results are merged and re-ranked by score before the top-k are
        returned.

        Args:
            query: Natural-language query string to embed and search with.
            k: Maximum number of memories to return.
            tags: If given, restrict results to memories that carry all
                listed tags.
            collection: If given, restrict search to this collection only.

        Returns:
            Memories ordered by descending similarity score, at most *k*
            items.

        Raises:
            ValueError: If *collection* is not one of the three known names.
            SemanticMemoryError: If embedding or any collection search fails.
        """
        if collection is not None and collection not in _ALL_COLLECTIONS:
            raise ValueError(
                f"Unknown collection {collection!r}. Valid options: {_ALL_COLLECTIONS}"
            )

        try:
            vector = await self._embedder.embed(query)
        except Exception as exc:
            raise SemanticMemoryError("Embedding failed for recall query") from exc

        collections_to_search = (collection,) if collection is not None else _ALL_COLLECTIONS

        results: list[Memory] = []
        for col in collections_to_search:
            try:
                hits = await self._store.search(
                    collection=col,
                    vector=vector,
                    k=k,
                    tag_filter=tags,
                )
            except Exception as exc:
                raise SemanticMemoryError(f"Search failed in collection {col!r}") from exc

            results.extend(_hit_to_memory(hit, col) for hit in hits)

        # Re-rank when searching multiple collections so callers get a globally
        # ordered list rather than per-collection order.
        results.sort(key=lambda m: m.score, reverse=True)
        return results[:k]

    async def forget(self, memory_id: str, reason: str) -> None:
        """Remove a memory and write a signed audit entry.

        The collection is decoded from *memory_id* so no reverse-lookup is
        required.  Idempotent — if the memory no longer exists the call is a
        no-op.  Raises only when *memory_id* is syntactically invalid or the
        deletion fails with a storage error.

        The audit entry is written before the deletion so the intent is
        journalled regardless of whether the deletion succeeds.

        Args:
            memory_id: The identifier returned by a previous call to
                :meth:`remember`.
            reason: Human-readable justification for removing this memory.
                Persisted in the audit log.

        Raises:
            SemanticMemoryError: If *memory_id* is malformed or the deletion
                fails.
        """
        collection = _parse_collection(memory_id)

        # Journal-first: record the intent before performing the side-effect so
        # the audit trail is never lost even if deletion raises.
        if self._audit is not None:
            await self._audit.log(
                "semantic_memory.forget",
                {
                    "memory_id": memory_id,
                    "collection": collection,
                    "reason": reason,
                    "forgotten_at": datetime.now(UTC).isoformat(),
                },
            )

        try:
            deleted = await self._store.delete(
                collection=collection,
                point_id=memory_id,
            )
        except Exception as exc:
            raise SemanticMemoryError(f"Deletion failed for memory {memory_id!r}") from exc

        if deleted:
            log.info(
                "semantic_memory.forgotten",
                memory_id=memory_id,
                collection=collection,
                reason=reason,
            )
        else:
            log.info(
                "semantic_memory.forget_noop",
                memory_id=memory_id,
                collection=collection,
                reason=reason,
            )
