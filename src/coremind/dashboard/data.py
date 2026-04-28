"""Data-source ports consumed by the dashboard handlers.

Each port is a :class:`~typing.Protocol` so any duck-typed object that
matches the signature can be passed in.  This keeps the dashboard decoupled
from the concrete SurrealDB / JSONL stores and makes it trivial to test.

All ports are *read-only*: the dashboard never persists state through them.
The only state-changing path is approval submission, which goes through the
:class:`~coremind.notify.adapters.dashboard.DashboardNotificationPort` —
itself a Phase 3 channel adapter that signs and journals each decision.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from coremind.action.schemas import Action
from coremind.intention.schemas import Intent, IntentStatus
from coremind.notify.adapters.dashboard import DashboardNotificationPort
from coremind.reasoning.schemas import ReasoningOutput
from coremind.reflection.schemas import ReflectionReport
from coremind.world.model import WorldEventRecord, WorldSnapshot


class WorldSource(Protocol):
    """Read-only port over the World Model (L2)."""

    async def snapshot(self, at: datetime | None = None) -> WorldSnapshot:
        """Return a point-in-time snapshot of entities and relationships."""

    async def recent_events(
        self,
        since: datetime,
        limit: int = 500,
    ) -> list[WorldEventRecord]:
        """Return events strictly after ``since``."""


class CycleSource(Protocol):
    """Read-only port over reasoning cycles (L4)."""

    async def list_cycles(
        self,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[ReasoningOutput]:
        """Return cycles newest-first."""

    async def get_cycle(self, cycle_id: str) -> ReasoningOutput | None:
        """Return the cycle with ``cycle_id`` or ``None`` if unknown."""


class IntentSource(Protocol):
    """Read-only port over the intent store (L5)."""

    async def list(
        self,
        *,
        status: IntentStatus | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[Intent]:
        """Return intents filtered by status/since, newest-first."""


class JournalEntryView(Protocol):
    """Read-only view of a single audit-journal entry.

    Defined as a :class:`Protocol` so the dashboard does not depend on the
    concrete :class:`coremind.action.journal._JournalEntry` model and so
    ``mypy --strict`` covers the audit-page render path end-to-end.
    """

    @property
    def seq(self) -> int:
        """Monotonically increasing sequence number, starting at 1."""

    @property
    def kind(self) -> str:
        """Entry kind — typically ``"action"`` or ``"meta"``."""

    @property
    def timestamp(self) -> datetime:
        """When the entry was written, in UTC."""

    @property
    def payload(self) -> Mapping[str, Any]:
        """Structured payload; semantics depend on :attr:`kind`."""


class JournalSource(Protocol):
    """Read-only port over the action journal (L6)."""

    async def read_recent(
        self,
        *,
        limit: int = 100,
        since: datetime | None = None,
    ) -> Sequence[JournalEntryView]:
        """Return entries newest-first, optionally bounded by ``since``.

        Replaces an earlier ``read_all() -> list[object]`` shape: the
        bounded variant keeps page renders O(limit) regardless of journal
        size and lets ``mypy --strict`` see typed fields on each entry.
        """

    async def find_action(self, action_id: str) -> Action | None:
        """Return a single action by id or ``None``."""


class StoredReflectionReport(BaseModel):
    """A reflection report archived for the dashboard.

    The L7 loop produces :class:`ReflectionReport` instances; this wrapper
    pins the storage timestamp the dashboard uses for ordering.
    """

    model_config = ConfigDict(frozen=True)

    stored_at: datetime
    report: ReflectionReport


class ReflectionReportSource(Protocol):
    """Read-only port over archived reflection reports (L7)."""

    async def list_reports(self, *, limit: int = 20) -> list[StoredReflectionReport]:
        """Return reports newest-first."""


class EventSubscriber(Protocol):
    """Optional live event source — typically the in-process EventBus."""

    def subscribe(self) -> AsyncIterator[WorldEventRecord]:
        """Yield events as they are published."""


@dataclass(frozen=True)
class DashboardDataSources:
    """Container for every read port the dashboard may consume.

    All fields are optional: when a source is absent, the corresponding
    page renders an empty / "not configured" state instead of failing.
    """

    world: WorldSource | None = None
    cycles: CycleSource | None = None
    intents: IntentSource | None = None
    journal: JournalSource | None = None
    reflection: ReflectionReportSource | None = None
    notifications: DashboardNotificationPort | None = None
    events: EventSubscriber | None = None
