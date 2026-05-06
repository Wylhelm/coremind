"""Narrative identity layer — persistent context between reasoning cycles (L4, narrative).

Maintains a living summary of the user's life context as a JSON file at
~/.coremind/run/narrative_state.json.  New observations are appended with
auto-decay after 7 days.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

type Clock = Callable[[], datetime]

_NARRATIVE_PATH = Path.home() / ".coremind" / "run" / "narrative_state.json"
_OBSERVATION_TTL_DAYS = 7
_MAX_PATTERNS = 20
_MAX_CONCERNS = 20


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampedItem(BaseModel):
    text: str
    recorded_at: datetime = Field(default_factory=_utc_now)


class NarrativeState(BaseModel):
    user_mood_trend: str = "stable"
    recent_patterns: list[TimestampedItem] = Field(default_factory=list)
    active_concerns: list[TimestampedItem] = Field(default_factory=list)
    relationship_notes: str = ""
    last_updated: datetime = Field(default_factory=_utc_now)
    version: int = 1


class NarrativeMemory:
    """Persistent narrative identity store backed by a JSON file.

    Args:
        store_path: Path to the narrative state file.
            Defaults to ``~/.coremind/run/narrative_state.json``.
        clock: Injectable clock for deterministic tests.
    """

    def __init__(
        self,
        *,
        store_path: Path | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._path = store_path or _NARRATIVE_PATH
        self._clock = clock
        self._lock = asyncio.Lock()
        self._state = NarrativeState()

    def get_current(self) -> NarrativeState:
        return self._state

    async def load(self) -> None:
        """Load narrative state from disk.  Idempotent — safe to call at startup."""
        async with self._lock:
            if not self._path.exists():
                return
            try:
                content = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
                self._state = NarrativeState.model_validate_json(content)
                log.info("narrative.loaded", version=self._state.version)
            except (OSError, ValueError) as exc:
                log.warning(
                    "narrative.load_failed",
                    path=str(self._path),
                    error=str(exc),
                )

    async def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> None:
            self._path.write_text(self._state.model_dump_json(indent=2), encoding="utf-8")

        await asyncio.to_thread(_write)

    def _decay(self, items: list[TimestampedItem], now: datetime) -> list[TimestampedItem]:
        cutoff = now - timedelta(days=_OBSERVATION_TTL_DAYS)
        return [item for item in items if item.recorded_at > cutoff]

    async def update(
        self,
        *,
        user_mood_trend: str | None = None,
        recent_patterns: list[str] | None = None,
        active_concerns: list[str] | None = None,
        relationship_notes: str | None = None,
    ) -> None:
        """Update one or more fields of the narrative state.

        Only non-None values are applied; omitted fields are preserved.
        """
        async with self._lock:
            now = self._clock()
            update_dict: dict[str, object] = {}
            if user_mood_trend is not None:
                update_dict["user_mood_trend"] = user_mood_trend
            if recent_patterns is not None:
                update_dict["recent_patterns"] = [
                    TimestampedItem(text=t, recorded_at=now) for t in recent_patterns
                ]
            if active_concerns is not None:
                update_dict["active_concerns"] = [
                    TimestampedItem(text=t, recorded_at=now) for t in active_concerns
                ]
            if relationship_notes is not None:
                update_dict["relationship_notes"] = relationship_notes
            update_dict["last_updated"] = now
            update_dict["version"] = self._state.version + 1
            self._state = self._state.model_copy(update=update_dict)
            await self._save()
            log.info("narrative.updated", version=self._state.version)

    async def add_observation(self, text: str) -> None:
        """Append a single observation to recent patterns with auto-decay.

        Observations older than ``_OBSERVATION_TTL_DAYS`` are pruned on every
        append.  The list is capped at ``_MAX_PATTERNS`` entries.
        """
        async with self._lock:
            now = self._clock()
            patterns = self._decay(self._state.recent_patterns, now)
            patterns.append(TimestampedItem(text=text, recorded_at=now))
            if len(patterns) > _MAX_PATTERNS:
                patterns = patterns[-_MAX_PATTERNS:]
            concerns = self._decay(self._state.active_concerns, now)
            self._state = self._state.model_copy(
                update={
                    "recent_patterns": patterns,
                    "active_concerns": concerns,
                    "last_updated": now,
                    "version": self._state.version + 1,
                }
            )
            await self._save()
            log.info("narrative.observation_added", text=text[:80])

    def _render_for_prompt(self) -> str:
        """Render the narrative state as a markdown snippet for LLM prompts."""
        parts: list[str] = []
        mood = self._state.user_mood_trend
        if mood:
            parts.append(f"- User mood trend: {mood}")
        patterns = self._decay(self._state.recent_patterns, self._clock())
        if patterns:
            parts.append("- Recent patterns:")
            for p in patterns:
                parts.append(f"  - {p.text}")
        concerns = self._decay(self._state.active_concerns, self._clock())
        if concerns:
            parts.append("- Active concerns:")
            for c in concerns:
                parts.append(f"  - {c.text}")
        notes = self._state.relationship_notes
        if notes:
            parts.append(f"- Relationship notes: {notes}")
        if not parts:
            return "(no narrative context yet)"
        return "\n".join(parts)
