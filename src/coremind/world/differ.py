"""Snapshot diffing for the Embedding World (Phase 3A).

Computes the difference between two WorldSnapshots, identifying added,
removed, and changed entities while ignoring noisy timestamp-only updates.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from coremind.world.model import Entity, WorldSnapshot

IGNORED_PROPERTIES: frozenset[str] = frozenset({"last_changed", "last_updated", "last_seen"})
"""Properties that change every cycle but carry no semantic meaning for diffing."""


class SnapshotDiff(BaseModel):
    """Represents the difference between two WorldSnapshots."""

    added: list[Entity] = Field(default_factory=list)
    removed: list[Entity] = Field(default_factory=list)
    changed: list[tuple[Entity, Entity]] = Field(default_factory=list)
    unchanged_count: int = 0
    total_current: int = 0

    @property
    def has_changes(self) -> bool:
        """Return True if any entities were added, removed, or changed."""
        return bool(self.added or self.removed or self.changed)

    @property
    def change_summary(self) -> str:
        """Return a human-readable summary of the diff."""
        parts: list[str] = []
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        if self.changed:
            parts.append(f"{len(self.changed)} changed")
        if self.unchanged_count:
            parts.append(f"{self.unchanged_count} unchanged")
        return ", ".join(parts) if parts else "no changes"


class SnapshotDiffer:
    """Computes diffs between WorldSnapshots.

    Stateless — a single instance can be reused across reasoning cycles.
    """

    def diff(
        self,
        current: WorldSnapshot,
        previous: WorldSnapshot | None,
    ) -> SnapshotDiff:
        """Compute the diff between current and previous snapshots.

        If previous is None, all current entities are reported as added.
        """
        if previous is None:
            return SnapshotDiff(
                added=list(current.entities),
                total_current=len(current.entities),
            )

        prev_by_key = {self._key(e): e for e in previous.entities}
        curr_by_key = {self._key(e): e for e in current.entities}

        prev_keys = set(prev_by_key.keys())
        curr_keys = set(curr_by_key.keys())

        added = [curr_by_key[k] for k in sorted(curr_keys - prev_keys)]
        removed = [prev_by_key[k] for k in sorted(prev_keys - curr_keys)]

        changed: list[tuple[Entity, Entity]] = []
        unchanged = 0
        for k in sorted(prev_keys & curr_keys):
            old, new = prev_by_key[k], curr_by_key[k]
            if self._entities_differ(old, new):
                changed.append((old, new))
            else:
                unchanged += 1

        return SnapshotDiff(
            added=added,
            removed=removed,
            changed=changed,
            unchanged_count=unchanged,
            total_current=len(current.entities),
        )

    def _key(self, entity: Entity) -> str:
        """Compute the composite key for an entity."""
        return f"{entity.type}:{entity.display_name}"

    def _entities_differ(self, old: Entity, new: Entity) -> bool:
        """Return True if entities differ in semantically meaningful properties."""
        old_props = {k: v for k, v in old.properties.items() if k not in IGNORED_PROPERTIES}
        new_props = {k: v for k, v in new.properties.items() if k not in IGNORED_PROPERTIES}
        return old_props != new_props
