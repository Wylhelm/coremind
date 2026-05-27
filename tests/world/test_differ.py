"""Unit tests for src/coremind/world/differ.py."""

from __future__ import annotations

from datetime import UTC, datetime

from coremind.world.differ import IGNORED_PROPERTIES, SnapshotDiff, SnapshotDiffer
from coremind.world.model import Entity, JsonValue, WorldSnapshot

_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def _make_entity(
    entity_type: str,
    display_name: str,
    **properties: JsonValue,
) -> Entity:
    return Entity(
        type=entity_type,
        display_name=display_name,
        created_at=_NOW,
        updated_at=_NOW,
        properties=properties,
        source_plugins=["test"],
    )


def _make_snapshot(entities: list[Entity]) -> WorldSnapshot:
    return WorldSnapshot(taken_at=_NOW, entities=entities)


class TestSnapshotDifferNoPrevious:
    def test_returns_all_as_added(self) -> None:
        differ = SnapshotDiffer()
        entities = [
            _make_entity("light", "bureau", state="on"),
            _make_entity("light", "salon", state="off"),
        ]
        diff = differ.diff(_make_snapshot(entities), None)

        assert len(diff.added) == 2
        assert diff.removed == []
        assert diff.changed == []
        assert diff.unchanged_count == 0
        assert diff.total_current == 2

    def test_has_changes_true(self) -> None:
        differ = SnapshotDiffer()
        entities = [_make_entity("light", "bureau", state="on")]
        diff = differ.diff(_make_snapshot(entities), None)

        assert diff.has_changes


class TestSnapshotDifferAdded:
    def test_detects_added_entity(self) -> None:
        differ = SnapshotDiffer()
        prev = _make_snapshot(
            [
                _make_entity("light", "bureau", state="on"),
                _make_entity("light", "salon", state="off"),
            ]
        )
        curr = _make_snapshot(
            [
                _make_entity("light", "bureau", state="on"),
                _make_entity("light", "salon", state="off"),
                _make_entity("light", "cuisine", state="on"),
            ]
        )

        diff = differ.diff(curr, prev)

        assert len(diff.added) == 1
        assert diff.added[0].display_name == "cuisine"
        assert diff.unchanged_count == 2


class TestSnapshotDifferRemoved:
    def test_detects_removed_entity(self) -> None:
        differ = SnapshotDiffer()
        prev = _make_snapshot(
            [
                _make_entity("light", "bureau", state="on"),
                _make_entity("light", "salon", state="off"),
                _make_entity("light", "cuisine", state="on"),
            ]
        )
        curr = _make_snapshot(
            [
                _make_entity("light", "bureau", state="on"),
                _make_entity("light", "salon", state="off"),
            ]
        )

        diff = differ.diff(curr, prev)

        assert len(diff.removed) == 1
        assert diff.removed[0].display_name == "cuisine"


class TestSnapshotDifferChanged:
    def test_detects_changed_properties(self) -> None:
        differ = SnapshotDiffer()
        prev = _make_snapshot([_make_entity("light", "bureau", state="off")])
        curr = _make_snapshot([_make_entity("light", "bureau", state="on")])

        diff = differ.diff(curr, prev)

        assert len(diff.changed) == 1
        old, new = diff.changed[0]
        assert old.properties["state"] == "off"
        assert new.properties["state"] == "on"
        assert diff.unchanged_count == 0

    def test_detects_added_property(self) -> None:
        differ = SnapshotDiffer()
        prev = _make_snapshot([_make_entity("light", "bureau", state="on")])
        curr = _make_snapshot(
            [
                _make_entity("light", "bureau", state="on", brightness=80),
            ]
        )

        diff = differ.diff(curr, prev)

        assert len(diff.changed) == 1

    def test_detects_removed_property(self) -> None:
        differ = SnapshotDiffer()
        prev = _make_snapshot(
            [
                _make_entity("light", "bureau", state="on", brightness=80),
            ]
        )
        curr = _make_snapshot([_make_entity("light", "bureau", state="on")])

        diff = differ.diff(curr, prev)

        assert len(diff.changed) == 1


class TestSnapshotDifferIgnoredProperties:
    def test_ignores_last_updated(self) -> None:
        differ = SnapshotDiffer()
        prev = _make_snapshot(
            [
                _make_entity("light", "bureau", state="on", last_updated="2026-05-27T10:00:00"),
            ]
        )
        curr = _make_snapshot(
            [
                _make_entity("light", "bureau", state="on", last_updated="2026-05-27T10:05:00"),
            ]
        )

        diff = differ.diff(curr, prev)

        assert diff.changed == []
        assert diff.unchanged_count == 1

    def test_ignores_last_changed(self) -> None:
        differ = SnapshotDiffer()
        prev = _make_snapshot(
            [
                _make_entity("sensor", "temp", value=21.5, last_changed="t1"),
            ]
        )
        curr = _make_snapshot(
            [
                _make_entity("sensor", "temp", value=21.5, last_changed="t2"),
            ]
        )

        diff = differ.diff(curr, prev)

        assert not diff.has_changes

    def test_ignores_last_seen(self) -> None:
        differ = SnapshotDiffer()
        prev = _make_snapshot(
            [
                _make_entity("device", "phone", state="home", last_seen="t1"),
            ]
        )
        curr = _make_snapshot(
            [
                _make_entity("device", "phone", state="home", last_seen="t2"),
            ]
        )

        diff = differ.diff(curr, prev)

        assert not diff.has_changes

    def test_still_detects_real_change_alongside_ignored(self) -> None:
        differ = SnapshotDiffer()
        prev = _make_snapshot(
            [
                _make_entity("light", "bureau", state="off", last_updated="t1"),
            ]
        )
        curr = _make_snapshot(
            [
                _make_entity("light", "bureau", state="on", last_updated="t2"),
            ]
        )

        diff = differ.diff(curr, prev)

        assert len(diff.changed) == 1

    def test_ignored_properties_frozenset_contents(self) -> None:
        assert "last_changed" in IGNORED_PROPERTIES
        assert "last_updated" in IGNORED_PROPERTIES
        assert "last_seen" in IGNORED_PROPERTIES


class TestSnapshotDiffModel:
    def test_has_changes_false_when_identical(self) -> None:
        differ = SnapshotDiffer()
        snapshot = _make_snapshot([_make_entity("light", "bureau", state="on")])

        diff = differ.diff(snapshot, snapshot)

        assert not diff.has_changes

    def test_change_summary_no_changes(self) -> None:
        diff = SnapshotDiff(unchanged_count=5, total_current=5)

        assert diff.change_summary == "5 unchanged"

    def test_change_summary_empty(self) -> None:
        diff = SnapshotDiff(total_current=0)

        assert diff.change_summary == "no changes"

    def test_change_summary_mixed(self) -> None:
        diff = SnapshotDiff(
            added=[_make_entity("light", "new", state="on")],
            changed=[
                (_make_entity("light", "x", state="off"), _make_entity("light", "x", state="on")),
            ],
            unchanged_count=10,
            total_current=12,
        )

        summary = diff.change_summary
        assert "1 added" in summary
        assert "1 changed" in summary
        assert "10 unchanged" in summary


class TestSnapshotDifferDeterminism:
    def test_output_order_is_deterministic(self) -> None:
        """Added/removed entities are sorted by key for stable output."""
        differ = SnapshotDiffer()
        prev = _make_snapshot([_make_entity("light", "a")])
        curr = _make_snapshot(
            [
                _make_entity("light", "c"),
                _make_entity("light", "b"),
            ]
        )

        diff = differ.diff(curr, prev)

        added_names = [e.display_name for e in diff.added]
        assert added_names == ["b", "c"]
