"""World Encoding Pipeline for the Embedding World (Phase 3D).

Orchestrates the :class:`~coremind.world.embeddings.EmbeddingEncoder`,
:class:`~coremind.world.differ.SnapshotDiffer`,
:class:`~coremind.world.snapshot_memory.SnapshotMemory`, and
:class:`~coremind.world.compressed_prompt.CompressedPromptBuilder` into a
single ``process()`` call.

Includes graceful fallback to full-text snapshots when the embedding
service is unreachable.
"""

from __future__ import annotations

import json
import uuid

import structlog

from coremind.world.compressed_prompt import CompressedPrompt, CompressedPromptBuilder
from coremind.world.differ import SnapshotDiff, SnapshotDiffer
from coremind.world.embeddings import EmbeddingEncoder, EncoderError
from coremind.world.model import WorldSnapshot
from coremind.world.snapshot_memory import SnapshotMemory

log = structlog.get_logger(__name__)


class WorldEncodingPipeline:
    """Orchestrates encoder + differ + memory + prompt builder.

    Produces a :class:`CompressedPrompt` from each new
    :class:`WorldSnapshot`.  Falls back to full-text JSON when the
    embedding service is unavailable.

    Args:
        encoder: Embedding encoder for snapshot vectorization.
        differ: Stateless snapshot differ.
        memory: Qdrant-backed snapshot embedding store.
        prompt_builder: Builds compact prompts from diffs + similarity.
    """

    def __init__(
        self,
        encoder: EmbeddingEncoder,
        differ: SnapshotDiffer,
        memory: SnapshotMemory,
        prompt_builder: CompressedPromptBuilder,
    ) -> None:
        self._encoder = encoder
        self._differ = differ
        self._memory = memory
        self._prompt_builder = prompt_builder
        self._fallback_active = False
        self._previous_snapshot: WorldSnapshot | None = None

    @property
    def fallback_active(self) -> bool:
        """True when the encoder is unreachable and we are using full-text fallback."""
        return self._fallback_active

    async def process(self, current: WorldSnapshot) -> CompressedPrompt:
        """Process a new snapshot and return a CompressedPrompt for L4/L5.

        Stores the embedding in Qdrant for future similarity queries.
        Falls back to full-text output on encoder failure.

        Args:
            current: The latest world snapshot.

        Returns:
            A CompressedPrompt suitable for LLM consumption.
        """
        diff = self._differ.diff(current, self._previous_snapshot)

        try:
            embedding = await self._encoder.encode_snapshot(current)

            snapshot_id = uuid.uuid4().hex
            await self._memory.store(
                snapshot_id=snapshot_id,
                vector=embedding,
                summary=f"{diff.total_current} entities, {diff.change_summary}",
                entity_count=diff.total_current,
                timestamp=current.taken_at,
            )

            prompt = await self._prompt_builder.build(current, diff, embedding)

            if self._fallback_active:
                log.info("encoding.recovered")
                self._fallback_active = False

            self._previous_snapshot = current
            return prompt

        except EncoderError as exc:
            if not self._fallback_active:
                log.warning("encoding.fallback_active", error=str(exc))
                self._fallback_active = True

            self._previous_snapshot = current
            return self._build_fallback(current, diff)

    def _build_fallback(self, snapshot: WorldSnapshot, diff: SnapshotDiff) -> CompressedPrompt:
        """Produce a v1-style full-text fallback prompt."""
        full_text = json.dumps(
            [e.model_dump(mode="json") for e in snapshot.entities],
            indent=2,
            default=str,
        )
        return CompressedPrompt(
            summary=f"{diff.total_current} entities (embedding service unavailable)",
            changes_text=full_text,
            similar_states_text="",
            key_metrics_text="",
            full_fallback=full_text,
        )
