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
import time
from datetime import datetime
from pathlib import Path

import structlog
from pydantic import ValidationError

from coremind.errors import IntentionError
from coremind.intention.schemas import Intent, IntentStatus

log = structlog.get_logger(__name__)

type _IntentList = list[Intent]

# Rate-limiting for malformed_line warnings: at most 1 per minute per error kind.
_MALFORMED_LOG_WINDOW_SECONDS = 60
_last_malformed_log: dict[str, float] = {}


class IntentStore:
    """JSONL-backed intent store.

    Args:
        store_path: Path to the JSONL file.  Created on first write.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = asyncio.Lock()
        # In-memory cache: invalidated after every save().
        self._cache: dict[str, Intent] | None = None
        self._cache_mtime: float = 0.0

    async def save(self, intent: Intent) -> None:
        """Append ``intent`` to the journal.

        Idempotent by ``intent.id`` on read — later writes of the same id
        shadow earlier ones.

        Validates the intent via Pydantic before writing to prevent
        corrupt entries from entering the store.

        Raises:
            IntentionError: On validation or write failure.
        """
        # Validate BEFORE writing — never let an invalid intent hit disk.
        try:
            Intent.model_validate(intent.model_dump())
        except ValidationError as exc:
            raise IntentionError(f"refusing to save invalid intent {intent.id!r}: {exc}") from exc

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
            # Invalidate cache after every write.
            self._cache = None

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

    async def list_intents(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> _IntentList:
        """Return intents with ``created_at`` in ``[since, until)``.

        Half-open interval matches the CycleSource / IntentSource protocol
        contract used by the reflection loop.
        """
        latest = await self._read_latest()
        items = [i for i in latest.values() if i.created_at >= since and i.created_at < until]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items

    async def recent(
        self,
        *,
        since: datetime,
    ) -> _IntentList:
        """Return intents created since ``since`` (any status), newest first."""
        return await self.list(since=since, limit=10_000)

    async def _read_latest(self) -> dict[str, Intent]:
        """Replay the JSONL file and return the most recent Intent per id.

        Uses an in-memory cache keyed on file modification time.  The cache
        is invalidated after every :meth:`save` call.
        """
        if not self._path.exists():
            return {}

        async with self._lock:
            # Use cache if still valid.
            mtime = self._path.stat().st_mtime if self._path.exists() else 0.0
            if self._cache is not None and mtime <= self._cache_mtime:
                return self._cache

            def _load() -> dict[str, Intent]:
                latest: dict[str, Intent] = {}
                bad_count: dict[str, int] = {}
                with self._path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        try:
                            raw = json.loads(line)
                            intent = Intent.model_validate(raw)
                        except (json.JSONDecodeError, ValidationError) as exc:
                            # Rate-limit: max 1 warning per error kind per minute.
                            err_kind = type(exc).__name__
                            now = time.monotonic()
                            last = _last_malformed_log.get(err_kind, 0.0)
                            if now - last >= _MALFORMED_LOG_WINDOW_SECONDS:
                                _last_malformed_log[err_kind] = now
                                bad_count[err_kind] = bad_count.get(err_kind, 0) + 1
                            else:
                                bad_count[err_kind] = bad_count.get(err_kind, 0) + 1
                            continue
                        latest[intent.id] = intent
                # Single summary warning once per window if any bad lines found.
                for err_kind, count in bad_count.items():
                    if count > 0:
                        log.warning(
                            "intention.store.malformed_lines",
                            count=count,
                            error_kind=err_kind,
                        )
                return latest

            self._cache = await asyncio.to_thread(_load)
            self._cache_mtime = mtime
            return self._cache
