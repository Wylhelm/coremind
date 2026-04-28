"""Embedding providers for semantic memory (L3).

Provides the :class:`Embedder` Protocol (re-exported from
:mod:`coremind.memory.semantic`) and concrete implementations:

- :class:`SentenceTransformersEmbedder` — local model, default multilingual-e5-small (384-dim).
- :class:`OllamaEmbedder` — Ollama HTTP embedding API.
- :class:`OpenAIEmbedder` — OpenAI ``text-embedding-*`` HTTP API.
- :class:`HashEmbedder` — deterministic pseudo-embedder for unit tests.

The :func:`build_embedder` factory resolves a concrete embedder from a
:class:`EmbedderConfig` that can be loaded from ``~/.coremind/config.toml``.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
from typing import Literal

import aiohttp
import structlog
from pydantic import BaseModel, Field

from coremind.errors import EmbeddingError

log = structlog.get_logger(__name__)

DEFAULT_MODEL: str = "intfloat/multilingual-e5-small"
DEFAULT_DIM: int = 384


class EmbedderConfig(BaseModel):
    """Configuration for the embedding provider.

    Attributes:
        provider: Which backend to use.
        model: Model name (provider-specific).
        dimension: Expected vector dimensionality.
        endpoint: Base URL for HTTP-based providers (Ollama, OpenAI).
        api_key_env: Name of the environment variable holding the API key
            (OpenAI only).  The key itself is never stored in config.
    """

    provider: Literal["sentence-transformers", "ollama", "openai", "hash"] = "sentence-transformers"
    model: str = DEFAULT_MODEL
    dimension: int = Field(default=DEFAULT_DIM, ge=1)
    endpoint: str = "http://127.0.0.1:11434"
    api_key_env: str = "OPENAI_API_KEY"


_HTTP_OK = 200


# Sentence-transformers (local) ---------------------------------------------


class SentenceTransformersEmbedder:
    """Local embedding model via ``sentence-transformers``.

    The model is loaded lazily on first use so importing this module is cheap.
    Encoding runs in a thread pool to keep the event loop responsive.

    Args:
        model_name: A Hugging Face model identifier loadable by
            ``sentence_transformers.SentenceTransformer``.
        dimension: Expected output dimension (used to validate the loaded model).
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        dimension: int = DEFAULT_DIM,
    ) -> None:
        self._model_name = model_name
        self._dimension = dimension
        self._model: object | None = None
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> object:
        """Lazy-load the model on first use."""
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is not None:
                return self._model
            try:
                from sentence_transformers import (  # noqa: PLC0415 — optional dep
                    SentenceTransformer,
                )
            except ImportError as exc:
                raise EmbeddingError(
                    "sentence-transformers is not installed; install the 'embeddings-local' extra"
                ) from exc

            def _load() -> object:
                return SentenceTransformer(self._model_name)

            try:
                self._model = await asyncio.to_thread(_load)
            except Exception as exc:
                raise EmbeddingError(
                    f"Failed to load sentence-transformers model {self._model_name!r}"
                ) from exc
            log.info("embedder.loaded", model=self._model_name)
            return self._model

    async def embed(self, text: str) -> list[float]:
        """Encode *text* into a dense vector.

        Args:
            text: Input text.

        Returns:
            A ``self._dimension``-dim float vector.

        Raises:
            EmbeddingError: If the model fails to load or encode.
        """
        model = await self._ensure_loaded()

        def _encode() -> list[float]:
            # encode returns a numpy array; convert to list[float]
            vec = model.encode(text, normalize_embeddings=True)  # type: ignore[attr-defined]
            return [float(x) for x in vec.tolist()]

        try:
            vector = await asyncio.to_thread(_encode)
        except Exception as exc:
            raise EmbeddingError("sentence-transformers encode failed") from exc

        if len(vector) != self._dimension:
            raise EmbeddingError(
                f"Model {self._model_name!r} produced {len(vector)}-dim vector, "
                f"expected {self._dimension}"
            )
        return vector


# Ollama (HTTP) -------------------------------------------------------------


class OllamaEmbedder:
    """Embeddings via the Ollama HTTP API.

    Args:
        endpoint: Ollama base URL (e.g. ``http://127.0.0.1:11434``).
        model: Model tag (e.g. ``nomic-embed-text``).
        dimension: Expected output dimension.
        timeout_seconds: Per-request timeout.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        dimension: int = DEFAULT_DIM,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._dimension = dimension
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def embed(self, text: str) -> list[float]:
        """Call the Ollama embedding endpoint.

        Raises:
            EmbeddingError: On HTTP failure or malformed response.
        """
        url = f"{self._endpoint}/api/embeddings"
        payload = {"model": self._model, "prompt": text}
        try:
            async with (
                aiohttp.ClientSession(timeout=self._timeout) as session,
                session.post(url, json=payload) as resp,
            ):
                if resp.status != _HTTP_OK:
                    body = await resp.text()
                    raise EmbeddingError(
                        f"Ollama embed returned HTTP {resp.status}: {body[:200]!r}"
                    )
                data = await resp.json()
        except aiohttp.ClientError as exc:
            raise EmbeddingError(f"Ollama embed transport error: {exc}") from exc

        vec = data.get("embedding")
        if not isinstance(vec, list):
            raise EmbeddingError("Ollama response missing 'embedding' list field")
        if len(vec) != self._dimension:
            raise EmbeddingError(
                f"Ollama returned {len(vec)}-dim vector, expected {self._dimension}"
            )
        return [float(x) for x in vec]


# OpenAI (HTTP) -------------------------------------------------------------


class OpenAIEmbedder:
    """Embeddings via the OpenAI HTTP API.

    Args:
        model: Model name (e.g. ``text-embedding-3-small``).
        dimension: Expected output dimension.
        api_key: API key; ``None`` means read from ``OPENAI_API_KEY`` env.
        endpoint: API base URL.
        timeout_seconds: Per-request timeout.
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        *,
        dimension: int = 1536,
        api_key: str | None = None,
        endpoint: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._model = model
        self._dimension = dimension
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._endpoint = endpoint.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def embed(self, text: str) -> list[float]:
        """Call the OpenAI embedding endpoint."""
        if not self._api_key:
            raise EmbeddingError("OPENAI_API_KEY is not set")
        url = f"{self._endpoint}/embeddings"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload = {"model": self._model, "input": text}
        try:
            async with (
                aiohttp.ClientSession(timeout=self._timeout, headers=headers) as session,
                session.post(url, json=payload) as resp,
            ):
                if resp.status != _HTTP_OK:
                    body = await resp.text()
                    raise EmbeddingError(
                        f"OpenAI embed returned HTTP {resp.status}: {body[:200]!r}"
                    )
                data = await resp.json()
        except aiohttp.ClientError as exc:
            raise EmbeddingError(f"OpenAI embed transport error: {exc}") from exc

        try:
            vec = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EmbeddingError("OpenAI embedding response malformed") from exc
        if len(vec) != self._dimension:
            raise EmbeddingError(
                f"OpenAI returned {len(vec)}-dim vector, expected {self._dimension}"
            )
        return [float(x) for x in vec]


# Hash-based (test / offline) -----------------------------------------------


class HashEmbedder:
    """Deterministic pseudo-embedder for tests and offline runs.

    Produces a unit-norm vector derived from SHA-256 of the input text.
    Not semantically meaningful — only useful as a stable test double that
    preserves per-text determinism and returns a consistent dimension.

    Args:
        dimension: Output vector dimension.
    """

    def __init__(self, dimension: int = DEFAULT_DIM) -> None:
        self._dimension = dimension

    async def embed(self, text: str) -> list[float]:
        """Produce a deterministic unit-norm vector for *text*."""
        # Expand the 32-byte digest by hashing seeded counters until we have
        # enough bytes for ``dimension * 2`` (uint16 per component).
        needed = self._dimension * 2
        buf = bytearray()
        counter = 0
        while len(buf) < needed:
            h = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            buf.extend(h)
            counter += 1
        raw = buf[:needed]
        # Map bytes to signed components in [-1.0, 1.0)
        vec = [
            (int.from_bytes(raw[i : i + 2], "big", signed=True) / 32768.0)
            for i in range(0, needed, 2)
        ]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_embedder(
    config: EmbedderConfig,
) -> SentenceTransformersEmbedder | OllamaEmbedder | OpenAIEmbedder | HashEmbedder:
    """Construct an embedder from configuration.

    Args:
        config: Validated :class:`EmbedderConfig`.

    Returns:
        A concrete embedder instance.

    Raises:
        EmbeddingError: If the provider string is unrecognised.
    """
    if config.provider == "sentence-transformers":
        return SentenceTransformersEmbedder(
            model_name=config.model,
            dimension=config.dimension,
        )
    if config.provider == "ollama":
        return OllamaEmbedder(
            endpoint=config.endpoint,
            model=config.model,
            dimension=config.dimension,
        )
    if config.provider == "openai":
        return OpenAIEmbedder(
            model=config.model,
            dimension=config.dimension,
            api_key=os.environ.get(config.api_key_env),
        )
    if config.provider == "hash":
        return HashEmbedder(dimension=config.dimension)
    raise EmbeddingError(f"Unknown embedder provider: {config.provider!r}")
