"""Reasoning loop — scheduled L4 cycle.

Runs on a configurable cadence and is also triggered by "significant event"
heuristics driven by delta thresholds.  Each cycle:

1. Collects a :class:`WorldSnapshot` from L2.
2. Pulls relevant memory excerpts from L3 semantic memory, using the
   entities present in the snapshot as query seeds.
3. Renders a versioned prompt template.
4. Calls the LLM with a Pydantic :class:`ReasoningOutput` response model.
5. Persists the result to L2 (and optionally to the audit journal).

No side effects beyond those persistence calls are produced — reasoning is
passive in Phase 2.  Action and intention layers consume :class:`ReasoningOutput`
objects later.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from pydantic import BaseModel, Field

from coremind.errors import LLMError, ReasoningError
from coremind.reasoning.llm import LLM
from coremind.reasoning.prompts import render_prompt
from coremind.reasoning.schemas import ReasoningOutput, TokenUsage
from coremind.world.model import EntityRef, WorldSnapshot

log = structlog.get_logger(__name__)

type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ReasoningLoopConfig(BaseModel):
    """Scheduler configuration for the reasoning loop."""

    interval_seconds: int = Field(default=900, ge=10)  # 15 minutes
    layer: str = "reasoning_heavy"
    template_system: str = "reasoning.heavy.system.v1"
    template_user: str = "reasoning.heavy.user.v1"
    memory_k_per_entity: int = Field(default=3, ge=0, le=50)
    max_entities_in_prompt: int = Field(default=50, ge=1)


# ---------------------------------------------------------------------------
# Port protocols
# ---------------------------------------------------------------------------


class SnapshotProvider(Protocol):
    """Port that yields a world snapshot."""

    async def snapshot(self, at: datetime | None = None) -> WorldSnapshot:
        """Return a point-in-time world snapshot."""
        ...


class MemoryRecall(Protocol):
    """Port for pulling semantic memories relevant to a query."""

    async def recall(
        self,
        query: str,
        k: int = 10,
        tags: list[str] | None = None,
    ) -> list[object]:
        """Return memories matching *query* (order by descending similarity)."""
        ...


class CyclePersister(Protocol):
    """Port for persisting a completed reasoning cycle.

    Implemented by the L2 world store; a file-based fallback is provided
    for environments without SurrealDB.
    """

    async def persist_cycle(self, cycle: ReasoningOutput) -> None:
        """Persist a reasoning cycle.  Must be idempotent by ``cycle.cycle_id``."""
        ...

    async def list_cycles(
        self,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[ReasoningOutput]:
        """Return cycles ordered by timestamp descending."""
        ...

    async def get_cycle(self, cycle_id: str) -> ReasoningOutput | None:
        """Return a single cycle by id, or ``None`` if unknown."""
        ...


# ---------------------------------------------------------------------------
# ReasoningLoop
# ---------------------------------------------------------------------------


class ReasoningLoop:
    """Scheduled L4 reasoning cycle.

    Args:
        snapshot_provider: Source of world snapshots (the L2 store).
        memory: Semantic memory for excerpt retrieval.  May be ``None`` when
            semantic memory is not yet initialised.
        llm: LLM wrapper for structured completions.
        persister: Destination for completed cycles.
        narrative: Optional narrative identity store for injecting persistent
            user context into each reasoning cycle.
        config: Scheduler parameters.
        clock: Injectable clock for deterministic tests.
    """

    def __init__(
        self,
        snapshot_provider: SnapshotProvider,
        memory: MemoryRecall | None,
        llm: LLM,
        persister: CyclePersister,
        *,
        narrative: object | None = None,
        config: ReasoningLoopConfig | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._snapshots = snapshot_provider
        self._memory = memory
        self._llm = llm
        self._persister = persister
        self._narrative = narrative
        self._config = config or ReasoningLoopConfig()
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._investigation_questions: list[str] = []
        self._about_user_context = self._load_about_user()
        self._load_investigations_from_disk()

    @property
    def config(self) -> ReasoningLoopConfig:
        """Return the scheduler configuration."""
        return self._config

    @staticmethod
    def _load_about_user() -> str:
        """Load the persistent 'about user' context for reasoning prompts."""
        import os

        path = os.path.expanduser("~/.coremind/about_user.txt")
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception:
            return ""

    def _load_investigation_questions(self) -> str:
        """Return previous investigation questions as formatted text."""
        if not self._investigation_questions:
            return ""
        lines = ["Questions you were investigating:"]
        for q in self._investigation_questions[-5:]:  # Keep last 5
            lines.append(f"  - {q}")
        return "\n".join(lines)

    def _save_investigation_questions(self, investigations: Sequence[object]) -> None:
        """Store new investigation questions for future cycles and persist to disk."""
        for inv in investigations:
            if hasattr(inv, "question"):
                self._investigation_questions.append(inv.question)
        # Keep only last 10
        if len(self._investigation_questions) > 10:
            self._investigation_questions = self._investigation_questions[-10:]
        self._persist_investigations_to_disk()

    def _persist_investigations_to_disk(self) -> None:
        """Save investigation questions to a JSON file for persistence across restarts."""
        import json, os

        path = os.path.expanduser("~/.coremind/run/investigations.json")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(self._investigation_questions, f, indent=2)
        except Exception:
            pass

    def _load_investigations_from_disk(self) -> None:
        """Load saved investigation questions from disk."""
        import json, os

        path = os.path.expanduser("~/.coremind/run/investigations.json")
        try:
            with open(path) as f:
                self._investigation_questions = json.load(f)
        except Exception:
            self._investigation_questions = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background scheduler task.

        Idempotent — calling ``start`` again is a no-op while a task is running.
        """
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._scheduler(), name="coremind.reasoning")

    async def stop(self) -> None:
        """Stop the scheduler and wait for the current cycle to finish."""
        self._stop_event.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _scheduler(self) -> None:
        """Run cycles until :meth:`stop` is called."""
        interval = self._config.interval_seconds
        while not self._stop_event.is_set():
            try:
                await self.run_cycle()
            except ReasoningError:
                log.error("reasoning.cycle_failed", exc_info=True)
            except Exception:
                log.exception("reasoning.cycle_unexpected")
                raise
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Cycle execution
    # ------------------------------------------------------------------

    async def run_cycle(self) -> ReasoningOutput:
        """Execute a single reasoning cycle and persist the result.

        Returns:
            The produced :class:`ReasoningOutput`.

        Raises:
            ReasoningError: If snapshot or persistence fails.  LLM failures
                are wrapped into :class:`ReasoningError` as well.
        """
        cycle_id = uuid.uuid4().hex
        now = self._clock()
        log.info("reasoning.cycle.start", cycle_id=cycle_id)

        try:
            snapshot = await self._snapshots.snapshot(at=now)
        except Exception as exc:
            raise ReasoningError("failed to collect world snapshot") from exc

        memory_excerpt = await self._build_memory_excerpt(snapshot)
        narrative_context = await self._build_narrative_context()
        snapshot_json = _snapshot_to_prompt_json(snapshot, self._config.max_entities_in_prompt)
        schema_json = json.dumps(ReasoningOutput.model_json_schema(), indent=2)

        system = render_prompt(self._config.template_system)
        user = render_prompt(
            self._config.template_user,
            snapshot_json=snapshot_json,
            memory_excerpt=memory_excerpt,
            narrative_context=narrative_context,
            schema_json=schema_json,
            about_user=self._about_user_context,
            previous_questions=self._load_investigation_questions(),
        )

        layer = self._config.layer
        model_used = getattr(self._llm.config, layer).model

        # Call LLM — its errors propagate as LLMError; wrap them.
        try:
            output = await self._llm.complete_structured(
                layer=layer,  # type: ignore[arg-type]
                system=system,
                user=user,
                response_model=ReasoningOutput,
            )
        except LLMError as exc:
            raise ReasoningError(f"LLM call failed for cycle {cycle_id}") from exc

        # Overwrite model-provided bookkeeping with authoritative values.
        output = output.model_copy(
            update={
                "cycle_id": cycle_id,
                "timestamp": now,
                "model_used": model_used,
                "token_usage": output.token_usage
                or TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            }
        )

        try:
            await self._persister.persist_cycle(output)
        except Exception as exc:
            raise ReasoningError(f"failed to persist cycle {cycle_id}") from exc

        await self._add_narrative_observation(output)

        # Save investigation questions for future cycles (curiosity loop)
        self._save_investigation_questions(output.investigations)

        log.info(
            "reasoning.cycle.done",
            cycle_id=cycle_id,
            patterns=len(output.patterns),
            anomalies=len(output.anomalies),
            predictions=len(output.predictions),
            investigations=len(output.investigations),
        )
        return output

    async def _build_memory_excerpt(self, snapshot: WorldSnapshot) -> str:
        """Collect relevant memory excerpts for entities in *snapshot*.

        Queries semantic memory for each entity's display name, capped at
        ``max_entities_in_prompt`` entities x ``memory_k_per_entity`` results.
        Deduplicates by memory id before rendering.

        Args:
            snapshot: The world snapshot used to seed queries.

        Returns:
            A markdown-formatted excerpt, or an empty string if no memories
            are available.
        """
        if self._memory is None or self._config.memory_k_per_entity == 0:
            return ""

        k = self._config.memory_k_per_entity
        max_entities = self._config.max_entities_in_prompt
        seen: set[str] = set()
        lines: list[str] = []

        for entity in snapshot.entities[:max_entities]:
            query = entity.display_name or f"{entity.type}"
            try:
                memories = await self._memory.recall(query=query, k=k)
            except Exception:
                log.warning("reasoning.memory_recall_failed", entity=entity.type, exc_info=True)
                continue
            for mem in memories:
                mem_id = getattr(mem, "id", None)
                if mem_id is None or mem_id in seen:
                    continue
                seen.add(mem_id)
                text = getattr(mem, "text", "")
                lines.append(f"- ({entity.type}) {text}")

        if not lines:
            return ""
        return "\n".join(lines[: max_entities * k])

    async def _build_narrative_context(self) -> str:
        """Render the current narrative state as a prompt snippet.

        Returns:
            A markdown-formatted narrative context string, or an empty
            string if no narrative store is configured.
        """
        if self._narrative is None:
            return ""
        try:
            narrative_module = self._narrative
            return narrative_module._render_for_prompt()  # type: ignore[attr-defined, no-any-return]
        except Exception:
            log.warning("reasoning.narrative_context_failed", exc_info=True)
            return ""

    async def _add_narrative_observation(self, output: ReasoningOutput) -> None:
        """Append a key insight from this reasoning cycle to the narrative.

        Extracts the highest-confidence pattern or anomaly description as
        the observation text.
        """
        if self._narrative is None:
            return
        insight = self._extract_key_insight(output)
        if not insight:
            return
        try:
            await self._narrative.add_observation(insight)  # type: ignore[attr-defined]
        except Exception:
            log.warning("reasoning.narrative_observation_failed", exc_info=True)

    @staticmethod
    def _extract_key_insight(output: ReasoningOutput) -> str:
        """Extract the most salient insight from a reasoning cycle output.

        Priority: high-severity anomaly > highest-confidence pattern > None.
        """
        if output.anomalies:
            high = [a for a in output.anomalies if a.severity == "high"]
            if high:
                return high[0].description
        if output.patterns:
            best = max(output.patterns, key=lambda p: p.confidence)
            return best.description
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snapshot_to_prompt_json(snapshot: WorldSnapshot, max_entities: int) -> str:
    """Serialise a snapshot to a compact prompt-friendly JSON string.

    Large snapshots are truncated at ``max_entities`` to keep the prompt
    within reasonable bounds.  Recent events are capped at 50 to avoid
    noise dominating the window.

    Args:
        snapshot: The snapshot to serialise.
        max_entities: Maximum number of entities to include.

    Returns:
        A pretty-printed JSON string.
    """
    doc: dict[str, object] = {
        "taken_at": snapshot.taken_at.isoformat(),
        "entities": [
            {
                "type": e.type,
                "display_name": e.display_name,
                "properties": e.properties,
            }
            for e in snapshot.entities[:max_entities]
        ],
        "recent_events": [
            {
                "timestamp": ev.timestamp.isoformat(),
                "entity": f"{ev.entity.type}:{ev.entity.id}",
                "attribute": ev.attribute,
                "value": ev.value,
                "unit": ev.unit,
            }
            for ev in snapshot.recent_events[:50]
        ],
    }
    return json.dumps(doc, indent=2, default=str)


def entities_touched(snapshot: WorldSnapshot, window: timedelta) -> list[EntityRef]:
    """Return entities mutated within *window* of the snapshot ``taken_at``.

    Utility for "significant event" heuristics that may trigger an
    out-of-schedule reasoning cycle.

    Args:
        snapshot: The snapshot to inspect.
        window: Lookback window.

    Returns:
        Deduplicated entity refs observed in the window.
    """
    cutoff = snapshot.taken_at - window
    seen: set[tuple[str, str]] = set()
    out: list[EntityRef] = []
    for ev in snapshot.recent_events:
        if ev.timestamp < cutoff:
            continue
        key = (ev.entity.type, ev.entity.id)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev.entity)
    return out
