"""Adapter implementations of the L7 reflection loop ports for CLI use.

These adapters wrap the daemon's actual data sources (JSONL intent store,
JSONL cycle persister, hash-chained audit journal) so the
:class:`~coremind.reflection.loop.ReflectionLoop` can consume them through
its :class:`CycleSource`, :class:`IntentSource`, and :class:`ActionFeed`
protocols.

Each adapter bridges the protocol's ``list_*(since=, until=)`` signature to
the underlying store's own query API.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from pydantic import ValidationError

from coremind.action.journal import ActionJournal
from coremind.action.schemas import Action
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import Intent
from coremind.reasoning.persistence import JsonlCyclePersister
from coremind.reasoning.schemas import ReasoningOutput

log = structlog.get_logger(__name__)


class CliCycleSource:
    """Adapter that wraps :class:`JsonlCyclePersister` as a
    :class:`~coremind.reflection.loop.CycleSource`.

    The persister exposes ``list_cycles(since, limit)``; the protocol
    wants ``list_cycles(since, until)``.  This adapter filters the
    result to the ``[since, until)`` window.
    """

    def __init__(self, persister: JsonlCyclePersister) -> None:
        self._persister = persister

    async def list_cycles(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> list[ReasoningOutput]:
        """Return cycles whose ``timestamp`` lies in ``[since, until)``."""
        cycles = await self._persister.list_cycles(since=since, limit=10_000)
        return [c for c in cycles if c.timestamp < until]


class CliIntentSource:
    """Adapter that wraps :class:`IntentStore` as a
    :class:`~coremind.reflection.loop.IntentSource`.

    ``IntentStore.list(since=..., limit=...)`` returns newest-first; the
    protocol expects a plain list for the window.  We fetch a generous
    limit and truncate by ``until``.
    """

    def __init__(self, store: IntentStore) -> None:
        self._store = store

    async def list_intents(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> list[Intent]:
        """Return intents whose ``created_at`` lies in ``[since, until)``."""
        intents = await self._store.list(since=since, limit=10_000)
        return [i for i in intents if i.created_at < until]


class CliActionFeed:
    """Adapter that wraps :class:`ActionJournal` as a
    :class:`~coremind.reflection.loop.ActionFeed`.

    Walks the journal entries within the window and extracts
    :class:`Action` payloads from ``kind == "action"`` entries.
    """

    def __init__(self, journal: ActionJournal) -> None:
        self._journal = journal

    async def list_actions(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> list[Action]:
        """Return actions whose ``timestamp`` lies in ``[since, until)``."""
        entries = await self._journal.read_recent(since=since, limit=10_000)
        actions: list[Action] = []
        for entry in entries:
            if entry.kind != "action":
                continue
            try:
                action = Action.model_validate(entry.payload)
                if action.timestamp < until:
                    actions.append(action)
            except ValidationError:
                log.warning("reflect.invalid_action_entry", seq=entry.seq)
                continue
        return actions
