"""Embedding encoder for the World Model (L2).

Wraps a raw embedder (e.g. :class:`~coremind.memory.embeddings.OllamaEmbedder`)
with content-hash caching, entity-to-text conversion, and weighted snapshot
averaging.  Used by the compressed-prompt pipeline to convert entities and
snapshots into 768-d vectors without redundant remote calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import structlog
from pydantic import BaseModel

from coremind.errors import EmbeddingError
from coremind.world.model import Entity, WorldSnapshot

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Protocol for raw embedders
# ---------------------------------------------------------------------------


@runtime_checkable
class Embedder(Protocol):
    """Protocol satisfied by OllamaEmbedder, HashEmbedder, etc."""

    async def embed(self, text: str) -> list[float]: ...


# ---------------------------------------------------------------------------
# Stats model
# ---------------------------------------------------------------------------


class EncoderStats(BaseModel):
    """Runtime statistics for the embedding encoder."""

    cache_hits: int = 0
    cache_misses: int = 0
    errors: int = 0
    total_encode_ms: float = 0.0

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of encode calls served from cache."""
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0

    @property
    def avg_encode_ms(self) -> float:
        """Average latency of cache-miss calls (ms)."""
        return self.total_encode_ms / self.cache_misses if self.cache_misses > 0 else 0.0


# ---------------------------------------------------------------------------
# Encoder error
# ---------------------------------------------------------------------------


class EncoderError(EmbeddingError):
    """Raised when the embedding encoder fails."""


# ---------------------------------------------------------------------------
# EmbeddingEncoder
# ---------------------------------------------------------------------------


class EmbeddingEncoder:
    """Caching, weight-aware embedding encoder for WorldModel entities.

    Delegates raw embedding calls to an :class:`Embedder` instance and adds:
    - SHA-256 content-hash LRU cache
    - Deterministic entity-to-text conversion
    - Recency + complexity weighted snapshot averaging

    Args:
        embedder: Any object satisfying the :class:`Embedder` protocol.
        dimension: Expected vector dimensionality.
        cache_size: Maximum cached vectors before LRU eviction.
    """

    def __init__(
        self,
        embedder: Embedder,
        *,
        dimension: int = 768,
        cache_size: int = 5000,
    ) -> None:
        self._embedder = embedder
        self._dimension = dimension
        self._cache: dict[str, list[float]] = {}
        self._cache_max = cache_size
        self._stats = EncoderStats()

    @property
    def stats(self) -> EncoderStats:
        """Current encoder statistics."""
        return self._stats

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def encode_text(self, text: str) -> list[float]:
        """Encode text to a vector. Cached by SHA-256 of input.

        Args:
            text: Arbitrary text to embed.

        Returns:
            A vector of ``self._dimension`` floats.

        Raises:
            EncoderError: If the underlying embedder fails.
        """
        cache_key = self._hash(text)
        if cache_key in self._cache:
            self._stats.cache_hits += 1
            return self._cache[cache_key]

        self._stats.cache_misses += 1
        start = time.monotonic()
        try:
            vector = await self._embedder.embed(text)
        except Exception as exc:
            self._stats.errors += 1
            raise EncoderError(f"Embedding failed: {exc}") from exc
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._stats.total_encode_ms += elapsed_ms

        # LRU-style eviction: drop oldest entry when at capacity
        if len(self._cache) >= self._cache_max:
            self._cache.pop(next(iter(self._cache)))
        self._cache[cache_key] = vector
        return vector

    async def encode_entity(self, entity: Entity) -> list[float]:
        """Encode a single entity to a vector.

        Args:
            entity: A World Model entity.

        Returns:
            A vector of ``self._dimension`` floats.
        """
        text = self._entity_to_text(entity)
        return await self.encode_text(text)

    async def encode_snapshot(self, snapshot: WorldSnapshot) -> list[float]:
        """Encode a full snapshot as a weighted average of entity embeddings.

        Args:
            snapshot: A World Model snapshot.

        Returns:
            A vector of ``self._dimension`` floats.  Returns a zero-vector
            if the snapshot contains no entities.
        """
        if not snapshot.entities:
            return [0.0] * self._dimension

        entity_vectors = await asyncio.gather(*(self.encode_entity(e) for e in snapshot.entities))
        weights = [self._compute_weight(e) for e in snapshot.entities]
        return self._weighted_average(entity_vectors, weights)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _entity_to_text(self, entity: Entity) -> str:
        """Convert entity to deterministic text for embedding.

        Format: ``type:display_name | prop1=val1 | prop2=val2``
        Properties are sorted alphabetically for determinism.
        """
        parts = [f"{entity.type}:{entity.display_name}"]
        for attr, value in sorted(entity.properties.items()):
            parts.append(f"{attr}={value}")
        return " | ".join(parts)

    def _compute_weight(self, entity: Entity) -> float:
        """Compute embedding weight for weighted average.

        Recent + complex entities get higher weight.
        """
        weight = 1.0
        # Recency factor: decays over 24h to a floor of 0.1
        age_hours = (datetime.now(UTC) - entity.updated_at).total_seconds() / 3600
        weight *= max(0.1, 1.0 - (age_hours / 24))
        # Complexity factor: more properties → slightly higher weight
        weight *= 1 + min(len(entity.properties), 10) / 10
        return weight

    def _weighted_average(
        self,
        vectors: Sequence[list[float]],
        weights: Sequence[float],
    ) -> list[float]:
        """Compute weighted average of vectors."""
        total_weight = sum(weights)
        if total_weight == 0:
            return [0.0] * self._dimension
        result = [0.0] * len(vectors[0])
        for vec, w in zip(vectors, weights, strict=True):
            for i, v in enumerate(vec):
                result[i] += v * w / total_weight
        return result

    @staticmethod
    def _hash(text: str) -> str:
        """SHA-256 hash of text for cache keying."""
        return hashlib.sha256(text.encode()).hexdigest()
