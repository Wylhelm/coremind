"""Scheduled intention loop (L5).

Every ``interval_seconds`` (default 600) the loop:

1. Takes a world snapshot from L2.
2. Pulls recent reasoning cycles and recent intents for context.
3. Calls the LLM with the intention prompt to produce a
   :class:`~coremind.intention.schemas.QuestionBatch`.
4. Scores salience and confidence for each candidate.
5. Wraps candidates in :class:`~coremind.intention.schemas.Intent` objects.
6. Hands each new intent off to the :class:`~coremind.action.router.ActionRouter`.

Failures in one cycle never kill the loop: exceptions are logged and the
next tick proceeds normally.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from pydantic import BaseModel, Field

from coremind.action.router import ActionRouter
from coremind.errors import IntentionError, LLMError
from coremind.intention.persistence import IntentStore
from coremind.intention.prompts import render_prompt
from coremind.intention.salience import (
    categorize,
    score_confidence,
    score_salience,
)
from coremind.intention.schemas import (
    Intent,
    QuestionBatch,
    RawIntent,
)
from coremind.reasoning.llm import LLM
from coremind.reasoning.schemas import ReasoningOutput
from coremind.world.model import JsonValue, WorldSnapshot

log = structlog.get_logger(__name__)

type Clock = Callable[[], datetime]

# Near-identical recent question suppression threshold (Jaccard token overlap).
_DUPLICATE_JACCARD_THRESHOLD = 0.85


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class IntentionLoopConfig(BaseModel):
    """Scheduler configuration for the intention loop."""

    interval_seconds: int = Field(default=600, ge=10)
    template_system: str = "intention.system.v1"
    template_user: str = "intention.user.v1"
    max_entities_in_prompt: int = Field(default=40, ge=1)
    max_questions: int = Field(default=5, ge=1, le=20)
    recent_intent_window_hours: int = Field(default=24, ge=1)
    recent_reasoning_window_hours: int = Field(default=1, ge=1)
    min_salience: float = Field(default=0.0, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Port protocols
# ---------------------------------------------------------------------------


class SnapshotProvider(Protocol):
    """Yields world snapshots."""

    async def snapshot(self, at: datetime | None = None) -> WorldSnapshot:
        """Return a snapshot at ``at`` (or now)."""
        ...


class ReasoningFeed(Protocol):
    """Yields recent :class:`ReasoningOutput` entries for context."""

    async def list_cycles(
        self, since: datetime | None = None, limit: int = 50
    ) -> list[ReasoningOutput]:
        """Return reasoning cycles, newest first."""
        ...


class PatternProvider(Protocol):
    """Exposes active procedural-memory patterns as a summary string."""

    async def active_patterns_summary(self) -> str:
        """Return a compact text description of currently-active patterns."""
        ...


class RuleMatcher(Protocol):
    """Counts procedural rules matching a given context.

    Used solely for confidence scoring; ``context`` is a flat dict the
    intention loop builds from the snapshot.
    """

    async def match_count(self, context: dict[str, JsonValue]) -> int:
        """Return the number of active rules whose trigger matches."""
        ...


# ---------------------------------------------------------------------------
# IntentionLoop
# ---------------------------------------------------------------------------


class IntentionLoop:
    """Scheduled L5 loop.

    Args:
        snapshot_provider: Source of world snapshots.
        reasoning_feed: Recent reasoning cycles.
        intent_store: Intent persistence.
        llm: LLM wrapper.
        router: Action router for dispatching freshly formed intents.
        patterns: Optional procedural-pattern summary provider.
        rule_matcher: Optional rule-match counter for confidence.
        config: Scheduler parameters.
        clock: Injectable clock.
    """

    def __init__(
        self,
        snapshot_provider: SnapshotProvider,
        reasoning_feed: ReasoningFeed,
        intent_store: IntentStore,
        llm: LLM,
        router: ActionRouter,
        *,
        patterns: PatternProvider | None = None,
        rule_matcher: RuleMatcher | None = None,
        config: IntentionLoopConfig | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._snapshots = snapshot_provider
        self._reasoning = reasoning_feed
        self._intents = intent_store
        self._llm = llm
        self._router = router
        self._patterns = patterns
        self._rules = rule_matcher
        self._config = config or IntentionLoopConfig()
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background scheduler task.  Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._scheduler(), name="coremind.intention")

    async def stop(self) -> None:
        """Stop the scheduler."""
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
            except IntentionError:
                log.error("intention.cycle_failed", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The phase contract is "failures in one cycle never kill the
                # loop".  Any unexpected error is logged and the next tick
                # proceeds normally.
                log.exception("intention.cycle_unexpected")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Cycle execution
    # ------------------------------------------------------------------

    async def run_cycle(self) -> list[Intent]:
        """Execute a single intention cycle.

        Returns:
            The list of freshly created :class:`Intent` objects (possibly empty).
        """
        now = self._clock()
        try:
            snapshot = await self._snapshots.snapshot(at=now)
        except Exception as exc:
            raise IntentionError("failed to collect world snapshot") from exc

        cycles = await self._reasoning.list_cycles(
            since=now - timedelta(hours=self._config.recent_reasoning_window_hours),
            limit=20,
        )
        recent_intents = await self._intents.recent(
            since=now - timedelta(hours=self._config.recent_intent_window_hours)
        )

        system = render_prompt(self._config.template_system)
        user = render_prompt(
            self._config.template_user,
            snapshot_json=_snapshot_to_json(snapshot, self._config.max_entities_in_prompt),
            reasoning_summary=_reasoning_summary(cycles),
            recent_intents_summary=_recent_intents_summary(recent_intents),
            patterns_summary=(
                await self._patterns.active_patterns_summary()
                if self._patterns is not None
                else "(none)"
            ),
            schema_json=json.dumps(QuestionBatch.model_json_schema(), indent=2),
            max_questions=self._config.max_questions,
        )

        try:
            batch = await self._llm.complete_structured(
                layer="intention",
                system=system,
                user=user,
                response_model=QuestionBatch,
            )
        except LLMError as exc:
            raise IntentionError("intention LLM call failed") from exc

        return await self._form_intents(batch, snapshot, recent_intents, now)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _form_intents(
        self,
        batch: QuestionBatch,
        snapshot: WorldSnapshot,
        recent_intents: list[Intent],
        now: datetime,
    ) -> list[Intent]:
        """Score raw intents, persist them, and dispatch through the router."""
        out: list[Intent] = []
        for raw in batch.questions[: self._config.max_questions]:
            if _is_duplicate(raw, recent_intents):
                log.debug("intention.duplicate_skipped", question=raw.question.text)
                continue
            salience = score_salience(raw, snapshot, recent_intents)
            rule_matches = 0
            if self._rules is not None:
                context = _snapshot_context(snapshot)
                try:
                    rule_matches = await self._rules.match_count(context)
                except Exception:
                    log.warning("intention.rule_match_failed", exc_info=True)
            confidence = score_confidence(raw, rule_matches)
            category = categorize(
                confidence,
                raw.proposed_action.action_class if raw.proposed_action else None,
            )
            # High-salience suggests become conversation openers
            if category == "suggest" and salience >= 0.70:
                category = "conversation"
            intent = Intent(
                id=uuid.uuid4().hex,
                created_at=now,
                question=raw.question,
                proposed_action=raw.proposed_action,
                salience=salience,
                confidence=confidence,
                category=category,  # type: ignore[arg-type]
                status="pending",
            )
            # Skip low-confidence intents — don't bother the user
            if (self._config.min_salience > 0 and salience < self._config.min_salience) or (
                self._config.min_confidence > 0 and confidence < self._config.min_confidence
            ):
                intent.status = "auto_dismissed"
                await self._intents.save(intent)
                log.debug(
                    "intention.auto_dismissed",
                    id=intent.id,
                    salience=salience,
                    confidence=confidence,
                )
                continue
            await self._intents.save(intent)
            try:
                await self._router.route(intent)
            except Exception:
                log.exception("intention.route_failed", intent_id=intent.id)
            out.append(intent)
        log.info(
            "intention.cycle.done",
            produced=len(out),
            candidates=len(batch.questions),
        )
        return out


# Pure helpers below.


def _snapshot_to_json(snapshot: WorldSnapshot, max_entities: int) -> str:
    """Serialise a snapshot to compact prompt-friendly JSON."""
    doc = {
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
            }
            for ev in snapshot.recent_events[:50]
        ],
    }
    return json.dumps(doc, indent=2, default=str)


def _reasoning_summary(cycles: list[ReasoningOutput]) -> str:
    """One-line-per-cycle digest suitable for a prompt."""
    if not cycles:
        return "(no recent reasoning cycles)"
    lines: list[str] = []
    for c in cycles[:5]:
        lines.append(
            f"- {c.timestamp.isoformat()} "
            f"patterns={len(c.patterns)} "
            f"anomalies={len(c.anomalies)} "
            f"predictions={len(c.predictions)}"
        )
    return "\n".join(lines)


def _recent_intents_summary(intents: list[Intent]) -> str:
    """One-line digest of recent intents for loop avoidance."""
    if not intents:
        return "(no recent intents)"
    lines = [f"- [{i.status}] {i.question.text}" for i in intents[:10]]
    return "\n".join(lines)


def _snapshot_context(snapshot: WorldSnapshot) -> dict[str, JsonValue]:
    """Flatten a snapshot into a context dict for rule-match lookups."""
    ctx: dict[str, JsonValue] = {}
    for ev in snapshot.recent_events[:20]:
        ctx[f"{ev.entity.type}.{ev.attribute}"] = ev.value
    return ctx


def _is_duplicate(raw: RawIntent, recent_intents: list[Intent]) -> bool:
    """Return ``True`` if ``raw`` duplicates one of ``recent_intents``.

    Uses a very high Jaccard overlap threshold so near-duplicates are caught
    without penalising merely related questions.
    """
    new_tokens = set(raw.question.text.lower().split())
    if not new_tokens:
        return False
    for prior in recent_intents:
        prior_tokens = set(prior.question.text.lower().split())
        if not prior_tokens:
            continue
        inter = len(new_tokens & prior_tokens)
        union = len(new_tokens | prior_tokens)
        if union and inter / union >= _DUPLICATE_JACCARD_THRESHOLD:
            return True
    return False
