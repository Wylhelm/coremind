"""Effector registry builder for the CoreMind daemon.

Extracted from :mod:`coremind.core.daemon` to keep the orchestrator lean.
"""

from __future__ import annotations

import structlog

from coremind.action.effectors import (
    CalendarEffector,
    EffectorRegistry,
    GmailEffector,
    HomeAssistantEffector,
    NotificationEffector,
    VikunjaEffector,
)
from coremind.notify.router import NotificationRouter

log = structlog.get_logger(__name__)


def build_effector_registry(
    notify_router: NotificationRouter,
) -> EffectorRegistry:
    """Build the in-process effector registry with all available effectors.

    Each effector wraps an external API and implements :class:`EffectorPort`.
    The registry doubles as an :class:`EffectorResolver` callable, so it can
    be passed directly to :class:`Executor`.

    When the future Phase 3.5 gRPC reverse-channel lands, this function can
    be replaced by one that builds per-plugin gRPC effector stubs.  For now,
    in-process effectors are pragmatic and sufficient.
    """
    registry = EffectorRegistry()

    # Notification effector — wraps the existing notification router
    notifier = NotificationEffector(notify_router)
    registry.register("coremind.plugin.notification.send", notifier)
    registry.register("coremind.plugin.notification.send_sms", notifier)
    # Alias: LLM sometimes generates different operation names for the same thing
    registry.register("coremind.plugin.telegram.send_message", notifier)
    registry.register("coremind.plugin.task_manager.remind", notifier)

    # Home Assistant effector
    ha = HomeAssistantEffector()
    registry.register_many(
        [
            "coremind.plugin.homeassistant.get_state",
            "coremind.plugin.homeassistant.get_history",
            "coremind.plugin.homeassistant.turn_on",
            "coremind.plugin.homeassistant.turn_off",
            "coremind.plugin.homeassistant.light.turn_off",
            "coremind.plugin.homeassistant.create_automation",
            "coremind.plugin.homeassistant.send_notification",
            "coremind.plugin.homeassistant.get_printer_estimated_pages",
            "coremind.plugin.homeassistant.set_temperature",
        ],
        ha,
    )

    # Vikunja task manager effector
    vikunja = VikunjaEffector()
    registry.register_many(
        [
            "coremind.plugin.vikunja.list_tasks",
            "coremind.plugin.vikunja.get_tasks",
        ],
        vikunja,
    )

    # Gmail effector (via gog CLI)
    gmail = GmailEffector()
    registry.register_many(
        [
            "coremind.plugin.gmail.fetch_unread",
            "coremind.plugin.gmail.search_emails",
        ],
        gmail,
    )

    # Calendar effector (Google Calendar via gog)
    calendar = CalendarEffector()
    registry.register_many(
        [
            "coremind.plugin.calendar.fetch_upcoming_events",
            "coremind.plugin.calendar.get_next_payday",
        ],
        calendar,
    )

    log.info("effector_registry.built", operation_count=len(registry._effectors))
    return registry
