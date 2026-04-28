"""Episodic memory over the World Model event store (L3, episodic layer).

Provides a time-bucketed view of raw WorldEventRecords and a nightly
compaction step that persists LLM-generated summaries as Episode entities.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import structlog
from pydantic import BaseModel, Field

from coremind.errors import StoreError, SummarizerError
from coremind.world.model import EntityRef, WorldEventRecord

log = structlog.get_logger(__name__)

type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """Return the current UTC time.

    This is the default clock for EpisodicMemory.  Inject a deterministic
    alternative via the ``clock`` constructor argument in tests.
    """
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


class Episode(BaseModel):
    """A compacted summary of a time-bounded group of world events.

    Episodes are produced by grouping raw WorldEventRecords by entity and
    calendar day, then summarizing with the reasoning LLM. They represent
    "what happened" at a higher level of abstraction than raw events.
    """

    id: str
    entity: EntityRef
    window_start: datetime
    window_end: datetime
    summary: str
    event_count: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Port protocols (injectable dependencies)
# ---------------------------------------------------------------------------


class Summarizer(Protocol):
    """Port for the LLM-assisted episode summarizer.

    Satisfied by the reasoning LLM wrapper introduced in Phase 2.5.
    Use a stub implementation in unit tests.
    """

    async def summarize_events(
        self,
        entity: EntityRef,
        events: Sequence[WorldEventRecord],
    ) -> str:
        """Produce a one-paragraph natural-language summary for events.

        Args:
            entity: The entity the events relate to.
            events: The raw events to summarize.

        Returns:
            A one-paragraph summary string.

        Raises:
            SummarizerError: If the LLM backend fails to produce a summary.
        """
        ...


class EpisodicStorePort(Protocol):
    """Subset of the WorldStore interface required by EpisodicMemory.

    Implemented by the real WorldStore adapter and by test fakes.  Any
    class that provides these four coroutines satisfies the protocol.
    """

    async def events_in_window(
        self,
        after: datetime,
        before: datetime,
        entity: EntityRef | None = None,
        limit: int = 1000,
    ) -> list[WorldEventRecord]:
        """Return events with timestamp in the half-open interval (after, before].

        Args:
            after: Exclusive lower bound.
            before: Inclusive upper bound.
            entity: Optional entity filter; None means all entities.
            limit: Maximum number of events to return.

        Returns:
            Events ordered by timestamp ascending.
        """
        ...

    async def events_before(
        self,
        cutoff: datetime,
        entity: EntityRef | None = None,
        limit: int = 5000,
    ) -> list[WorldEventRecord]:
        """Return events with timestamp strictly before *cutoff*.

        Args:
            cutoff: Exclusive upper bound.
            entity: Optional entity filter; None means all entities.
            limit: Maximum number of events to return.

        Returns:
            Events ordered by timestamp ascending.
        """
        ...

    async def upsert_episode(self, episode: Episode) -> None:
        """Persist an Episode entity to the world store.

        Args:
            episode: The episode to persist.

        Raises:
            StoreError: If the persistence operation fails.
        """
        ...

    async def delete_events(self, event_ids: Sequence[str]) -> None:
        """Remove events by their IDs from the store.

        Args:
            event_ids: IDs of events to delete.

        Raises:
            StoreError: If the delete operation fails.
        """
        ...


# ---------------------------------------------------------------------------
# Core class
# ---------------------------------------------------------------------------


class EpisodicMemory:
    """View over the event store for time-bucketed episodic summaries.

    Reads raw WorldEventRecords from L2 and presents them as Episode objects
    bucketed by entity and calendar day.  The ``compact_older_than`` method
    persists LLM-generated summaries and optionally purges raw events.

    Args:
        store: World store adapter satisfying EpisodicStorePort.
        summarizer: LLM backend used to generate episode summaries during
            compaction.
        keep_raw_after_compact: When True (the default), raw events are
            retained after compaction.  Set to False to reclaim space.
    """

    def __init__(
        self,
        store: EpisodicStorePort,
        summarizer: Summarizer,
        *,
        keep_raw_after_compact: bool = True,
        clock: Clock = _utc_now,
    ) -> None:
        self._store = store
        self._summarizer = summarizer
        self._keep_raw = keep_raw_after_compact
        self._clock = clock

    async def recent(
        self,
        window: timedelta,
        entity: EntityRef | None = None,
    ) -> list[Episode]:
        """Return time-bucketed summaries of recent activity.

        Queries raw events in the given window and groups them by
        (entity, calendar day) without persisting any results.

        Args:
            window: Time window to look back from now.
            entity: If provided, restrict to events for this entity only.

        Returns:
            Episodes ordered by window_start ascending.
        """
        now = self._clock()
        after = now - window
        events = await self._store.events_in_window(
            after=after,
            before=now,
            entity=entity,
        )
        return _bucket_events(events, created_at=now)

    async def compact_older_than(self, age: timedelta) -> None:
        """Roll old raw events into persisted Episode summaries.

        For each (entity, calendar day) group older than *age*, calls the
        summarizer LLM to produce a one-paragraph description, then stores
        the result as an Episode via the store.  Optionally deletes the raw
        events after compaction (controlled by ``keep_raw_after_compact``).

        Failures from the summarizer for one group are logged and skipped;
        remaining groups are still processed.

        Args:
            age: Events older than ``now - age`` are eligible for compaction.
        """
        cutoff = self._clock() - age
        events = await self._store.events_before(cutoff=cutoff)
        if not events:
            log.info("episodic.compact.no_events", cutoff=cutoff.isoformat())
            return

        groups = _group_by_entity_day(events)
        log.info(
            "episodic.compact.start",
            groups=len(groups),
            cutoff=cutoff.isoformat(),
        )

        for (entity_type, entity_id, day), group_events in groups.items():
            entity = EntityRef(type=entity_type, id=entity_id)
            window_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
            window_end = window_start + timedelta(days=1)

            try:
                summary = await self._summarizer.summarize_events(
                    entity=entity,
                    events=group_events,
                )
            except SummarizerError as exc:
                log.warning(
                    "episodic.compact.summarize_failed",
                    entity=f"{entity_type}:{entity_id}",
                    day=str(day),
                    exc_info=exc,
                )
                continue

            episode_id = _stable_episode_id(entity, window_start)
            episode = Episode(
                id=episode_id,
                entity=entity,
                window_start=window_start,
                window_end=window_end,
                summary=summary,
                event_count=len(group_events),
                created_at=self._clock(),
            )

            try:
                await self._store.upsert_episode(episode)
            except StoreError as exc:
                log.error(
                    "episodic.compact.store_failed",
                    episode_id=episode_id,
                    exc_info=exc,
                )
                continue

            if not self._keep_raw:
                event_ids = [e.id for e in group_events]
                try:
                    await self._store.delete_events(event_ids)
                except StoreError as exc:
                    log.warning(
                        "episodic.compact.delete_failed",
                        count=len(event_ids),
                        exc_info=exc,
                    )

            log.info(
                "episodic.compact.episode_stored",
                episode_id=episode_id,
                entity=f"{entity_type}:{entity_id}",
                event_count=len(group_events),
            )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

type _GroupKey = tuple[str, str, date]


def _group_by_entity_day(
    events: list[WorldEventRecord],
) -> dict[_GroupKey, list[WorldEventRecord]]:
    """Group events by (entity_type, entity_id, calendar_day) in UTC.

    Args:
        events: Raw world event records to group.

    Returns:
        Dict keyed by (entity_type, entity_id, date).
    """
    groups: dict[_GroupKey, list[WorldEventRecord]] = defaultdict(list)
    for event in events:
        day = event.timestamp.astimezone(UTC).date()
        key: _GroupKey = (event.entity.type, event.entity.id, day)
        groups[key].append(event)
    return dict(groups)


def _bucket_events(
    events: list[WorldEventRecord],
    *,
    created_at: datetime,
) -> list[Episode]:
    """Convert a flat list of events into time-bucketed Episode objects.

    Episodes produced here are ephemeral (not persisted); they are view
    objects for the ``EpisodicMemory.recent`` method.

    Args:
        events: Raw world event records.
        created_at: Timestamp to stamp on the produced episodes.

    Returns:
        Episodes sorted by window_start ascending.
    """
    groups = _group_by_entity_day(events)
    episodes: list[Episode] = []

    for (entity_type, entity_id, day), group_events in groups.items():
        window_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        window_end = window_start + timedelta(days=1)
        entity = EntityRef(type=entity_type, id=entity_id)
        attributes = sorted({e.attribute for e in group_events})
        summary = (
            f"Observed {len(group_events)} event(s) for "
            f"{entity_type}:{entity_id} — "
            f"attributes: {', '.join(attributes)}"
        )
        episodes.append(
            Episode(
                id=_stable_episode_id(entity, window_start),
                entity=entity,
                window_start=window_start,
                window_end=window_end,
                summary=summary,
                event_count=len(group_events),
                created_at=created_at,
            )
        )

    return sorted(episodes, key=lambda e: e.window_start)


def _stable_episode_id(entity: EntityRef, window_start: datetime) -> str:
    """Generate a deterministic episode ID from entity and window start.

    Args:
        entity: The entity the episode covers.
        window_start: The start of the compacted window (UTC midnight).

    Returns:
        A 16-character hex string derived from SHA-256.
    """
    raw = f"{entity.type}:{entity.id}:{window_start.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
