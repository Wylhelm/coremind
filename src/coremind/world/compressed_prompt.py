"""Compressed prompt builder for the Embedding World (Phase 3D).

Produces a compact, Markdown-formatted world-state representation for L4/L5
consumption.  Combines snapshot diffs with similarity search results to give
the LLM only what changed and what looks familiar — typically <3000 tokens
instead of the 15K-30K full JSON snapshots used in v1.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel

from coremind.world.differ import IGNORED_PROPERTIES, SnapshotDiff
from coremind.world.model import Entity, WorldSnapshot
from coremind.world.snapshot_memory import SimilarSnapshot, SnapshotMemory

log = structlog.get_logger(__name__)


class CompressedPrompt(BaseModel):
    """Compact world-state representation for LLM consumption."""

    summary: str
    changes_text: str
    similar_states_text: str
    key_metrics_text: str
    full_fallback: str | None = None

    @property
    def estimated_tokens(self) -> int:
        """Rough estimate: 1 token ≈ 4 chars."""
        return len(self.to_prompt_text()) // 4

    def to_prompt_text(self) -> str:
        """Render as final prompt text for LLM."""
        parts = [
            "## World State Summary",
            self.summary,
            "",
            "## Changes Since Last Cycle",
            self.changes_text,
            "",
        ]
        if self.similar_states_text:
            parts.extend(
                [
                    "## Similar Past States",
                    self.similar_states_text,
                    "",
                ]
            )
        if self.key_metrics_text:
            parts.extend(
                [
                    "## Key Metrics",
                    self.key_metrics_text,
                ]
            )
        return "\n".join(parts)


class CompressedPromptBuilder:
    """Builds compact prompts from diffs and similarity results.

    Formats a :class:`SnapshotDiff` and top-K similar past states into a
    human-readable, token-efficient prompt for the reasoning and intention
    layers.

    Args:
        memory: Qdrant-backed snapshot memory for similarity search.
        top_k: Number of similar past states to retrieve.
    """

    def __init__(self, memory: SnapshotMemory, *, top_k: int = 3) -> None:
        self._memory = memory
        self._top_k = top_k

    async def build(
        self,
        snapshot: WorldSnapshot,
        diff: SnapshotDiff,
        snapshot_embedding: list[float],
    ) -> CompressedPrompt:
        """Build a compressed prompt from a snapshot, its diff, and embedding.

        Args:
            snapshot: Current world snapshot.
            diff: Diff between current and previous snapshot.
            snapshot_embedding: Embedding vector for similarity search.

        Returns:
            A CompressedPrompt ready for LLM consumption.
        """
        # Include critical sensor values directly in summary so the LLM
        # always sees them, even when they haven't changed (no diff entry).
        critical_values: list[str] = []
        for entity in snapshot.entities:
            if entity.type == "health":
                for k in ["sleep_hours", "resting_heart_rate", "steps"]:
                    v = entity.properties.get(k)
                    if v is not None:
                        critical_values.append(f"{k}={v}")
        critical_line = f" Health: {', '.join(critical_values)}." if critical_values else ""

        summary = (
            f"{diff.total_current} entities, {diff.change_summary}."
            f"{critical_line}"
            f" Timestamp: {snapshot.taken_at.isoformat()}."
        )

        changes_text = self._format_changes(diff)

        similar = await self._memory.find_similar(snapshot_embedding, k=self._top_k)
        similar_text = self._format_similar(similar)

        metrics = self._compute_metrics(snapshot)
        metrics_text = self._format_metrics(metrics)

        return CompressedPrompt(
            summary=summary,
            changes_text=changes_text,
            similar_states_text=similar_text,
            key_metrics_text=metrics_text,
        )

    def _format_changes(self, diff: SnapshotDiff) -> str:
        """Format diff as concise added/removed/changed lines."""
        if not diff.has_changes:
            return "No changes since last cycle."

        lines: list[str] = []
        for entity in diff.added:
            lines.append(f"+ {entity.display_name}: {self._brief(entity)}")
        for entity in diff.removed:
            lines.append(f"- {entity.display_name} (removed)")
        for old, new in diff.changed:
            lines.append(f"~ {new.display_name}: {self._diff_attrs(old, new)}")
        return "\n".join(lines)

    def _brief(self, entity: Entity) -> str:
        """One-line entity summary (type + top 3 properties)."""
        important = sorted(
            (k, v) for k, v in entity.properties.items() if k not in IGNORED_PROPERTIES
        )[:3]
        attrs = ", ".join(f"{k}={v}" for k, v in important)
        return f"({entity.type}) {attrs}"

    def _diff_attrs(self, old: Entity, new: Entity) -> str:
        """Show only changed properties between old and new."""
        diffs: list[str] = []
        all_keys = set(old.properties.keys()) | set(new.properties.keys())
        for k in sorted(all_keys):
            if k in IGNORED_PROPERTIES:
                continue
            o = old.properties.get(k)
            n = new.properties.get(k)
            if o != n:
                diffs.append(f"{k}: {o} → {n}")
        return "; ".join(diffs) if diffs else "(no semantic change)"

    def _format_similar(self, similar: list[SimilarSnapshot]) -> str:
        """Format similar past states with age and similarity score."""
        if not similar:
            return ""
        lines: list[str] = []
        for s in similar:
            age = self._age_str(s.timestamp)
            lines.append(f"- {age} (similarity {s.score:.2f}): {s.summary}")
        return "\n".join(lines)

    def _compute_metrics(self, snapshot: WorldSnapshot) -> dict[str, Any]:
        """Compute summary metrics from snapshot."""
        metrics: dict[str, Any] = {
            "total_entities": len(snapshot.entities),
            "by_type": {},
        }
        for entity in snapshot.entities:
            t = entity.type
            metrics["by_type"][t] = metrics["by_type"].get(t, 0) + 1
        return metrics

    def _format_metrics(self, metrics: dict[str, Any]) -> str:
        """Format metrics as compact text."""
        lines = [f"Total entities: {metrics['total_entities']}"]
        by_type: dict[str, int] = metrics["by_type"]
        for entity_type, count in sorted(by_type.items()):
            lines.append(f"- {entity_type}: {count}")
        return "\n".join(lines)

    def _age_str(self, timestamp: datetime) -> str:
        """Human-readable age string from a timestamp."""
        seconds_per_hour = 3600
        delta = datetime.now(UTC) - timestamp
        if delta.days > 0:
            return f"{delta.days}d ago"
        if delta.total_seconds() > seconds_per_hour:
            return f"{int(delta.total_seconds() / seconds_per_hour)}h ago"
        return f"{int(delta.total_seconds() / 60)}m ago"
