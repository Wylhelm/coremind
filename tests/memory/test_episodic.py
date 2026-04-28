"""Tests for coremind.memory.episodic.

All tests are unit tests (no I/O, no containers).  External dependencies
(WorldStore, LLM summarizer) are replaced with in-process fakes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from coremind.errors import StoreError, SummarizerError
from coremind.memory.episodic import (
    Episode,
    EpisodicMemory,
)
from coremind.world.model import EntityRef, WorldEventRecord

# Fixed anchor used across all tests to avoid wall-clock dependence.
_FIXED_NOW = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    entity_type: str = "sensor",
    entity_id: str = "temp_1",
    attribute: str = "temperature",
    timestamp: datetime | None = None,
    event_id: str | None = None,
) -> WorldEventRecord:
    """Build a minimal WorldEventRecord for use in tests."""
    if timestamp is None:
        timestamp = datetime.now(UTC)
    if event_id is None:
        event_id = f"{entity_type}_{entity_id}_{attribute}_{timestamp.isoformat()}"
    return WorldEventRecord(
        id=event_id,
        timestamp=timestamp,
        source="test_plugin",
        source_version="1.0.0",
        signature=None,
        entity=EntityRef(type=entity_type, id=entity_id),
        attribute=attribute,
        value=22.5,
        confidence=1.0,
    )


class _FakeStore:
    """In-process fake satisfying EpisodicStorePort."""

    def __init__(
        self,
        events: list[WorldEventRecord] | None = None,
        *,
        fail_upsert: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self._events: list[WorldEventRecord] = list(events or [])
        self.upserted_episodes: list[Episode] = []
        self.deleted_event_ids: list[str] = []
        self._fail_upsert = fail_upsert
        self._fail_delete = fail_delete

    async def events_in_window(
        self,
        after: datetime,
        before: datetime,
        entity: EntityRef | None = None,
        limit: int = 1000,
    ) -> list[WorldEventRecord]:
        result = [
            e
            for e in self._events
            if after < e.timestamp <= before
            and (entity is None or (e.entity.type == entity.type and e.entity.id == entity.id))
        ]
        return sorted(result, key=lambda e: e.timestamp)[:limit]

    async def events_before(
        self,
        cutoff: datetime,
        entity: EntityRef | None = None,
        limit: int = 5000,
    ) -> list[WorldEventRecord]:
        result = [
            e
            for e in self._events
            if e.timestamp < cutoff
            and (entity is None or (e.entity.type == entity.type and e.entity.id == entity.id))
        ]
        return sorted(result, key=lambda e: e.timestamp)[:limit]

    async def upsert_episode(self, episode: Episode) -> None:
        if self._fail_upsert:
            raise StoreError("upsert failed")
        self.upserted_episodes.append(episode)

    async def delete_events(self, event_ids: Sequence[str]) -> None:
        if self._fail_delete:
            raise StoreError("delete failed")
        ids = set(event_ids)
        self._events = [e for e in self._events if e.id not in ids]
        self.deleted_event_ids.extend(event_ids)


class _FakeSummarizer:
    """Stub summarizer that returns a canned response."""

    def __init__(self, response: str = "Summary text.") -> None:
        self._response = response
        self.calls: list[tuple[EntityRef, list[WorldEventRecord]]] = []

    async def summarize_events(
        self,
        entity: EntityRef,
        events: Sequence[WorldEventRecord],
    ) -> str:
        self.calls.append((entity, list(events)))
        return self._response


@pytest.mark.asyncio
async def test_recent_returns_episodes_within_window() -> None:
    in_window = _make_event(event_id="e1", timestamp=_FIXED_NOW - timedelta(hours=2))
    out_of_window = _make_event(event_id="e2", timestamp=_FIXED_NOW - timedelta(hours=48))
    store = _FakeStore([in_window, out_of_window])

    mem = EpisodicMemory(store=store, summarizer=_FakeSummarizer(), clock=lambda: _FIXED_NOW)
    episodes = await mem.recent(window=timedelta(hours=24))

    assert len(episodes) == 1
    assert episodes[0].event_count == 1


@pytest.mark.asyncio
async def test_recent_empty_store_returns_empty_list() -> None:
    mem = EpisodicMemory(store=_FakeStore(), summarizer=_FakeSummarizer(), clock=lambda: _FIXED_NOW)

    episodes = await mem.recent(window=timedelta(hours=24))

    assert episodes == []


@pytest.mark.asyncio
async def test_recent_filters_by_entity() -> None:
    t = _FIXED_NOW - timedelta(hours=1)
    events = [
        _make_event(entity_id="a", event_id="e1", timestamp=t),
        _make_event(entity_id="b", event_id="e2", timestamp=t),
    ]
    store = _FakeStore(events)
    mem = EpisodicMemory(store=store, summarizer=_FakeSummarizer(), clock=lambda: _FIXED_NOW)

    target = EntityRef(type="sensor", id="a")
    episodes = await mem.recent(window=timedelta(hours=24), entity=target)

    assert len(episodes) == 1
    assert episodes[0].entity.id == "a"


@pytest.mark.asyncio
async def test_recent_does_not_persist_to_store() -> None:
    store = _FakeStore([_make_event(event_id="e1", timestamp=_FIXED_NOW - timedelta(hours=1))])
    mem = EpisodicMemory(store=store, summarizer=_FakeSummarizer(), clock=lambda: _FIXED_NOW)

    await mem.recent(window=timedelta(hours=24))

    assert store.upserted_episodes == []


@pytest.mark.asyncio
async def test_recent_multiple_entities_same_day_returns_one_episode_each() -> None:
    t = _FIXED_NOW - timedelta(hours=1)
    events = [
        _make_event(entity_id="x", event_id="e1", timestamp=t),
        _make_event(entity_id="y", event_id="e2", timestamp=t),
    ]
    mem = EpisodicMemory(
        store=_FakeStore(events), summarizer=_FakeSummarizer(), clock=lambda: _FIXED_NOW
    )

    episodes = await mem.recent(window=timedelta(hours=24))

    assert len(episodes) == 2


@pytest.mark.asyncio
async def test_recent_episodes_sorted_by_window_start() -> None:
    # Two events on different calendar days, both within a 3-day window
    t1 = _FIXED_NOW - timedelta(days=2, hours=2)
    t2 = _FIXED_NOW - timedelta(days=1, hours=2)
    events = [
        _make_event(event_id="e2", timestamp=t2),
        _make_event(event_id="e1", timestamp=t1),
    ]
    store = _FakeStore(events)
    mem = EpisodicMemory(store=store, summarizer=_FakeSummarizer(), clock=lambda: _FIXED_NOW)

    episodes = await mem.recent(window=timedelta(days=3))

    assert len(episodes) == 2
    assert episodes[0].window_start < episodes[1].window_start


@pytest.mark.asyncio
async def test_recent_episode_summary_contains_attribute_names() -> None:
    t = _FIXED_NOW - timedelta(hours=1)
    events = [
        _make_event(attribute="temperature", event_id="e1", timestamp=t),
        _make_event(attribute="humidity", event_id="e2", timestamp=t),
    ]
    mem = EpisodicMemory(
        store=_FakeStore(events), summarizer=_FakeSummarizer(), clock=lambda: _FIXED_NOW
    )

    episodes = await mem.recent(window=timedelta(hours=24))

    assert len(episodes) == 1
    assert "temperature" in episodes[0].summary
    assert "humidity" in episodes[0].summary


@pytest.mark.asyncio
async def test_compact_produces_one_episode_per_entity_day() -> None:
    old = datetime(2024, 1, 5, 10, 0, tzinfo=UTC)
    events = [
        _make_event(entity_id="x", event_id="e1", timestamp=old),
        _make_event(entity_id="x", event_id="e2", timestamp=old.replace(hour=11)),
        _make_event(entity_id="y", event_id="e3", timestamp=old),
    ]
    store = _FakeStore(events)
    mem = EpisodicMemory(
        store=store, summarizer=_FakeSummarizer("Summarized."), clock=lambda: _FIXED_NOW
    )

    await mem.compact_older_than(age=timedelta(days=1))

    assert len(store.upserted_episodes) == 2


@pytest.mark.asyncio
async def test_compact_episode_has_correct_entity_day_window() -> None:
    old = datetime(2024, 1, 5, 14, 30, tzinfo=UTC)
    store = _FakeStore([_make_event(event_id="e1", timestamp=old)])
    mem = EpisodicMemory(
        store=store, summarizer=_FakeSummarizer("Summary."), clock=lambda: _FIXED_NOW
    )

    await mem.compact_older_than(age=timedelta(days=1))

    ep = store.upserted_episodes[0]
    assert ep.window_start == datetime(2024, 1, 5, 0, 0, tzinfo=UTC)
    assert ep.window_end == datetime(2024, 1, 6, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_compact_episode_event_count_matches_source_events() -> None:
    old = datetime(2024, 1, 5, 10, 0, tzinfo=UTC)
    events = [
        _make_event(event_id="e1", timestamp=old),
        _make_event(event_id="e2", timestamp=old.replace(hour=11)),
        _make_event(event_id="e3", timestamp=old.replace(hour=12)),
    ]
    store = _FakeStore(events)
    mem = EpisodicMemory(
        store=store, summarizer=_FakeSummarizer("Summary."), clock=lambda: _FIXED_NOW
    )

    await mem.compact_older_than(age=timedelta(days=1))

    assert store.upserted_episodes[0].event_count == 3


@pytest.mark.asyncio
async def test_compact_stores_llm_summary_in_episode() -> None:
    old = datetime(2024, 1, 5, 10, 0, tzinfo=UTC)
    store = _FakeStore([_make_event(event_id="e1", timestamp=old)])
    summarizer = _FakeSummarizer("The sensor was active all morning.")
    mem = EpisodicMemory(store=store, summarizer=summarizer, clock=lambda: _FIXED_NOW)

    await mem.compact_older_than(age=timedelta(days=1))

    assert store.upserted_episodes[0].summary == "The sensor was active all morning."


@pytest.mark.asyncio
async def test_compact_keeps_raw_events_by_default() -> None:
    old = datetime(2024, 1, 5, 10, 0, tzinfo=UTC)
    store = _FakeStore([_make_event(event_id="e1", timestamp=old)])
    mem = EpisodicMemory(store=store, summarizer=_FakeSummarizer(), clock=lambda: _FIXED_NOW)

    await mem.compact_older_than(age=timedelta(days=1))

    assert store.deleted_event_ids == []


@pytest.mark.asyncio
async def test_compact_deletes_raw_events_when_keep_raw_is_false() -> None:
    old = datetime(2024, 1, 5, 10, 0, tzinfo=UTC)
    events = [
        _make_event(event_id="e1", timestamp=old),
        _make_event(event_id="e2", timestamp=old.replace(hour=11)),
    ]
    store = _FakeStore(events)
    mem = EpisodicMemory(
        store=store,
        summarizer=_FakeSummarizer(),
        keep_raw_after_compact=False,
        clock=lambda: _FIXED_NOW,
    )

    await mem.compact_older_than(age=timedelta(days=1))

    assert set(store.deleted_event_ids) == {"e1", "e2"}


@pytest.mark.asyncio
async def test_compact_no_events_is_noop() -> None:
    store = _FakeStore([])
    summarizer = _FakeSummarizer()
    mem = EpisodicMemory(store=store, summarizer=summarizer, clock=lambda: _FIXED_NOW)

    await mem.compact_older_than(age=timedelta(days=1))

    assert store.upserted_episodes == []
    assert summarizer.calls == []


@pytest.mark.asyncio
async def test_compact_summarizer_failure_skips_group_continues_others() -> None:
    old = datetime(2024, 1, 5, 10, 0, tzinfo=UTC)
    events = [
        _make_event(entity_id="fail", event_id="e1", timestamp=old),
        _make_event(entity_id="ok", event_id="e2", timestamp=old),
    ]
    store = _FakeStore(events)

    class _PartialFailSummarizer:
        async def summarize_events(
            self,
            entity: EntityRef,
            events: Sequence[WorldEventRecord],
        ) -> str:
            if entity.id == "fail":
                raise SummarizerError("LLM error")
            return "Summary ok."

    mem = EpisodicMemory(store=store, summarizer=_PartialFailSummarizer(), clock=lambda: _FIXED_NOW)

    await mem.compact_older_than(age=timedelta(days=1))

    assert len(store.upserted_episodes) == 1
    assert store.upserted_episodes[0].entity.id == "ok"


@pytest.mark.asyncio
async def test_compact_episode_id_is_stable_across_calls() -> None:
    old = datetime(2024, 1, 5, 10, 0, tzinfo=UTC)
    store_a = _FakeStore([_make_event(event_id="e1", timestamp=old)])
    store_b = _FakeStore([_make_event(event_id="e1", timestamp=old)])

    mem_a = EpisodicMemory(
        store=store_a, summarizer=_FakeSummarizer("S1"), clock=lambda: _FIXED_NOW
    )
    mem_b = EpisodicMemory(
        store=store_b, summarizer=_FakeSummarizer("S2"), clock=lambda: _FIXED_NOW
    )

    await mem_a.compact_older_than(age=timedelta(days=1))
    await mem_b.compact_older_than(age=timedelta(days=1))

    assert store_a.upserted_episodes[0].id == store_b.upserted_episodes[0].id


@pytest.mark.asyncio
async def test_compact_does_not_include_recent_events() -> None:
    recent_event = _make_event(event_id="e1", timestamp=_FIXED_NOW - timedelta(hours=1))
    store = _FakeStore([recent_event])
    summarizer = _FakeSummarizer()
    mem = EpisodicMemory(store=store, summarizer=summarizer, clock=lambda: _FIXED_NOW)

    # compact with age=7 days — the 1-hour-old event is not old enough
    await mem.compact_older_than(age=timedelta(days=7))

    assert store.upserted_episodes == []
    assert summarizer.calls == []


@pytest.mark.asyncio
async def test_compact_upsert_failure_is_logged_and_continues() -> None:
    old = datetime(2024, 1, 5, 10, 0, tzinfo=UTC)
    store = _FakeStore([_make_event(event_id="e1", timestamp=old)], fail_upsert=True)
    mem = EpisodicMemory(store=store, summarizer=_FakeSummarizer("S."), clock=lambda: _FIXED_NOW)

    # Must not raise even though upsert fails.
    await mem.compact_older_than(age=timedelta(days=1))

    assert store.upserted_episodes == []


@pytest.mark.asyncio
async def test_compact_delete_failure_is_logged_and_continues() -> None:
    old = datetime(2024, 1, 5, 10, 0, tzinfo=UTC)
    store = _FakeStore([_make_event(event_id="e1", timestamp=old)], fail_delete=True)
    mem = EpisodicMemory(
        store=store,
        summarizer=_FakeSummarizer("S."),
        keep_raw_after_compact=False,
        clock=lambda: _FIXED_NOW,
    )

    # Must not raise even though delete fails; episode must still be stored.
    await mem.compact_older_than(age=timedelta(days=1))

    assert len(store.upserted_episodes) == 1
