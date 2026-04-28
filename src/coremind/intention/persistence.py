"""Intent persistence — JSONL store, ``cycle_id``-style idempotency.

Intents live in a JSONL file, one intent per line.  Writes are append-only
but :meth:`IntentStore.save` is idempotent on ``Intent.id``: ``_read_all``
de-duplicates by id so repeated saves keep the most recent version.

Phase 4 may migrate the intent store into the World Model as a first-class
table; until then, JSONL is the simplest durable option that matches the
approach already used for reasoning cycles.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import structlog
from pydantic import ValidationError

from coremind.errors import IntentionError
from coremind.intention.schemas import Intent, IntentStatus

log = structlog.get_logger(__name__)

type _IntentList = list[Intent]


class IntentStore:
    """JSONL-backed intent store.

    Args:
        store_path: Path to the JSONL file.  Created on first write.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = asyncio.Lock()

    async def save(self, intent: Intent) -> None:
        """Append ``intent`` to the journal.

        Idempotent by ``intent.id`` on read — later writes of the same id
        shadow earlier ones.

        Raises:
            IntentionError: On write failure.
        """
        line = intent.model_dump_json() + "\n"

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()

        async with self._lock:
            try:
                await asyncio.to_thread(_write)
            except OSError as exc:
                raise IntentionError(f"cannot append to intent store: {self._path}") from exc

    async def get(self, intent_id: str) -> Intent | None:
        """Return the latest version of ``intent_id`` or ``None``."""
        latest = await self._read_latest()
        return latest.get(intent_id)

    async def list(
        self,
        *,
        status: IntentStatus | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> _IntentList:
        """Return intents filtered by status/since, newest first."""
        latest = await self._read_latest()
        items = list(latest.values())
        if status is not None:
            items = [i for i in items if i.status == status]
        if since is not None:
            items = [i for i in items if i.created_at >= since]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[:limit]

    async def recent(
        self,
        *,
        since: datetime,
    ) -> _IntentList:
        """Return intents created since ``since`` (any status), newest first."""
        return await self.list(since=since, limit=10_000)

    async def _read_latest(self) -> dict[str, Intent]:
        """Replay the JSONL file and return the most recent Intent per id."""
        if not self._path.exists():
            return {}

        def _load() -> dict[str, Intent]:
            latest: dict[str, Intent] = {}
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                        intent = Intent.model_validate(raw)
                    except (json.JSONDecodeError, ValidationError):
                        log.warning("intention.store.malformed_line")
                        continue
                    latest[intent.id] = intent
            return latest

        async with self._lock:
            return await asyncio.to_thread(_load)
