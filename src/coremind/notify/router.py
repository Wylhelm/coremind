"""Notification router — primary + fallbacks + quiet-hours policy.

The router is a :class:`NotificationPort` itself so callers upstream (the
approval gate and executor) do not need to know about adapters or quiet
hours at all.

Delivery algorithm
------------------

1. Consult the :class:`QuietHoursFilter` to get a :data:`Decision`.
2. If ``defer``, do not deliver and emit a ``notify.deferred`` meta-event.
   The caller may retry later (the approval gate re-requests via TTL flow).
3. If ``deliver`` / ``deliver_low_urgency``, try the primary port.
   On :class:`NotificationError`, walk fallbacks in order.  The first
   success returns a :class:`NotificationReceipt`.
4. If every port fails, raise :class:`NotificationError`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import structlog

from coremind.errors import NotificationError
from coremind.notify.port import (
    ApprovalAction,
    ApprovalResponse,
    NotificationCategory,
    NotificationPort,
    NotificationReceipt,
)
from coremind.notify.quiet_hours import QuietHoursFilter

log = structlog.get_logger(__name__)

type _MetaWriter = Callable[[str, dict[str, Any]], Awaitable[None]]


class DeferredNotificationError(NotificationError):
    """Raised when a notification is held by the quiet-hours policy.

    Subclass of :class:`NotificationError` so existing ``except`` clauses
    continue to work, but type-specific callers can distinguish.
    """


class NotificationRouter:
    """Primary + fallback :class:`NotificationPort`.

    Args:
        primary: The primary port.
        fallbacks: Ordered list of fallback ports tried on primary failure.
        quiet_hours: Filter applied before any delivery attempt.
        journal_meta: Optional callable invoked with ``(meta_type, payload)``
            for each routing decision (deferred, delivered, fallback).  Wired
            to :meth:`coremind.action.journal.ActionJournal.append_meta` in
            production, left as ``None`` in tests.
    """

    id: str = "router"
    supports_callbacks: bool = True

    def __init__(
        self,
        primary: NotificationPort,
        fallbacks: list[NotificationPort],
        quiet_hours: QuietHoursFilter,
        *,
        journal_meta: _MetaWriter | None = None,
    ) -> None:
        self._primary = primary
        self._fallbacks = list(fallbacks)
        self._quiet = quiet_hours
        self._meta = journal_meta

    # ------------------------------------------------------------------
    # NotificationPort implementation
    # ------------------------------------------------------------------

    async def notify(
        self,
        *,
        message: str,
        category: NotificationCategory,
        actions: list[ApprovalAction] | None,
        intent_id: str | None,
        action_class: str | None = None,
    ) -> NotificationReceipt:
        """Deliver ``message`` honouring quiet hours and fallback order."""
        decision = self._quiet.decide(category=category, action_class=action_class)
        if decision == "defer":
            if self._meta is not None:
                await self._meta(
                    "notify.deferred",
                    {"intent_id": intent_id, "category": category},
                )
            log.info("notify.deferred", intent_id=intent_id, category=category)
            raise DeferredNotificationError(
                f"notification for intent {intent_id!r} deferred by quiet hours"
            )

        ports: list[NotificationPort] = [self._primary, *self._fallbacks]
        last_error: Exception | None = None
        for port in ports:
            try:
                receipt = await port.notify(
                    message=message,
                    category=category,
                    actions=actions,
                    intent_id=intent_id,
                    action_class=action_class,
                )
            except NotificationError as exc:
                last_error = exc
                log.warning(
                    "notify.port_failed",
                    port=port.id,
                    intent_id=intent_id,
                    error=str(exc),
                )
                if self._meta is not None:
                    await self._meta(
                        "notify.port_failed",
                        {
                            "port_id": port.id,
                            "intent_id": intent_id,
                            "error": str(exc),
                        },
                    )
                continue
            if self._meta is not None:
                await self._meta(
                    "notify.delivered",
                    {
                        "port_id": port.id,
                        "intent_id": intent_id,
                        "channel_message_id": receipt.channel_message_id,
                    },
                )
            return receipt

        last = last_error or NotificationError("no notification ports configured")
        raise NotificationError(f"no notification port accepted the message (last error: {last})")

    def subscribe_responses(self) -> AsyncIterator[ApprovalResponse]:
        """Multiplex responses from every callback-capable port."""
        return _multiplex([p for p in (self._primary, *self._fallbacks) if p.supports_callbacks])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _multiplex(
    ports: list[NotificationPort],
) -> AsyncIterator[ApprovalResponse]:
    """Yield approval responses from any port as they arrive.

    A minimal fair multiplexer: starts one task per port and yields whichever
    produces a value first.  Exits when every source iterator is exhausted.
    """

    async def _drain(port: NotificationPort, queue: asyncio.Queue[Any]) -> None:
        try:
            async for item in port.subscribe_responses():
                await queue.put((port.id, item))
        except Exception as exc:  # isolate per-port stream failures
            log.warning(
                "notify.subscribe_failed",
                port=port.id,
                error=str(exc),
            )
        finally:
            await queue.put((port.id, None))

    queue: asyncio.Queue[Any] = asyncio.Queue()
    tasks = [asyncio.create_task(_drain(p, queue)) for p in ports]
    alive = len(tasks)
    try:
        while alive:
            _port_id, item = await queue.get()
            if item is None:
                alive -= 1
                continue
            yield item
    finally:
        for t in tasks:
            t.cancel()
