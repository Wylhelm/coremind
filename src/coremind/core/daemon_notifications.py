"""Notification router builder for the CoreMind daemon.

Extracted from :mod:`coremind.core.daemon` to keep the orchestrator lean.
"""

from __future__ import annotations

import os
from datetime import time

import structlog

from coremind.config import DaemonConfig
from coremind.notify.adapters.dashboard import DashboardNotificationPort
from coremind.notify.adapters.telegram import TelegramNotificationPort
from coremind.notify.port import NotificationPort
from coremind.notify.quiet_hours import QuietHoursFilter, QuietHoursPolicy
from coremind.notify.router import NotificationRouter

log = structlog.get_logger(__name__)


def build_notification_router(
    config: DaemonConfig,
    dashboard_port: DashboardNotificationPort,
) -> NotificationRouter:
    """Construct the notification router from ``config``.

    Args:
        config: The validated daemon configuration.
        dashboard_port: The dashboard adapter the router should route to.
            Passed in so the daemon can keep a reference for the web
            dashboard's data sources, rather than constructing one inside.

    Currently supports dashboard + telegram adapters.  Telegram is wired only
    when ``config.notify.telegram.enabled`` is true; otherwise the dashboard
    port is used as primary with no fallbacks.

    Secrets loading is deferred to Phase 4's SecretsStore; if Telegram is
    enabled but the bot token is unavailable, the adapter falls back to a
    disabled state at notify time.
    """
    ports: dict[str, NotificationPort] = {"dashboard": dashboard_port}

    if config.notify.telegram.enabled and config.notify.telegram.chat_id:
        token = os.environ.get("COREMIND_TELEGRAM_BOT_TOKEN", "")
        if token:
            ports["telegram"] = TelegramNotificationPort(
                token,
                config.notify.telegram.chat_id,
            )

    primary = ports.get(config.notify.primary) or ports["dashboard"]
    fallbacks = [ports[name] for name in config.notify.fallbacks if name in ports]

    policy = QuietHoursPolicy(
        timezone=config.quiet_hours.timezone,
        quiet_start=config.quiet_hours.quiet_start,
        quiet_end=config.quiet_hours.quiet_end,
    )
    quiet = QuietHoursFilter(policy) if config.quiet_hours.enabled else _AllowAllFilter()
    return NotificationRouter(primary, fallbacks, quiet)


class _AllowAllFilter(QuietHoursFilter):
    """Quiet-hours filter that never defers — used when the policy is disabled."""

    def __init__(self) -> None:
        super().__init__(QuietHoursPolicy(quiet_start=time(0, 0), quiet_end=time(0, 0)))
