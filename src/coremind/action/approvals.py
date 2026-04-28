"""Approval gate for ``ask``-class intents.

Blocks execution of an :class:`~coremind.intention.schemas.Intent` until the
user responds through a :class:`~coremind.notify.port.NotificationPort`.

Semantics (per `ARCHITECTURE.md §15.3`):

- Default TTL is 24 h, configurable.
- Snoozing is explicit and bounded to **one** snooze per intent.
- Expiration never auto-executes: an expired intent emits
  ``approval.expired`` and stays ``expired`` forever.
- Every approval response is journaled as a signed meta-event.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import structlog

from coremind.action.journal import ActionJournal
from coremind.errors import ApprovalError
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import Intent
from coremind.notify.port import ApprovalAction, ApprovalResponse, NotificationPort
from coremind.notify.router import DeferredNotificationError

log = structlog.get_logger(__name__)

type Clock = Callable[[], datetime]

_DEFAULT_TTL = timedelta(hours=24)


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class _ApprovalExecutor(Protocol):
    """Minimal executor port used by the approval gate.

    Named with a leading underscore to avoid shadowing
    :class:`coremind.action.executor.Executor` for readers.  Phase 3 wires
    the real executor as the implementation of this port; the indirection
    keeps the gate testable in isolation.
    """

    async def execute(
        self,
        intent: Intent,
        *,
        notify: Literal["silent", "summary", "immediate"] = "immediate",
    ) -> Any:
        """Dispatch ``intent`` for execution."""
        ...


class ApprovalGate:
    """Manage the lifecycle of ``ask``-class intents.

    Args:
        notify_port: Notification port to surface approval requests.
        intent_store: Intent persistence.
        journal: Audit journal used to record every approval event.
        executor: Executor invoked when an approval is granted.
        ttl: Default time-to-live for a pending request.
        clock: Injectable clock.
    """

    def __init__(
        self,
        notify_port: NotificationPort,
        intent_store: IntentStore,
        journal: ActionJournal,
        executor: _ApprovalExecutor,
        *,
        ttl: timedelta = _DEFAULT_TTL,
        clock: Clock = _utc_now,
    ) -> None:
        self._notify = notify_port
        self._intents = intent_store
        self._journal = journal
        self._executor = executor
        self._ttl = ttl
        self._clock = clock
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Request / response flow
    # ------------------------------------------------------------------

    async def request(self, intent: Intent, *, ttl: timedelta | None = None) -> None:
        """Surface *intent* for user approval and persist its pending state.

        Args:
            intent: The intent to hold for approval.  Must have a
                ``proposed_action``.
            ttl: Override for the default request TTL.

        Raises:
            ApprovalError: If ``proposed_action`` is missing.
        """
        if intent.proposed_action is None:
            raise ApprovalError(
                f"intent {intent.id!r} has no proposed_action; cannot request approval"
            )

        intent.status = "pending_approval"
        intent.expires_at = self._clock() + (ttl or self._ttl)
        await self._intents.save(intent)

        actions = [
            ApprovalAction(label="✅ Approve", value="approve"),
            ApprovalAction(label="❌ Deny", value="deny"),
            ApprovalAction(label="⏸ Snooze 1h", value="snooze:3600"),
        ]
        message = _format_request(intent)
        try:
            receipt = await self._notify.notify(
                message=message,
                category="ask",
                actions=actions,
                intent_id=intent.id,
                action_class=intent.proposed_action.action_class,
            )
        except DeferredNotificationError:
            # Quiet hours / focus window deferred the notification.  Roll
            # back the pending state so the intent can be re-requested in
            # the next active window rather than stranded in
            # ``pending_approval`` with no notification ever sent.
            intent.status = "pending"
            intent.expires_at = None
            await self._intents.save(intent)
            await self._journal.append_meta(
                "approval.deferred",
                {"intent_id": intent.id},
            )
            log.info("approval.deferred", intent_id=intent.id)
            raise
        await self._journal.append_meta(
            "approval.requested",
            {
                "intent_id": intent.id,
                "port_id": receipt.port_id,
                "channel_message_id": receipt.channel_message_id,
                "expires_at": intent.expires_at.isoformat(),
            },
        )
        log.info(
            "approval.requested",
            intent_id=intent.id,
            port=receipt.port_id,
            expires_at=intent.expires_at.isoformat(),
        )

    async def handle_response(self, response: ApprovalResponse) -> None:
        """Apply a user response to a pending intent.

        Args:
            response: The signed approval response.

        Raises:
            ApprovalError: If the intent is unknown, not pending, already
                expired, or the requested action is invalid.
        """
        async with self._lock:
            intent = await self._intents.get(response.intent_id)
            if intent is None:
                raise ApprovalError(f"unknown intent {response.intent_id!r}")

            now = self._clock()
            expired_now = (
                intent.expires_at is not None
                and now >= intent.expires_at
                and intent.status == "pending_approval"
            )
            if expired_now:
                intent.status = "expired"
                await self._intents.save(intent)
                await self._journal.append_meta(
                    "approval.expired",
                    {"intent_id": intent.id},
                )
                raise ApprovalError(f"intent {intent.id!r} expired before response was received")

            if intent.status != "pending_approval":
                raise ApprovalError(
                    f"intent {intent.id!r} is not pending_approval (status={intent.status!r})"
                )

            if response.decision == "approve":
                intent.status = "approved"
                intent.human_feedback = response.note
                await self._intents.save(intent)
                await self._journal.append_meta(
                    "approval.response",
                    {
                        "intent_id": intent.id,
                        "decision": "approve",
                        "responder": response.responder.id,
                        "note": response.note or "",
                    },
                )
                log.info("approval.approved", intent_id=intent.id)
                # Execution is performed by the daemon's approved-intent
                # dispatcher (see :meth:`dispatch_approved`).  This keeps
                # CLI-originated and channel-originated approvals on a
                # single execution path with no race against the gate.
                return

            if response.decision == "deny":
                intent.status = "rejected"
                intent.human_feedback = response.note
                await self._intents.save(intent)
                await self._journal.append_meta(
                    "approval.response",
                    {
                        "intent_id": intent.id,
                        "decision": "deny",
                        "responder": response.responder.id,
                        "note": response.note or "",
                    },
                )
                log.info("approval.denied", intent_id=intent.id)
                return

            # snooze branch
            if intent.snooze_count >= 1:
                # Second snooze attempt — refuse.
                await self._journal.append_meta(
                    "approval.snooze_refused",
                    {"intent_id": intent.id, "responder": response.responder.id},
                )
                raise ApprovalError(f"intent {intent.id!r} has already been snoozed once")

            snooze_seconds = response.snooze_seconds or 3600
            intent.snooze_count += 1
            intent.expires_at = now + timedelta(seconds=snooze_seconds)
            intent.status = "pending_approval"
            await self._intents.save(intent)
            await self._journal.append_meta(
                "approval.response",
                {
                    "intent_id": intent.id,
                    "decision": "snooze",
                    "snooze_seconds": snooze_seconds,
                    "responder": response.responder.id,
                    "note": response.note or "",
                },
            )
            log.info(
                "approval.snoozed",
                intent_id=intent.id,
                snooze_seconds=snooze_seconds,
            )

    # ------------------------------------------------------------------
    # Expiration sweep
    # ------------------------------------------------------------------

    async def expire_stale(self) -> int:
        """Expire every pending approval whose TTL has elapsed.

        Returns:
            The number of intents moved into the ``expired`` state.
        """
        now = self._clock()
        pending = await self._intents.list(status="pending_approval")
        expired = 0
        for intent in pending:
            if intent.expires_at is None or now < intent.expires_at:
                continue
            intent.status = "expired"
            await self._intents.save(intent)
            await self._journal.append_meta(
                "approval.expired",
                {"intent_id": intent.id},
            )
            log.info("approval.expired", intent_id=intent.id)
            expired += 1
        return expired

    # ------------------------------------------------------------------
    # Approved-intent dispatch
    # ------------------------------------------------------------------

    async def dispatch_approved(self) -> int:
        """Execute every intent currently in the ``approved`` state.

        Approval responses (CLI, Telegram, dashboard, …) merely transition an
        intent to ``approved``.  Execution is performed here so all surfaces
        share a single, race-free dispatch path.

        Returns:
            The number of intents handed off to the executor.
        """
        async with self._lock:
            approved = await self._intents.list(status="approved", limit=1000)
        dispatched = 0
        for intent in approved:
            if intent.proposed_action is None:
                # Pure question that someone marked approved — nothing to do.
                intent.status = "done"
                await self._intents.save(intent)
                continue
            try:
                await self._executor.execute(intent, notify="immediate")
            except Exception:  # isolate per-intent failures
                log.exception(
                    "approval.dispatch_failed",
                    intent_id=intent.id,
                )
                continue
            dispatched += 1
        return dispatched


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_request(intent: Intent) -> str:
    """Render a short human-readable approval request message."""
    proposal = intent.proposed_action
    if proposal is None:  # pragma: no cover — enforced by caller
        raise ApprovalError(f"intent {intent.id!r} has no proposed_action")
    lines = [
        f"CoreMind proposes an action ({intent.category}):",
        f"• operation: {proposal.operation}",
        f"• class:     {proposal.action_class}",
        f"• why:       {intent.question.text}",
        f"• expected:  {proposal.expected_outcome}",
    ]
    if proposal.reversal:
        lines.append(f"• reversal:  {proposal.reversal}")
    lines.append(f"• confidence: {intent.confidence:.2f}")
    return "\n".join(lines)
