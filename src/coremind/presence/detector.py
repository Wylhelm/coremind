"""Presence detector — procedural rule that detects prolonged presence.

Phase 1: Simple timer-based detection.
Phase 2: LLM-powered contextual evaluation via :class:`ActivityEvaluator`.

When an :class:`ActivityEvaluator` is provided, the detector delegates the
"should I notify?" decision to the LLM, which considers time of day, day of
week, calendar, sleep data, and activity patterns — not just elapsed time.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog

from coremind.action.router import ActionRouter
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import ActionProposal, Intent, InternalQuestion
from coremind.presence.evaluator import ActivityEvaluator
from coremind.world.store import WorldStore

log = structlog.get_logger(__name__)

# How often to check for presence patterns
CHECK_INTERVAL_SECONDS: int = 300  # 5 minutes
# How long before we notify about prolonged presence (Phase 1 fallback)
PRESENCE_ALERT_MINUTES: int = 120  # 2 hours
# How many consecutive person_present=true observations needed
MIN_CONSECUTIVE: int = 3
# Maximum age of camera data before it's considered stale (seconds)
# Tapo plugin polls every 300s → 2x that = 600s (10 min) is reasonable
MAX_DATA_STALENESS_SECONDS: int = 600  # 10 minutes
# How long before Phase 2 evaluator kicks in (sooner than Phase 1)
EVALUATOR_MIN_ELAPSED_MINUTES: int = 60  # 1 hour
# Number of recent activity samples to keep for context
ACTIVITY_HISTORY_SIZE: int = 72  # ~6h at 5min intervals


class PresenceDetector:
    """Detects prolonged user presence from camera events and generates intents.

    Phase 2: When an :class:`ActivityEvaluator` is provided, delegates the
    notification decision to context-aware LLM evaluation. Falls back to
    simple timer logic when no evaluator is configured.
    """

    def __init__(
        self,
        world_store: WorldStore,
        intent_store: IntentStore,
        router: ActionRouter,
        *,
        alert_minutes: int = PRESENCE_ALERT_MINUTES,
        check_interval: int = CHECK_INTERVAL_SECONDS,
        evaluator: object | None = None,  # ActivityEvaluator | None
        sleep_provider: Callable[[], Awaitable[float | None]] | None = None,
        calendar_provider: Callable[[], Awaitable[list[str] | None]] | None = None,
    ) -> None:
        self._world = world_store
        self._intents = intent_store
        self._router = router
        self._alert_minutes = alert_minutes
        self._interval = check_interval
        self._evaluator = evaluator  # Phase 2: LLM-powered evaluator
        self._sleep_provider = sleep_provider
        self._calendar_provider = calendar_provider
        self._last_alert: datetime | None = None
        self._first_seen_at: datetime | None = None
        # Phase 2: activity history for context
        self._activity_history: list[dict[str, object]] = []
        self._last_eval_at: datetime | None = None

    async def run(self) -> None:
        """Main loop: periodically check for presence patterns."""
        log.info(
            "presence_detector.started",
            alert_minutes=self._alert_minutes,
            interval=self._interval,
        )
        while True:
            try:
                await self._check()
            except Exception:
                log.exception("presence_detector.check_error")
            await asyncio.sleep(self._interval)

    async def _check(self) -> None:
        """Check camera entity for prolonged presence."""
        try:
            snapshot = await self._world.snapshot()
        except Exception:
            return

        # Find the tapo camera entity
        tapo = None
        for entity in snapshot.entities:
            name = getattr(entity, "display_name", "") or str(entity)
            if "tapo" in str(name):
                tapo = entity
                break

        if tapo is None:
            return

        # --- Staleness check: ignore data from a dead/crashed plugin ---
        updated_at = getattr(tapo, "updated_at", None)
        now = datetime.now(UTC)
        if updated_at is not None:
            age_seconds = (now - updated_at).total_seconds()
            if age_seconds > MAX_DATA_STALENESS_SECONDS:
                # Plugin hasn't updated in too long → data is stale
                if self._first_seen_at is not None:
                    log.warning(
                        "presence_detector.stale_data_reset",
                        entity=tapo.display_name,
                        age_seconds=int(age_seconds),
                        max_staleness=MAX_DATA_STALENESS_SECONDS,
                    )
                self._first_seen_at = None
                self._last_alert = None
                self._last_eval_at = None
                self._activity_history = []
                return

        # Get properties
        props = getattr(tapo, "properties", {}) or {}

        person_present = props.get("person_present")
        person_name = props.get("person_name", "unknown")
        activity = props.get("activity", "unknown")

        if person_present is not True:
            self._first_seen_at = None
            self._last_alert = None
            self._last_eval_at = None
            self._activity_history = []
            return

        # Track when we first saw the person continuously present
        if self._first_seen_at is None:
            self._first_seen_at = now
            return  # Wait for next check to confirm presence

        # Phase 2: Record activity sample for context
        self._activity_history.append(
            {
                "timestamp": now.isoformat(),
                "activity": activity,
                "person_name": person_name,
            }
        )
        # Trim history
        if len(self._activity_history) > ACTIVITY_HISTORY_SIZE:
            self._activity_history = self._activity_history[-ACTIVITY_HISTORY_SIZE:]

        # Time since first detection
        elapsed_minutes = (now - self._first_seen_at).total_seconds() / 60

        # ------------------------------------------------------------------
        # Phase 2: LLM-powered contextual evaluation
        # ------------------------------------------------------------------
        if self._evaluator is not None and elapsed_minutes >= EVALUATOR_MIN_ELAPSED_MINUTES:
            # Only evaluate every 30 min to avoid spamming the LLM
            if self._last_eval_at and (now - self._last_eval_at).total_seconds() < 1800:
                return

            self._last_eval_at = now

            try:
                evaluator: ActivityEvaluator = self._evaluator  # type: ignore[assignment]

                # Fetch external context (non-blocking, graceful degradation)
                sleep_hours: float | None = None
                calendar_events: list[str] | None = None

                if self._sleep_provider:
                    try:
                        sleep_hours = await self._sleep_provider()
                    except Exception:
                        log.debug("presence_detector.sleep_provider_error", exc_info=True)

                if self._calendar_provider:
                    try:
                        calendar_events = await self._calendar_provider()
                    except Exception:
                        log.debug("presence_detector.calendar_provider_error", exc_info=True)

                evaluation = await evaluator.evaluate(
                    person_name=str(person_name),
                    current_activity=str(activity),
                    elapsed_minutes=elapsed_minutes,
                    activity_history=list(self._activity_history),
                    sleep_hours=sleep_hours,
                    calendar_events=calendar_events,
                )

                if not evaluation.should_notify:
                    log.info(
                        "presence_detector.evaluator_suppressed",
                        reason=evaluation.reason,
                        salience=evaluation.salience,
                        elapsed_minutes=elapsed_minutes,
                    )
                    return

                # Use the evaluator's suggestion
                question_text = evaluation.suggested_message or (
                    f"Hey, ça fait {int(elapsed_minutes / 60)}h{int(elapsed_minutes % 60):02d} "
                    f"que tu es là. Une pause?"
                )

                intent = Intent(
                    id=uuid.uuid4().hex,
                    created_at=now,
                    question=InternalQuestion(
                        id=uuid.uuid4().hex,
                        text=question_text,
                    ),
                    proposed_action=ActionProposal(
                        operation="coremind.plugin.notification.send",
                        parameters={"title": "Pause bien-être", "message": question_text},
                        expected_outcome=evaluation.reason,
                        action_class="notification.send",
                    ),
                    salience=evaluation.salience,
                    confidence=evaluation.confidence,
                    category="suggest",
                    status="pending",
                )

                await self._intents.save(intent)
                await self._router.route(intent)
                self._last_alert = now
                log.info(
                    "presence_detector.evaluator_alert",
                    salience=evaluation.salience,
                    confidence=evaluation.confidence,
                    reason=evaluation.reason,
                    elapsed_minutes=elapsed_minutes,
                )
                return

            except Exception:
                log.exception("presence_detector.evaluator_error")
                # Fall through to Phase 1 logic below

        # ------------------------------------------------------------------
        # Phase 1 fallback: simple timer (used when no evaluator or on error)
        # ------------------------------------------------------------------
        if elapsed_minutes < self._alert_minutes:
            return

        # Don't alert more than once per hour
        if self._last_alert and (now - self._last_alert).total_seconds() < 3600:
            return

        # Generate intent — never claim the user is at a specific desk/room.
        # The camera is in the living room and cannot determine actual room.
        hours = int(elapsed_minutes / 60)
        minutes = int(elapsed_minutes % 60)
        desk_keywords = ["desk", "computer", "working", "bureau", "ordinateur", "travail"]
        is_at_desk = any(kw in str(activity).lower() for kw in desk_keywords)

        if is_at_desk:
            name_str = f" ({person_name})" if person_name and person_name != "unknown" else ""
            question_text = (
                f"Hey{name_str}, ça fait {hours}h{minutes:02d} que tu travailles. "
                f"Une petite pause ? ☕"
            )
        else:
            question_text = f"Je te vois dans le salon depuis {hours}h{minutes:02d}. Tout va bien ?"

        intent = Intent(
            id=uuid.uuid4().hex,
            created_at=now,
            question=InternalQuestion(
                id=uuid.uuid4().hex,
                text=question_text,
            ),
            proposed_action=ActionProposal(
                operation="coremind.plugin.notification.send",
                parameters={"title": "Pause bien-être", "message": question_text},
                expected_outcome=f"Je te rappelle de faire une pause après {hours}h{minutes:02d}.",
                action_class="notification.send",
            ),
            salience=0.88,
            confidence=0.82,
            category="conversation",
            status="pending",
        )

        await self._intents.save(intent)
        await self._router.route(intent)
        self._last_alert = now
        log.info(
            "presence_detector.alert_sent",
            elapsed_minutes=elapsed_minutes,
            activity=activity,
        )
