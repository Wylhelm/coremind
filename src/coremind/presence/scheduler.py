"""Presence scheduler — triggers ambient Nest Hub interactions.

The scheduler listens for significant events on the EventBus and dispatches
ambient interactions at configured times (morning greetings, etc.).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time

import structlog

from coremind.core.event_bus import EventBus
from coremind.notify.adapters.nest_hub import NestHubAdapter
from coremind.presence.schemas import PresenceConfig, PresenceEvent, PresenceEventType

log = structlog.get_logger(__name__)


class PresenceScheduler:
    """Schedules and dispatches ambient presence interactions.

    Args:
        adapter: The Nest Hub adapter for speaking/displaying.
        config: Presence configuration.
        event_bus: Optional EventBus to listen for significant events.
    """

    def __init__(
        self,
        adapter: NestHubAdapter,
        config: PresenceConfig,
        *,
        event_bus: EventBus | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._event_bus = event_bus
        self._last_morning: datetime | None = None
        self._last_evening: datetime | None = None

    async def dispatch(self, event: PresenceEvent) -> bool:
        """Dispatch a presence event to the Nest Hub.

        Only speaks if urgency meets the minimum threshold.
        """
        if event.urgency < self._config.min_urgency:
            log.debug(
                "presence.below_urgency", event_type=event.event_type.value, urgency=event.urgency
            )
            return False

        try:
            await self._adapter.notify(
                message=event.message,
                category="info",
            )
            log.info("presence.dispatched", event_type=event.event_type.value)

            if event.display_url:
                await self._adapter.display_url(event.display_url)

            return True
        except Exception as exc:
            log.error("presence.dispatch_failed", error=str(exc))
            return False

    async def maybe_morning_greeting(self, now: datetime | None = None) -> bool:
        """Send a morning greeting if it's time and not already sent today."""
        if not self._config.morning_greeting:
            return False

        now = now or datetime.now()
        today = now.date()

        if self._last_morning and self._last_morning.date() == today:
            return False  # Already greeted today

        try:
            morning_h, morning_m = map(int, self._config.morning_time.split(":"))
        except (ValueError, AttributeError):
            morning_h, morning_m = 8, 0

        target = time(morning_h, morning_m)
        now_time = now.time()

        # Allow a 30-min window around the target time
        if abs((now_time.hour * 60 + now_time.minute) - (target.hour * 60 + target.minute)) > 30:
            return False

        self._last_morning = now
        event = PresenceEvent(
            event_type=PresenceEventType.MORNING_GREETING,
            message="Bonjour Guillaume. Bonne journée.",
            urgency=0.6,
        )
        return await self.dispatch(event)

    async def start(self) -> None:
        """Start the presence scheduler loop."""
        if not self._config.enabled:
            log.info("presence.disabled")
            return

        log.info("presence.started", morning=self._config.morning_greeting)
        while True:
            try:
                await self.maybe_morning_greeting()
            except Exception:
                log.exception("presence.tick_error")
            await asyncio.sleep(60)  # Check every minute
