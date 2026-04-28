"""Tests for coremind.memory.semantic.

All tests are unit tests (no I/O, no containers).  External dependencies
(VectorStorePort, Embedder, AuditLogger) are replaced with in-process fakes.
"""

from __future__ import annotations

from typing import cast

import pytest

from coremind.errors import SemanticMemoryError
from coremind.memory.semantic import (
    _ALL_COLLECTIONS,
    _ID_SEPARATOR,
    COLLECTION_DOCUMENTS,
    COLLECTION_FACTS,
    COLLECTION_PREFERENCES,
    SearchHit,
    SemanticMemory,
    _hit_to_memory,
    _parse_collection,
)
from coremind.world.model import JsonValue

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_FIXED_VECTOR: list[float] = [0.1, 0.2, 0.3]


class _FakeEmbedder:
    """Stub embedder that returns a fixed vector and records calls."""

    def __init__(self, *, fail: bool = False, vector_size: int = 3) -> None:
        self.calls: list[str] = []
        self._fail = fail
        self._vector_size = vector_size

    async def embed(self, text: str) -> list[float]:
        if self._fail:
            raise RuntimeError("embed error")
        self.calls.append(text)
        return [0.1] * self._vector_size


class _StoredPoint:
    def __init__(
        self,
        point_id: str,
        vector: list[float],
        payload: dict[str, JsonValue],
    ) -> None:
        self.point_id = point_id
        self.vector = vector
        self.payload = payload


class _FakeVectorStore:
    """In-process fake satisfying VectorStorePort."""

    def __init__(
        self,
        *,
        fail_upsert: bool = False,
        fail_search: bool = False,
        fail_delete: bool = False,
        search_score: float = 0.9,
    ) -> None:
        self.collections: dict[str, list[_StoredPoint]] = {}
        self.ensure_calls: list[tuple[str, int]] = []
        self._fail_upsert = fail_upsert
        self._fail_search = fail_search
        self._fail_delete = fail_delete
        self._search_score = search_score

    async def ensure_collection(self, name: str, vector_size: int) -> None:
        self.ensure_calls.append((name, vector_size))
        self.collections.setdefault(name, [])

    async def upsert(
        self,
        collection: str,
        point_id: str,
        vector: list[float],
        payload: dict[str, JsonValue],
    ) -> None:
        if self._fail_upsert:
            raise RuntimeError("upsert error")
        self.collections.setdefault(collection, []).append(_StoredPoint(point_id, vector, payload))

    async def search(
        self,
        collection: str,
        vector: list[float],
        k: int,
        tag_filter: list[str] | None = None,
    ) -> list[SearchHit]:
        if self._fail_search:
            raise RuntimeError("search error")
        points = self.collections.get(collection, [])
        hits = []
        for p in points:
            stored_tags: list[str] = cast(list[str], p.payload.get("tags") or [])
            if tag_filter and not all(t in stored_tags for t in tag_filter):
                continue
            hits.append(
                SearchHit(
                    id=p.point_id,
                    score=self._search_score,
                    payload=p.payload,
                )
            )
        return hits[:k]

    async def delete(self, collection: str, point_id: str) -> bool:
        if self._fail_delete:
            raise RuntimeError("delete error")
        pts = self.collections.get(collection, [])
        before = len(pts)
        self.collections[collection] = [p for p in pts if p.point_id != point_id]
        return len(self.collections[collection]) < before


class _FakeAuditLogger:
    """Stub audit logger that records log calls."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []

    async def log(self, action: str, payload: dict[str, object]) -> None:
        self.entries.append((action, payload))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    store: _FakeVectorStore | None = None,
    embedder: _FakeEmbedder | None = None,
    audit: _FakeAuditLogger | None = None,
) -> SemanticMemory:
    return SemanticMemory(
        store=store or _FakeVectorStore(),
        embedder=embedder or _FakeEmbedder(),
        audit=audit,
        vector_size=3,
    )


# ---------------------------------------------------------------------------
# initialise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialise_creates_all_three_collections() -> None:
    store = _FakeVectorStore()
    mem = SemanticMemory(store=store, embedder=_FakeEmbedder(), vector_size=3)

    await mem.initialise()

    created_names = {name for name, _ in store.ensure_calls}
    assert created_names == set(_ALL_COLLECTIONS)


@pytest.mark.asyncio
async def test_initialise_passes_vector_size_to_store() -> None:
    store = _FakeVectorStore()
    mem = SemanticMemory(store=store, embedder=_FakeEmbedder(vector_size=42), vector_size=42)

    await mem.initialise()

    for _, size in store.ensure_calls:
        assert size == 42


@pytest.mark.asyncio
async def test_initialise_raises_on_vector_size_mismatch() -> None:
    store = _FakeVectorStore()
    # Embedder emits 768-dim vectors but SemanticMemory is configured for 384.
    mem = SemanticMemory(store=store, embedder=_FakeEmbedder(vector_size=768), vector_size=384)

    with pytest.raises(SemanticMemoryError, match="dimension"):
        await mem.initialise()


# ---------------------------------------------------------------------------
# remember
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_returns_memory_id_with_collection_prefix() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store)
    await mem.initialise()

    memory_id = await mem.remember("hello world", tags=["a"], metadata={})

    assert memory_id.startswith(COLLECTION_FACTS + _ID_SEPARATOR)


@pytest.mark.asyncio
async def test_remember_stores_point_in_correct_collection() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store)
    await mem.initialise()

    await mem.remember("pref text", tags=[], metadata={}, collection=COLLECTION_PREFERENCES)

    assert len(store.collections[COLLECTION_PREFERENCES]) == 1
    assert len(store.collections.get(COLLECTION_FACTS, [])) == 0


@pytest.mark.asyncio
async def test_remember_stores_text_and_tags_in_payload() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store)
    await mem.initialise()

    await mem.remember("some fact", tags=["x", "y"], metadata={"source": "test"})

    point = store.collections[COLLECTION_FACTS][0]
    assert point.payload["text"] == "some fact"
    assert point.payload["tags"] == ["x", "y"]
    assert point.payload["source"] == "test"


@pytest.mark.asyncio
async def test_remember_calls_embedder_with_text() -> None:
    embedder = _FakeEmbedder()
    mem = _make_memory(embedder=embedder)
    await mem.initialise()

    await mem.remember("embed me", tags=[], metadata={})

    assert "embed me" in embedder.calls


@pytest.mark.asyncio
async def test_remember_raises_value_error_for_unknown_collection() -> None:
    mem = _make_memory()

    with pytest.raises(ValueError, match="Unknown collection"):
        await mem.remember("x", tags=[], metadata={}, collection="bogus_collection")


@pytest.mark.asyncio
async def test_remember_raises_semantic_memory_error_on_embed_failure() -> None:
    mem = _make_memory(embedder=_FakeEmbedder(fail=True))

    with pytest.raises(SemanticMemoryError, match="Embedding failed"):
        await mem.remember("x", tags=[], metadata={})


@pytest.mark.asyncio
async def test_remember_raises_semantic_memory_error_on_store_failure() -> None:
    mem = _make_memory(store=_FakeVectorStore(fail_upsert=True))

    with pytest.raises(SemanticMemoryError, match="Storage failed"):
        await mem.remember("x", tags=[], metadata={})


@pytest.mark.asyncio
async def test_remember_raises_semantic_memory_error_on_metadata_collision() -> None:
    mem = _make_memory()
    await mem.initialise()

    with pytest.raises(SemanticMemoryError, match="reserved payload fields"):
        await mem.remember("x", tags=[], metadata={"text": "sneaky override"})


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_returns_remembered_memory() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store)
    await mem.initialise()
    await mem.remember("dogs are great", tags=["animal"], metadata={})

    results = await mem.recall("great dogs", k=5)

    assert len(results) == 1
    assert results[0].text == "dogs are great"


@pytest.mark.asyncio
async def test_recall_empty_store_returns_empty_list() -> None:
    mem = _make_memory()
    await mem.initialise()

    results = await mem.recall("anything")

    assert results == []


@pytest.mark.asyncio
async def test_recall_tag_filter_excludes_non_matching_memories() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store)
    await mem.initialise()
    await mem.remember("tagged", tags=["keep"], metadata={})
    await mem.remember("untagged", tags=["drop"], metadata={})

    results = await mem.recall("query", tags=["keep"])

    assert len(results) == 1
    assert results[0].text == "tagged"


@pytest.mark.asyncio
async def test_recall_restricted_to_single_collection() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store)
    await mem.initialise()
    await mem.remember("fact memory", tags=[], metadata={}, collection=COLLECTION_FACTS)
    await mem.remember("doc memory", tags=[], metadata={}, collection=COLLECTION_DOCUMENTS)

    results = await mem.recall("query", collection=COLLECTION_FACTS)

    assert all(r.collection == COLLECTION_FACTS for r in results)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_recall_across_collections_sorted_by_score() -> None:
    store = _FakeVectorStore(search_score=0.75)
    mem = _make_memory(store=store)
    await mem.initialise()
    await mem.remember("a", tags=[], metadata={}, collection=COLLECTION_FACTS)
    await mem.remember("b", tags=[], metadata={}, collection=COLLECTION_PREFERENCES)

    results = await mem.recall("query", k=10)

    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_recall_respects_k_limit_across_collections() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store)
    await mem.initialise()
    for i in range(5):
        await mem.remember(f"fact {i}", tags=[], metadata={}, collection=COLLECTION_FACTS)
    for i in range(5):
        await mem.remember(f"pref {i}", tags=[], metadata={}, collection=COLLECTION_PREFERENCES)

    results = await mem.recall("query", k=3)

    assert len(results) <= 3


@pytest.mark.asyncio
async def test_recall_raises_value_error_for_unknown_collection() -> None:
    mem = _make_memory()

    with pytest.raises(ValueError, match="Unknown collection"):
        await mem.recall("q", collection="nonexistent")


@pytest.mark.asyncio
async def test_recall_raises_semantic_memory_error_on_embed_failure() -> None:
    mem = _make_memory(embedder=_FakeEmbedder(fail=True))

    with pytest.raises(SemanticMemoryError, match="Embedding failed"):
        await mem.recall("query")


@pytest.mark.asyncio
async def test_recall_raises_semantic_memory_error_on_search_failure() -> None:
    mem = _make_memory(store=_FakeVectorStore(fail_search=True))

    with pytest.raises(SemanticMemoryError, match="Search failed"):
        await mem.recall("query")


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_removes_memory_from_store() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store)
    await mem.initialise()
    memory_id = await mem.remember("to be forgotten", tags=[], metadata={})

    await mem.forget(memory_id, reason="test cleanup")

    assert store.collections[COLLECTION_FACTS] == []


@pytest.mark.asyncio
async def test_forget_logs_to_audit_when_audit_provided() -> None:
    audit = _FakeAuditLogger()
    store = _FakeVectorStore()
    mem = _make_memory(store=store, audit=audit)
    await mem.initialise()
    memory_id = await mem.remember("to be forgotten", tags=[], metadata={})

    await mem.forget(memory_id, reason="user request")

    assert len(audit.entries) == 1
    action, payload = audit.entries[0]
    assert action == "semantic_memory.forget"
    assert payload["memory_id"] == memory_id
    assert payload["reason"] == "user request"
    assert payload["collection"] == COLLECTION_FACTS


@pytest.mark.asyncio
async def test_forget_does_not_require_audit_logger() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store, audit=None)
    await mem.initialise()
    memory_id = await mem.remember("x", tags=[], metadata={})

    # Must not raise even without an audit logger.
    await mem.forget(memory_id, reason="no audit")


@pytest.mark.asyncio
async def test_forget_is_idempotent_for_missing_memory() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store)
    await mem.initialise()
    memory_id = await mem.remember("x", tags=[], metadata={})

    await mem.forget(memory_id, reason="first delete")

    # Second forget of the same id must be a no-op, not raise.
    await mem.forget(memory_id, reason="second delete")


@pytest.mark.asyncio
async def test_forget_raises_semantic_memory_error_on_delete_failure() -> None:
    store = _FakeVectorStore()
    mem = _make_memory(store=store)
    await mem.initialise()
    memory_id = await mem.remember("x", tags=[], metadata={})

    store._fail_delete = True

    with pytest.raises(SemanticMemoryError, match="Deletion failed"):
        await mem.forget(memory_id, reason="forced failure")


@pytest.mark.asyncio
async def test_forget_raises_semantic_memory_error_for_invalid_memory_id() -> None:
    mem = _make_memory()

    with pytest.raises(SemanticMemoryError, match="Invalid memory_id format"):
        await mem.forget("no-separator-here", reason="test")


@pytest.mark.asyncio
async def test_forget_raises_semantic_memory_error_for_unknown_collection_in_id() -> None:
    mem = _make_memory()

    with pytest.raises(SemanticMemoryError, match="Unknown collection"):
        await mem.forget(f"bad_collection{_ID_SEPARATOR}some-uuid", reason="test")


# ---------------------------------------------------------------------------
# _parse_collection (internal helper, tested via public interface indirectly
# but also directly to cover edge cases without needing a round-trip)
# ---------------------------------------------------------------------------


def test_parse_collection_returns_correct_collection() -> None:
    memory_id = f"{COLLECTION_FACTS}{_ID_SEPARATOR}abc-123"

    assert _parse_collection(memory_id) == COLLECTION_FACTS


def test_parse_collection_raises_for_missing_separator() -> None:
    with pytest.raises(SemanticMemoryError, match="Invalid memory_id format"):
        _parse_collection("noseparator")


def test_parse_collection_raises_for_unknown_collection() -> None:
    with pytest.raises(SemanticMemoryError, match="Unknown collection"):
        _parse_collection(f"unknown{_ID_SEPARATOR}uuid")


# ---------------------------------------------------------------------------
# _hit_to_memory
# ---------------------------------------------------------------------------


def test_hit_to_memory_raises_on_missing_created_at() -> None:
    hit = SearchHit(
        id=f"{COLLECTION_FACTS}{_ID_SEPARATOR}test-uuid",
        score=0.9,
        payload={
            "memory_id": f"{COLLECTION_FACTS}{_ID_SEPARATOR}test-uuid",
            "text": "some text",
            "tags": [],
            "collection": COLLECTION_FACTS,
            # 'created_at' intentionally omitted
        },
    )

    with pytest.raises(SemanticMemoryError, match="missing required field"):
        _hit_to_memory(hit, COLLECTION_FACTS)
