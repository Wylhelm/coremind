# Phase 3B — Embedding Encoder

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_3_EMBEDDING_WORLD.md](PHASE_3_EMBEDDING_WORLD.md)
**Prerequisites:** None (parallel with 3A)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Create `EmbeddingEncoder` — wraps the local Ollama `nomic-embed-text` model to produce 768-dimensional vectors from text. Includes content-hash caching, weight computation for entities, and graceful error handling.

---

## 2. Deliverables

| File | Purpose |
| --- | --- |
| `src/coremind/world/embeddings.py` | `EmbeddingEncoder`, `EncoderStats`, `EncoderError` |
| `tests/world/test_embeddings.py` | Unit tests (mocked HTTP) |

---

## 3. Data Model

```python
class EncoderStats(BaseModel):
    """Runtime statistics for the encoder."""
    cache_hits: int = 0
    cache_misses: int = 0
    errors: int = 0
    total_encode_ms: float = 0.0

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0

    @property
    def avg_encode_ms(self) -> float:
        return self.total_encode_ms / self.cache_misses if self.cache_misses > 0 else 0.0


class EncoderError(Exception):
    """Raised when the embedding service is unreachable or returns an error."""
```

---

## 4. Implementation

```python
class EmbeddingEncoder:
    """Wraps Ollama nomic-embed-text. Caches embeddings by content hash."""

    VECTOR_DIM: ClassVar[int] = 768

    def __init__(
        self,
        ollama_url: str = "http://OLLAMA_HOST:11434",
        model: str = "nomic-embed-text",
        cache_size: int = 5000,
        timeout_seconds: float = 10.0,
    ):
        self._url = ollama_url.rstrip("/") + "/api/embeddings"
        self._model = model
        self._cache: dict[str, list[float]] = {}
        self._cache_max = cache_size
        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        self._stats = EncoderStats()

    @property
    def stats(self) -> EncoderStats:
        return self._stats

    async def encode_text(self, text: str) -> list[float]:
        """Encode text to a 768-d vector. Cached by SHA-256 of input."""
        cache_key = self._hash(text)
        if cache_key in self._cache:
            self._stats.cache_hits += 1
            return self._cache[cache_key]

        self._stats.cache_misses += 1
        start = time.monotonic()
        try:
            response = await self._client.post(
                self._url,
                json={"model": self._model, "prompt": text},
            )
            response.raise_for_status()
            vector = response.json()["embedding"]
        except Exception as e:
            self._stats.errors += 1
            raise EncoderError(f"Embedding failed: {e}") from e
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._stats.total_encode_ms += elapsed_ms

        # LRU-style eviction: drop oldest entry
        if len(self._cache) >= self._cache_max:
            self._cache.pop(next(iter(self._cache)))
        self._cache[cache_key] = vector
        return vector

    async def encode_entity(self, entity: Entity) -> list[float]:
        """Encode a single entity to a vector."""
        text = self._entity_to_text(entity)
        return await self.encode_text(text)

    async def encode_snapshot(self, snapshot: WorldSnapshot) -> list[float]:
        """Encode a full snapshot as a weighted average of entity embeddings."""
        if not snapshot.entities:
            return [0.0] * self.VECTOR_DIM

        entity_vectors = await asyncio.gather(*[
            self.encode_entity(e) for e in snapshot.entities
        ])
        weights = [self._compute_weight(e) for e in snapshot.entities]
        return self._weighted_average(entity_vectors, weights)

    def _entity_to_text(self, entity: Entity) -> str:
        """Convert entity to text for embedding. Deterministic output."""
        parts = [f"{entity.entity_type}:{entity.entity_id}"]
        for attr, value in sorted(entity.attributes.items()):
            parts.append(f"{attr}={value}")
        if hasattr(entity, "room") and entity.room:
            parts.append(f"room={entity.room}")
        return " | ".join(parts)

    def _compute_weight(self, entity: Entity) -> float:
        """Weight for weighted-average embedding. Recent + complex = higher."""
        weight = 1.0
        if hasattr(entity, "last_updated") and entity.last_updated:
            age_hours = (datetime.now(UTC) - entity.last_updated).total_seconds() / 3600
            weight *= max(0.1, 1.0 - (age_hours / 24))
        weight *= 1 + min(len(entity.attributes), 10) / 10
        return weight

    def _weighted_average(
        self, vectors: list[list[float]], weights: list[float]
    ) -> list[float]:
        total_weight = sum(weights)
        if total_weight == 0:
            return [0.0] * len(vectors[0])
        result = [0.0] * len(vectors[0])
        for vec, w in zip(vectors, weights, strict=True):
            for i, v in enumerate(vec):
                result[i] += v * w / total_weight
        return result

    def _hash(self, text: str) -> str:
        import hashlib
        return hashlib.sha256(text.encode()).hexdigest()

    async def close(self) -> None:
        """Shutdown the HTTP client."""
        await self._client.aclose()
```

---

## 5. Tests

```python
# tests/world/test_embeddings.py

@pytest.mark.asyncio
async def test_encode_text_returns_768_dim_vector():
    encoder = make_encoder_with_mock(return_vector=[0.1] * 768)
    vector = await encoder.encode_text("hello world")
    assert len(vector) == 768

@pytest.mark.asyncio
async def test_encode_text_caches_by_hash():
    mock_client = MockClient(return_vector=[0.1] * 768)
    encoder = make_encoder_with_mock(client=mock_client)

    v1 = await encoder.encode_text("hello")
    v2 = await encoder.encode_text("hello")

    assert v1 == v2
    assert encoder.stats.cache_hits == 1
    assert encoder.stats.cache_misses == 1
    assert mock_client.call_count == 1

@pytest.mark.asyncio
async def test_encode_text_different_inputs_not_cached():
    mock_client = MockClient(return_vector=[0.1] * 768)
    encoder = make_encoder_with_mock(client=mock_client)

    await encoder.encode_text("hello")
    await encoder.encode_text("world")

    assert encoder.stats.cache_misses == 2
    assert mock_client.call_count == 2

@pytest.mark.asyncio
async def test_encode_text_raises_encoder_error_on_failure():
    encoder = make_encoder_with_mock(raise_error=True)
    with pytest.raises(EncoderError):
        await encoder.encode_text("hello")
    assert encoder.stats.errors == 1

@pytest.mark.asyncio
async def test_cache_eviction_when_full():
    encoder = make_encoder_with_mock(return_vector=[0.1] * 768, cache_size=2)
    await encoder.encode_text("a")
    await encoder.encode_text("b")
    await encoder.encode_text("c")  # evicts "a"
    assert len(encoder._cache) == 2

@pytest.mark.asyncio
async def test_encode_snapshot_empty_returns_zero_vector():
    encoder = make_encoder_with_mock(return_vector=[0.1] * 768)
    snapshot = make_empty_snapshot()
    vector = await encoder.encode_snapshot(snapshot)
    assert vector == [0.0] * 768

@pytest.mark.asyncio
async def test_encode_snapshot_weighted_average():
    encoder = make_encoder_with_mock(return_vector=[1.0] * 768)
    snapshot = make_snapshot(["light.a", "light.b"])
    vector = await encoder.encode_snapshot(snapshot)
    # All vectors are [1.0]*768 so weighted avg is also [1.0]*768
    assert all(abs(v - 1.0) < 1e-6 for v in vector)

def test_entity_to_text_deterministic():
    encoder = EmbeddingEncoder.__new__(EmbeddingEncoder)
    entity = make_entity("light.bureau", state="on", brightness="200")
    text = encoder._entity_to_text(entity)
    assert "light:" in text or "light.bureau" in text
    # Attributes are sorted
    assert text.index("brightness") < text.index("state")

def test_compute_weight_recent_higher():
    encoder = EmbeddingEncoder.__new__(EmbeddingEncoder)
    fresh = make_entity("e1", last_updated=datetime.now(UTC))
    stale = make_entity("e2", last_updated=datetime.now(UTC) - timedelta(hours=20))
    assert encoder._compute_weight(fresh) > encoder._compute_weight(stale)

def test_stats_cache_hit_rate():
    stats = EncoderStats(cache_hits=80, cache_misses=20)
    assert abs(stats.cache_hit_rate - 0.8) < 1e-6
```

---

## 6. Configuration

Add to `EmbeddingConfig` (in `src/coremind/core/config.py` or wherever config lives):

```python
class EmbeddingConfig(BaseModel):
    enabled: bool = True
    encoder_url: str = "http://OLLAMA_HOST:11434"
    encoder_model: str = "nomic-embed-text"
    cache_size: int = 5000
    timeout_seconds: float = 10.0
```

Corresponding TOML:

```toml
[embedding]
enabled = true
encoder_url = "http://OLLAMA_HOST:11434"
encoder_model = "nomic-embed-text"
cache_size = 5000
timeout_seconds = 10.0
```

---

## 7. Dependencies

Add to `pyproject.toml` if not already present:

- `httpx` (likely already there)

No new third-party dependencies required for this subphase.

---

## 8. Success Criteria

- [ ] `EmbeddingEncoder` calls Ollama `/api/embeddings` endpoint correctly
- [ ] Content-hash caching prevents duplicate HTTP calls
- [ ] LRU eviction works when cache is full
- [ ] `EncoderError` is raised on HTTP failures (not bare exceptions)
- [ ] `encode_snapshot()` produces a weighted average
- [ ] Weight computation favors recent + complex entities
- [ ] `EncoderStats` tracks hits, misses, errors, timing
- [ ] All tests pass with mocked HTTP (no real Ollama needed)
- [ ] `mypy --strict` passes
- [ ] `ruff check` passes
