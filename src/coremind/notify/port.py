"""Notification Port — the single abstraction CoreMind uses to reach the user.

A :class:`NotificationPort` exposes two operations:

- :meth:`NotificationPort.notify` — deliver a message to the user with zero
  or more approval actions.
- :meth:`NotificationPort.subscribe_responses` — yield
  :class:`ApprovalResponse` objects for ``ask``-class requests.

See `ARCHITECTURE.md §15.5` and `docs/phases/PHASE_3_INTENTION_ACTION.md §3.8`.

Multiple adapters may be registered.  The user declares a primary and an
ordered fallback list in config; :class:`NotificationRouter` implements the
"try primary, fall through to fallbacks on failure" policy and journals each
delivery outcome.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

type NotificationCategory = Literal["info", "suggest", "ask", "conversation"]


class ApprovalAction(BaseModel):
    """A user-selectable response for an ``ask``-class notification."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(min_length=1)
    value: str = Field(min_length=1)


class UserRef(BaseModel):
    """Identity claimed by the channel adapter for a user response."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    display_name: str = ""


class ApprovalResponse(BaseModel):
    """Result of the user responding to an ``ask``-class notification.

    Trust boundary: end-to-end authentication of an approval response is the
    responsibility of the originating channel adapter (e.g. Telegram bot
    membership + chat-id allowlist).  The journal entry recording the
    response is itself daemon-signed, but the ``responder`` field is only as
    trustworthy as the channel that produced it.
    """

    model_config = ConfigDict(frozen=True)

    intent_id: str = Field(min_length=1)
    decision: Literal["approve", "deny", "snooze"]
    snooze_seconds: int | None = Field(default=None, ge=1)
    note: str | None = None
    responder: UserRef
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NotificationReceipt(BaseModel):
    """Delivery receipt returned by :meth:`NotificationPort.notify`."""

    model_config = ConfigDict(frozen=True)

    port_id: str = Field(min_length=1)
    channel_message_id: str
    sent_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NotificationPort(Protocol):
    """Port implemented by every notification adapter.

    Attributes:
        id: Stable adapter identifier (``telegram``, ``dashboard``, …).
        supports_callbacks: ``True`` when this port can deliver
            :class:`ApprovalResponse` objects via :meth:`subscribe_responses`.
    """

    id: str
    supports_callbacks: bool

    async def notify(
        self,
        *,
        message: str,
        category: NotificationCategory,
        actions: list[ApprovalAction] | None,
        intent_id: str | None,
        action_class: str | None = None,
    ) -> NotificationReceipt:
        """Deliver ``message`` to the user and return a delivery receipt.

        Implementations raise :class:`coremind.errors.NotificationError` on
        transport failure so the router can try fallbacks.

        ``action_class`` is informational and used by the router to apply
        quiet-hours / focus-window exemptions for safety classes.  Adapters
        that have no per-class surface may safely ignore it.
        """
        ...

    def subscribe_responses(self) -> AsyncIterator[ApprovalResponse]:
        """Async-iterate every approval response received on this port.

        Ports that do not support callbacks (``supports_callbacks=False``)
        may return an iterator that immediately ends.
        """
        ...
