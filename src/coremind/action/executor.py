"""Executor — dispatches :class:`Action` objects to effector plugins.

The executor journals every action **before** dispatch so the audit trail is
always at least as complete as the set of attempted effects (see
``docs/phases/PHASE_3_INTENTION_ACTION.md §3.5``).

Port
----

The executor consumes a minimal :class:`EffectorPort` protocol rather than the
full plugin host, keeping this module test-friendly.  In production the port
is backed by :class:`coremind.plugin_host.registry.PluginRegistry` and a
gRPC call to the plugin's ``InvokeAction`` RPC.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import structlog

from coremind.action.journal import ActionJournal
from coremind.action.schemas import Action, ActionResult
from coremind.errors import ActionError
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import Intent
from coremind.notify.port import NotificationPort
from coremind.world.model import JsonValue

log = structlog.get_logger(__name__)

type Clock = Callable[[], datetime]

_DEFAULT_SUGGEST_GRACE = timedelta(seconds=30)


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


class EffectorPort(Protocol):
    """Minimal port for invoking an effector plugin."""

    async def invoke(self, action: Action) -> ActionResult:
        """Execute ``action`` and return the outcome."""
        ...


class EffectorNotFoundError(ActionError):
    """Raised when no effector is registered for an operation."""


class EffectorResolver(Protocol):
    """Resolve an ``Action.operation`` to an :class:`EffectorPort`.

    The daemon-wired implementation consults the plugin registry and the
    effector's declared ``accepts_operations``; returns ``None`` if no plugin
    can service the operation.
    """

    def __call__(self, operation: str, /) -> EffectorPort | None:
        """Return the effector port for ``operation`` or ``None``."""
        ...


class Executor:
    """Central dispatcher for :class:`Action` objects.

    Args:
        journal: Audit journal (the executor writes BEFORE invoking plugins).
        intent_store: Intent persistence — status transitions are saved here.
        resolver: Effector resolution function.
        notify_port: Optional notification port used for ``suggest``/``safe``
            user notifications.  When absent, notifications are skipped.
        suggest_grace: Grace window before a ``suggest`` action executes;
            user cancellation during this window aborts dispatch.
        clock: Injectable clock.
    """

    def __init__(
        self,
        journal: ActionJournal,
        intent_store: IntentStore,
        resolver: EffectorResolver,
        *,
        notify_port: NotificationPort | None = None,
        suggest_grace: timedelta = _DEFAULT_SUGGEST_GRACE,
        clock: Clock = _utc_now,
    ) -> None:
        self._journal = journal
        self._intents = intent_store
        self._resolver = resolver
        self._notify = notify_port
        self._suggest_grace = suggest_grace
        self._clock = clock
        self._cancelled: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        intent: Intent,
        *,
        notify: Literal["silent", "summary", "immediate"] = "silent",
    ) -> Action | None:
        """Execute ``intent.proposed_action`` now.

        Args:
            intent: The intent carrying the action to execute.  Its status
                is updated in-place and persisted.
            notify: Notification verbosity.  ``silent`` skips user contact,
                ``summary`` writes a ``notify.summary`` meta-event only,
                ``immediate`` posts through the notification port.

        Returns:
            The dispatched :class:`Action`, or ``None`` if the intent carries
            no proposal (pure question).
        """
        if intent.proposed_action is None:
            log.debug("executor.no_proposal", intent_id=intent.id)
            return None

        action = _build_action(intent, self._clock())
        intent.status = "executing"
        await self._intents.save(intent)

        # Journal BEFORE dispatch — commitment to intent precedes side effect.
        await self._journal.append(action)

        effector = self._resolver(action.operation)
        if effector is None:
            await self._record_failure(action, intent, "no_effector")
            raise EffectorNotFoundError(
                f"no effector registered for operation {action.operation!r}"
            )

        try:
            result = await effector.invoke(action)
        except Exception as exc:
            await self._record_failure(action, intent, f"effector_raised: {exc}")
            raise ActionError(f"effector for {action.operation!r} raised: {exc}") from exc

        action.result = result
        await self._journal.update_result(action)
        intent.status = "done" if result.status in {"ok", "noop"} else "failed"
        await self._intents.save(intent)

        if notify == "immediate" and self._notify is not None:
            await self._post_notification(action, intent)

        log.info(
            "executor.done",
            action_id=action.id,
            intent_id=intent.id,
            status=result.status,
        )
        return action

    async def start_conversation(self, intent: Intent) -> str | None:
        """Open a conversation about this intent AND schedule its action.

        Sends the question as an open-ended message, then executes the
        proposed action after a longer grace window (2 min vs 30s for suggest).
        The user can reply to cancel or modify.
        """
        if self._notify is None:
            return None

        intent.status = "conversation"
        await self._intents.save(intent)
        conv_id = f"conv_{intent.id[:20]}"
        message = _format_conversation(intent)
        await self._notify.notify(
            message=message,
            category="conversation",
            actions=None,
            intent_id=intent.id,
        )

        # Execute the action after a longer grace period (2 min)
        # This gives the user time to respond and cancel if needed
        action = await self.execute_with_grace(intent, grace=timedelta(seconds=120))
        if action is None:
            log.info("executor.conversation_action_cancelled", intent_id=intent.id)
        else:
            log.info("executor.conversation_action_executed", intent_id=intent.id)

        return conv_id

    async def execute_with_grace(
        self,
        intent: Intent,
        *,
        grace: timedelta | None = None,
    ) -> Action | None:
        """Surface ``intent`` to the user, wait out the grace window, then execute.

        The user may call :meth:`cancel` during the grace window to abort
        dispatch.  Cancellation flips the intent to ``rejected``.

        Args:
            intent: The intent to handle.
            grace: Grace period override.

        Returns:
            The dispatched :class:`Action`, or ``None`` if cancelled or there
            is no proposal.
        """
        if intent.proposed_action is None:
            return None

        await self._intents.save(intent)
        # Notify the user now, with an implicit "cancel within N seconds" window.
        if self._notify is not None:
            grace_s = int((grace or self._suggest_grace).total_seconds())
            await self._notify.notify(
                message=_format_suggest(intent, grace_s),
                category="suggest",
                actions=None,
                intent_id=intent.id,
            )

        try:
            await asyncio.sleep((grace or self._suggest_grace).total_seconds())
        except asyncio.CancelledError:
            intent.status = "rejected"
            await self._intents.save(intent)
            raise

        if intent.id in self._cancelled:
            self._cancelled.discard(intent.id)
            intent.status = "rejected"
            await self._intents.save(intent)
            await self._journal.append_meta(
                "action.cancelled_in_grace",
                {"intent_id": intent.id},
            )
            log.info("executor.cancelled_in_grace", intent_id=intent.id)
            return None

        return await self.execute(intent, notify="summary")

    def cancel(self, intent_id: str) -> None:
        """Request cancellation of a ``suggest`` intent currently in grace."""
        self._cancelled.add(intent_id)

    async def reverse(self, action_id: str) -> Action:
        """Dispatch the reversal operation declared on ``action_id``.

        Looks up the original action in the journal, constructs a new
        :class:`Action` targeting its reversal operation, signs it, journals
        it, and invokes the resolved effector.

        Args:
            action_id: Identifier of the original action to undo.

        Returns:
            The dispatched reversal :class:`Action`.

        Raises:
            ActionError: When the original action is missing, declared no
                reversal, or the effector raises.
        """
        original = await self._journal.find_action(action_id)
        if original is None:
            raise ActionError(f"action {action_id!r} not found in journal")
        if original.result is None or original.result.reversed_by_operation is None:
            raise ActionError(f"action {action_id!r} declared no reversal")

        reversal_op = original.result.reversed_by_operation
        reversal_params: dict[str, JsonValue] = dict(original.result.reversal_parameters or {})

        action = Action(
            id=uuid.uuid4().hex,
            intent_id=original.intent_id,
            timestamp=self._clock(),
            category="safe",
            operation=reversal_op,
            parameters=reversal_params,
            action_class=original.action_class,
            expected_outcome=f"reverses action {action_id}",
            reversal=None,
            confidence=original.confidence,
        )

        await self._journal.append(action)
        await self._journal.append_meta(
            "action.reversed",
            {"original_action_id": action_id, "reversal_action_id": action.id},
        )

        effector = self._resolver(reversal_op)
        if effector is None:
            action.result = ActionResult(
                action_id=action.id,
                status="permanent_failure",
                message="no_effector_for_reversal",
                completed_at=self._clock(),
            )
            await self._journal.update_result(action)
            raise EffectorNotFoundError(
                f"no effector registered for reversal operation {reversal_op!r}"
            )

        try:
            result = await effector.invoke(action)
        except Exception as exc:
            action.result = ActionResult(
                action_id=action.id,
                status="permanent_failure",
                message=f"effector_raised: {exc}",
                completed_at=self._clock(),
            )
            await self._journal.update_result(action)
            raise ActionError(f"reversal effector raised: {exc}") from exc

        action.result = result
        await self._journal.update_result(action)
        log.info(
            "executor.reversed",
            original_action_id=action_id,
            reversal_action_id=action.id,
            status=result.status,
        )
        return action

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _record_failure(self, action: Action, intent: Intent, reason: str) -> None:
        """Finalise an action whose dispatch failed pre- or post-invoke."""
        action.result = ActionResult(
            action_id=action.id,
            status="permanent_failure",
            message=reason,
            completed_at=self._clock(),
        )
        try:
            await self._journal.update_result(action)
        except Exception:
            log.exception("executor.update_result_failed", action_id=action.id)
        intent.status = "failed"
        await self._intents.save(intent)
        log.error(
            "executor.failed",
            action_id=action.id,
            intent_id=intent.id,
            reason=reason,
        )

    async def _post_notification(self, action: Action, intent: Intent) -> None:
        """Send a post-execution notification for ``immediate`` mode."""
        if self._notify is None:  # pragma: no cover — guarded by caller
            return
        msg = _format_execution_summary(action, intent)
        try:
            await self._notify.notify(
                message=msg,
                category="info",
                actions=None,
                intent_id=intent.id,
            )
        except Exception:
            log.warning("executor.notify_failed", intent_id=intent.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_action(intent: Intent, now: datetime) -> Action:
    """Construct an :class:`Action` from the intent's proposed action."""
    if intent.proposed_action is None:  # pragma: no cover — caller ensures
        raise ActionError(f"intent {intent.id!r} has no proposed_action")
    proposal = intent.proposed_action
    return Action(
        id=uuid.uuid4().hex,
        intent_id=intent.id,
        timestamp=now,
        category=intent.category,
        operation=proposal.operation,
        parameters=dict(proposal.parameters),
        action_class=proposal.action_class,
        expected_outcome=proposal.expected_outcome,
        reversal=proposal.reversal,
        confidence=intent.confidence,
    )


def _format_conversation(intent: Intent) -> str:
    """Render a natural user-facing message from the proposed action — NOT the internal question."""
    proposal = intent.proposed_action
    if proposal is None:
        return intent.question.text
    # Use expected_outcome as the user message (actions describe what will happen)
    outcome = proposal.expected_outcome or intent.question.text
    # Remove robotic "User receives..." prefix if present
    for prefix in ("User receives ", "User will receive ", "Guillaume receives "):
        if outcome.lower().startswith(prefix.lower()):
            outcome = outcome[len(prefix) :]
            break
    return outcome.strip()


def _format_suggest(intent: Intent, grace_seconds: int) -> str:
    """Render a suggest-category notification — natural action description, not internal question."""
    proposal = intent.proposed_action
    if proposal is None:
        raise ActionError(f"intent {intent.id!r} has no proposed_action")
    why = proposal.expected_outcome or intent.question.text
    # Clean up robotic phrasing
    for prefix in (
        "User receives ",
        "User will receive ",
        "Guillaume receives ",
        "The user gets ",
        "Guillaume gets ",
    ):
        if why.lower().startswith(prefix.lower()):
            why = why[len(prefix) :]
            break
    return f"{why.strip()}\n\nJe vais vérifier ça automatiquement. Dis-moi si tu veux que j'annule."


def _format_execution_summary(action: Action, intent: Intent) -> str:
    """Render a one-line summary of a completed action."""
    status = action.result.status if action.result else "dispatched"
    return (
        f"CoreMind executed {action.operation} ({action.category}) "
        f"for intent {intent.id[:8]}: {status}"
    )
