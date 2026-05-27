# Phase 3A — Snapshot Differ

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_3_EMBEDDING_WORLD.md](PHASE_3_EMBEDDING_WORLD.md)
**Prerequisites:** None
**Estimated effort:** 1–2 hours

---

## 1. Goal

Create `SnapshotDiffer` — a pure-logic component that computes the difference between two `WorldSnapshot` objects. This is the foundation for embedding-world: only changed entities go to the LLM.

No external dependencies. No network calls. Fully testable in isolation.

---

## 2. Deliverables

| File | Purpose |
|---|---|
| `src/coremind/world/differ.py` | `SnapshotDiff` model + `SnapshotDiffer` class |
| `tests/world/test_differ.py` | Unit tests |

---

## 3. Data Model

```python
from pydantic import BaseModel, Field

IGNORED_ATTRS: frozenset[str] = frozenset({"last_changed", "last_updated", "last_seen"})
"""Attributes that change every cycle but carry no semantic meaning for diffing."""


class SnapshotDiff(BaseModel):
    """Represents the difference between two WorldSnapshots."""

    added: list[Entity] = Field(default_factory=list)
    removed: list[Entity] = Field(default_factory=list)
    changed: list[tuple[Entity, Entity]] = Field(default_factory=list)  # (old, new)
    unchanged_count: int = 0
    total_current: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    @property
    def change_summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        if self.changed:
            parts.append(f"{len(self.changed)} changed")
        if self.unchanged_count:
            parts.append(f"{self.unchanged_count} unchanged")
        return ", ".join(parts) if parts else "no changes"
```

---

## 4. Implementation

```python
class SnapshotDiffer:
    """Computes diffs between WorldSnapshots."""

    def diff(
        self,
        current: WorldSnapshot,
        previous: WorldSnapshot | None,
    ) -> SnapshotDiff:
        """Compute the diff. If previous is None, all entities are 'added'."""
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
        return f"{entity.entity_type}:{entity.entity_id}"

    def _entities_differ(self, old: Entity, new: Entity) -> bool:
        old_attrs = {k: v for k, v in old.attributes.items() if k not in IGNORED_ATTRS}
        new_attrs = {k: v for k, v in new.attributes.items() if k not in IGNORED_ATTRS}
        return old_attrs != new_attrs
```

---

## 5. Tests

```python
# tests/world/test_differ.py

def test_diff_no_previous_returns_all_as_added():
    differ = SnapshotDiffer()
    curr = make_snapshot(["light.bureau", "light.salon"])
    diff = differ.diff(curr, None)
    assert len(diff.added) == 2
    assert diff.unchanged_count == 0
    assert diff.total_current == 2

def test_diff_added_entity():
    differ = SnapshotDiffer()
    prev = make_snapshot(["light.bureau", "light.salon"])
    curr = make_snapshot(["light.bureau", "light.salon", "light.cuisine"])
    diff = differ.diff(curr, prev)
    assert len(diff.added) == 1
    assert diff.added[0].entity_id == "light.cuisine"
    assert diff.unchanged_count == 2

def test_diff_removed_entity():
    differ = SnapshotDiffer()
    prev = make_snapshot(["light.bureau", "light.salon", "light.cuisine"])
    curr = make_snapshot(["light.bureau", "light.salon"])
    diff = differ.diff(curr, prev)
    assert len(diff.removed) == 1
    assert diff.removed[0].entity_id == "light.cuisine"

def test_diff_changed_attributes():
    differ = SnapshotDiffer()
    prev_e = make_entity("light.bureau", state="off")
    curr_e = make_entity("light.bureau", state="on")
    diff = differ.diff(make_snapshot_from([curr_e]), make_snapshot_from([prev_e]))
    assert len(diff.changed) == 1
    old, new = diff.changed[0]
    assert old.attributes["state"] == "off"
    assert new.attributes["state"] == "on"

def test_diff_ignores_timestamp_attrs():
    """last_changed, last_updated, last_seen should not count as changes."""
    differ = SnapshotDiffer()
    prev_e = make_entity("light.bureau", state="on", last_updated="2026-05-26T10:00:00")
    curr_e = make_entity("light.bureau", state="on", last_updated="2026-05-26T10:05:00")
    diff = differ.diff(make_snapshot_from([curr_e]), make_snapshot_from([prev_e]))
    assert len(diff.changed) == 0
    assert diff.unchanged_count == 1

def test_diff_has_changes_property():
    differ = SnapshotDiffer()
    s = make_snapshot(["light.bureau"])
    diff = differ.diff(s, s)
    assert not diff.has_changes

def test_diff_change_summary():
    diff = SnapshotDiff(added=[mock_entity()], changed=[(mock_entity(), mock_entity())], unchanged_count=10, total_current=12)
    assert "1 added" in diff.change_summary
    assert "1 changed" in diff.change_summary
    assert "10 unchanged" in diff.change_summary
```

---

## 6. Integration Notes

- Import `Entity` and `WorldSnapshot` from wherever they currently live in `src/coremind/world/`.
- If these types don't exist yet, define minimal Protocol-compatible stubs and note them for later refactoring.
- `SnapshotDiffer` is stateless — a single instance can be reused across cycles.
- `IGNORED_ATTRS` is a module-level frozenset; keep it extensible.

---

## 7. Success Criteria

- [ ] `SnapshotDiff` model validates correctly
- [ ] `SnapshotDiffer.diff()` handles: no previous, added, removed, changed, unchanged
- [ ] Timestamp attributes are excluded from change detection
- [ ] `change_summary` property produces readable output
- [ ] All tests pass under `pytest tests/world/test_differ.py`
- [ ] `mypy --strict` passes
- [ ] `ruff check` passes
