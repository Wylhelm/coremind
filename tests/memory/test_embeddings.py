"""Tests for embedding providers."""

from __future__ import annotations

import math

import pytest

from coremind.errors import EmbeddingError
from coremind.memory.embeddings import (
    DEFAULT_DIM,
    EmbedderConfig,
    HashEmbedder,
    build_embedder,
)


@pytest.mark.asyncio
async def test_hash_embedder_is_deterministic() -> None:
    embedder = HashEmbedder(dimension=64)
    a = await embedder.embed("hello world")
    b = await embedder.embed("hello world")
    assert a == b


@pytest.mark.asyncio
async def test_hash_embedder_different_texts_differ() -> None:
    embedder = HashEmbedder(dimension=64)
    a = await embedder.embed("alpha")
    b = await embedder.embed("beta")
    assert a != b


@pytest.mark.asyncio
async def test_hash_embedder_respects_dimension() -> None:
    embedder = HashEmbedder(dimension=128)
    vec = await embedder.embed("x")
    assert len(vec) == 128


@pytest.mark.asyncio
async def test_hash_embedder_produces_unit_norm() -> None:
    embedder = HashEmbedder(dimension=32)
    vec = await embedder.embed("arbitrary input")
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-6


def test_build_embedder_hash() -> None:
    emb = build_embedder(EmbedderConfig(provider="hash", dimension=DEFAULT_DIM))
    assert isinstance(emb, HashEmbedder)


def test_build_embedder_unknown_provider_raises() -> None:
    # construct a config with a bad provider by bypassing literal validation
    cfg = EmbedderConfig(provider="hash")
    object.__setattr__(cfg, "provider", "unknown")
    with pytest.raises(EmbeddingError):
        build_embedder(cfg)
