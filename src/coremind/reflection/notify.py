"""Notification adapter for delivering L7 reflection reports.

Wraps a :class:`coremind.notify.router.NotificationRouter` (or any
:class:`NotificationPort`) to deliver the Markdown report produced by
a reflection cycle through the daemon's notification channels.

Implements the :class:`coremind.reflection.loop.ReportNotifier` protocol.
"""

from __future__ import annotations

import structlog

from coremind.errors import NotificationError
from coremind.notify.port import NotificationPort
from coremind.reflection.schemas import ReflectionReport

log = structlog.get_logger(__name__)


class ReflectionNotifier:
    """Delivers reflection reports through a :class:`NotificationPort`.

    Args:
        port: The notification port (typically the daemon's
            :class:`NotificationRouter` instance) used to dispatch
            reports to the user.
    """

    def __init__(self, port: NotificationPort) -> None:
        self._port = port

    async def deliver(self, report: ReflectionReport) -> None:
        """Send ``report`` to the user via the configured notification port.

        The report's ``markdown`` field is sent as an ``info``-category
        message with no approval actions (it's a read-only digest).

        Args:
            report: The completed reflection report to deliver.

        Raises:
            NotificationError: If the notification port fails to deliver.
        """
        title = f"🧠 Weekly Reflection — {report.cycle_id}"
        body = report.markdown if report.markdown else "No activity this cycle."

        # Truncate to avoid extremely long messages (Telegram has a 4096
        # char limit; we stay under 3500 to leave room for the title).
        max_body_len = 3500
        if len(body) > max_body_len:
            body = body[:max_body_len] + "\n\n*(truncated — see dashboard for full report)*"

        message = f"{title}\n\n{body}"

        try:
            receipt = await self._port.notify(
                message=message,
                category="info",
                actions=None,
                intent_id=None,
            )
            log.info(
                "reflection.notify.delivered",
                cycle_id=report.cycle_id,
                port_id=receipt.port_id,
                channel_message_id=receipt.channel_message_id,
            )
        except NotificationError:
            log.warning(
                "reflection.notify.failed",
                cycle_id=report.cycle_id,
                exc_info=True,
            )
            raise
