"""Presence detector — a procedural rule that detects prolonged presence.

Checks the world model for consecutive person_present=true events from the
Tapo camera and generates intents when someone has been present for an
extended period. This bypasses the LLM-based reasoning for this specific
high-value pattern.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog

from coremind.world.store import WorldStore
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import Intent, InternalQuestion, ActionProposal
from coremind.action.router import ActionRouter

log = structlog.get_logger(__name__)

# How often to check for presence patterns
CHECK_INTERVAL_SECONDS: int = 300  # 5 minutes
# How long before we notify about prolonged presence
PRESENCE_ALERT_MINUTES: int = 60  # 1 hour
# How many consecutive person_present=true observations needed
MIN_CONSECUTIVE: int = 3


class PresenceDetector:
    """Detects prolonged user presence from camera events and generates intents."""

    def __init__(
        self,
        world_store: WorldStore,
        intent_store: IntentStore,
        router: ActionRouter,
        *,
        alert_minutes: int = PRESENCE_ALERT_MINUTES,
        check_interval: int = CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._world = world_store
        self._intents = intent_store
        self._router = router
        self._alert_minutes = alert_minutes
        self._interval = check_interval
        self._last_alert: datetime | None = None
        self._first_seen_at: datetime | None = None

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
            name = getattr(entity, 'display_name', '') or str(entity)
            if 'tapo' in str(name):
                tapo = entity
                break

        if tapo is None:
            return

        # Get properties
        props = getattr(tapo, 'properties', {}) or {}

        person_present = props.get('person_present')
        person_name = props.get('person_name', 'unknown')
        activity = props.get('activity', 'unknown')

        if person_present is not True:
            self._first_seen_at = None
            self._last_alert = None
            return

        # Track when we first saw the person continuously present
        now = datetime.now(UTC)
        if self._first_seen_at is None:
            self._first_seen_at = now
            return  # Wait for next check to confirm presence

        # Time since first detection
        elapsed_minutes = (now - self._first_seen_at).total_seconds() / 60

        if elapsed_minutes < self._alert_minutes:
            return

        # Don't alert more than once per hour
        if self._last_alert and (now - self._last_alert).total_seconds() < 3600:
            return

        # Generate intent
        hours = int(elapsed_minutes / 60)
        minutes = int(elapsed_minutes % 60)
        desk_keywords = ['desk', 'computer', 'working', 'bureau', 'ordinateur', 'travail']
        is_at_desk = any(kw in str(activity).lower() for kw in desk_keywords)

        if is_at_desk:
            name_str = f" ({person_name})" if person_name and person_name != "unknown" else ""
            question_text = (
                f"Hey{name_str}, tu es à ton bureau depuis {hours}h{minutes:02d}. "
                f"Une petite pause ? ☕"
            )
        else:
            question_text = (
                f"Je remarque que tu es dans la pièce depuis {hours}h{minutes:02d}. "
                f"Tout va bien ?"
            )

        intent = Intent(
            id=uuid.uuid4().hex,
            created_at=now,
            question=InternalQuestion(
                id=uuid.uuid4().hex,
                text=question_text,
            ),
            proposed_action=ActionProposal(
                operation="notify_user",
                parameters={"message": question_text},
                expected_outcome=f"User receives a friendly presence alert after {hours}h{minutes:02d}",
                action_class="presence_alert",
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
