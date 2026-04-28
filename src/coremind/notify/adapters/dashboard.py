"""In-process dashboard notification adapter.

Used by the Phase 4 web dashboard.  Implemented here because the port
contract lives in Phase 3.  The adapter simply stores notifications in
memory and exposes an async queue of approval responses pushed by the
dashboard UI (or tests).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import structlog
from pydantic import BaseModel, ConfigDict

from coremind.notify.port import (
    ApprovalAction,
    ApprovalResponse,
    NotificationCategory,
    NotificationReceipt,
)

log = structlog.get_logger(__name__)


class StoredNotification(BaseModel):
    """A notification held for the dashboard UI to display."""

    model_config = ConfigDict(frozen=True)

    id: str
    message: str
    category: NotificationCategory
    actions: list[ApprovalAction]
    intent_id: str | None
    sent_at: datetime


class DashboardNotificationPort:
    """In-memory implementation of :class:`NotificationPort`.

    Notifications are held in :attr:`history` for later inspection.  Approval
    responses are injected via :meth:`submit_response`.
    """

    id: str = "dashboard"
    supports_callbacks: bool = True

    def __init__(self) -> None:
        self._history: list[StoredNotification] = []
        # Pending entries keyed by ``intent_id`` so the dashboard can drop a
        # row as soon as a matching :class:`ApprovalResponse` is submitted,
        # rather than letting the lifetime ``history`` masquerade as a queue
        # of unresolved approvals.
        self._pending: dict[str, StoredNotification] = {}
        self._responses: asyncio.Queue[ApprovalResponse] = asyncio.Queue()

    @property
    def history(self) -> list[StoredNotification]:
        """Return every notification delivered so far (lifetime log)."""
        return list(self._history)

    def pending(self) -> list[StoredNotification]:
        """Return notifications still awaiting a user decision.

        An entry is considered pending until a matching ``ApprovalResponse``
        (same ``intent_id``) flows through :meth:`submit_response`.  Used by
        the dashboard's overview counter and intents page so they reflect
        live state instead of a monotonically growing audit list.
        """
        # Insertion order preserved; oldest first.
        return list(self._pending.values())

    async def notify(
        self,
        *,
        message: str,
        category: NotificationCategory,
        actions: list[ApprovalAction] | None,
        intent_id: str | None,
        action_class: str | None = None,
    ) -> NotificationReceipt:
        """Store the notification and return a receipt."""
        _ = action_class  # informational; the dashboard surfaces all classes
        note = StoredNotification(
            id=uuid.uuid4().hex,
            message=message,
            category=category,
            actions=list(actions or ()),
            intent_id=intent_id,
            sent_at=datetime.now(UTC),
        )
        self._history.append(note)
        # Only ``ask``-class notifications carry an intent that a response
        # could resolve; ``info``/``suggest`` never become pending.
        if intent_id is not None and category == "ask":
            self._pending[intent_id] = note
        log.info("notify.dashboard.sent", category=category, intent_id=intent_id)
        return NotificationReceipt(
            port_id=self.id,
            channel_message_id=note.id,
            sent_at=note.sent_at,
        )

    async def submit_response(self, response: ApprovalResponse) -> None:
        """Inject an approval response and clear the matching pending entry."""
        self._pending.pop(response.intent_id, None)
        await self._responses.put(response)

    async def subscribe_responses(self) -> AsyncIterator[ApprovalResponse]:
        """Yield approval responses as they are submitted."""
        while True:
            item = await self._responses.get()
            yield item
