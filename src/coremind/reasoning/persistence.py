"""Persistence adapter for reasoning cycles.

Reasoning cycles are persisted as JSONL for Phase 2: each cycle is a single
line, idempotent by ``cycle_id``.  This avoids coupling cycle storage to
the SurrealDB schema while remaining fully queryable from the CLI.

Phase 3 may migrate cycles into the World Model as first-class entities.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import structlog
from pydantic import ValidationError

from coremind.errors import ReasoningError
from coremind.reasoning.schemas import ReasoningOutput

log = structlog.get_logger(__name__)


class JsonlCyclePersister:
    """JSONL-backed reasoning-cycle persister.

    Cycles are appended to a single file in insertion order.  Queries scan
    the file linearly — acceptable at Phase 2 scale (one cycle every 15
    minutes → ≤100 per day).

    Args:
        store_path: Path to the JSONL file.  Created on first write.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = asyncio.Lock()

    async def persist_cycle(self, cycle: ReasoningOutput) -> None:
        """Append or replace a cycle by ``cycle_id``.

        Implementation writes a new line; ``list_cycles`` de-duplicates on
        read so repeated writes of the same id keep the most recent entry.

        Args:
            cycle: The cycle to persist.

        Raises:
            ReasoningError: If the journal cannot be written.
        """
        line = cycle.model_dump_json() + "\n"

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()

        async with self._lock:
            try:
                await asyncio.to_thread(_write)
            except OSError as exc:
                raise ReasoningError(f"cannot append to reasoning journal: {self._path}") from exc

    async def _read_all(self) -> list[ReasoningOutput]:
        """Read every cycle from the journal, deduplicating by id.

        Later entries for the same ``cycle_id`` override earlier ones.
        Malformed lines are skipped with a warning.

        Returns:
            Cycles in chronological order (by timestamp ascending).
        """
        if not self._path.exists():
            return []

        def _load() -> list[ReasoningOutput]:
            latest: dict[str, ReasoningOutput] = {}
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                        cycle = ReasoningOutput.model_validate(raw)
                    except (json.JSONDecodeError, ValidationError):
                        log.warning("reasoning.journal.malformed_line")
                        continue
                    latest[cycle.cycle_id] = cycle
            return sorted(latest.values(), key=lambda c: c.timestamp)

        async with self._lock:
            return await asyncio.to_thread(_load)

    async def list_cycles(
        self,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[ReasoningOutput]:
        """Return cycles ordered by timestamp descending.

        Args:
            since: If set, only return cycles with timestamp >= ``since``.
            limit: Maximum number of cycles to return.

        Returns:
            Cycles newest-first, at most *limit*.
        """
        cycles = await self._read_all()
        if since is not None:
            cycles = [c for c in cycles if c.timestamp >= since]
        cycles.sort(key=lambda c: c.timestamp, reverse=True)
        return cycles[:limit]

    async def get_cycle(self, cycle_id: str) -> ReasoningOutput | None:
        """Return the cycle with ``cycle_id`` or ``None`` if unknown."""
        for cycle in await self._read_all():
            if cycle.cycle_id == cycle_id:
                return cycle
        return None
