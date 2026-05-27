"""In-memory store implementations for the meta-cognition layer.

These are lightweight implementations suitable for development and testing.
A persistent SurrealDB-backed adapter can be added separately.
"""

from __future__ import annotations

from typing import Any

import structlog

from coremind.meta.schemas import AdjustmentRecord, MetaObservation, ProposedAdjustment

log = structlog.get_logger(__name__)


class InMemoryConfigStore:
    """Dict-backed async config store for meta-loop adjustments."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(initial) if initial else {}

    async def get(self, dotted_path: str) -> Any:
        """Return the value at *dotted_path*, or raise KeyError."""
        return self._data[dotted_path]

    async def set(self, dotted_path: str, value: Any) -> None:
        """Set *value* at *dotted_path*."""
        self._data[dotted_path] = value
        log.debug("config_store.set", path=dotted_path, value=value)


class InMemoryMetaStore:
    """List-backed async store for adjustment records and observations."""

    def __init__(self) -> None:
        self._adjustments: dict[str, AdjustmentRecord] = {}
        self._observations: list[MetaObservation] = []

    async def save_adjustment(self, record: AdjustmentRecord) -> None:
        """Persist an applied adjustment record."""
        self._adjustments[record.adjustment_id] = record

    async def get_adjustment(self, adjustment_id: str) -> AdjustmentRecord | None:
        """Return the adjustment record with *adjustment_id*, or None."""
        return self._adjustments.get(adjustment_id)

    async def update_adjustment(self, record: AdjustmentRecord) -> None:
        """Update an existing adjustment record."""
        self._adjustments[record.adjustment_id] = record

    async def save_observations(self, observations: list[MetaObservation]) -> None:
        """Persist a batch of observations."""
        self._observations.extend(observations)


class InMemoryApprovalQueue:
    """Simple in-memory queue for meta-loop proposals requiring approval."""

    def __init__(self) -> None:
        self._queue: list[ProposedAdjustment] = []

    async def add(self, proposal: ProposedAdjustment) -> None:
        """Enqueue *proposal* for user approval."""
        self._queue.append(proposal)

    @property
    def pending(self) -> list[ProposedAdjustment]:
        """Return all pending proposals (for testing/inspection)."""
        return list(self._queue)


class LoggingMetaEventBus:
    """Meta event bus that logs events via structlog (no-op delivery)."""

    def __init__(self) -> None:
        self._published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Log and record the event."""
        self._published.append((topic, payload))
        log.info("meta.event", topic=topic, **payload)

    @property
    def events(self) -> list[tuple[str, dict[str, Any]]]:
        """Return all published events (for testing/inspection)."""
        return list(self._published)
