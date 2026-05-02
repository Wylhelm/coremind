"""Feedback evaluator — L7 port for evaluating actions against user feedback.

Counts approved, rejected, reversed, and dismissed intents/actions across
a reflection window.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from coremind.action.schemas import Action
from coremind.intention.schemas import Intent
from coremind.reflection.schemas import FeedbackEvaluationResult

log = structlog.get_logger(__name__)


class FeedbackEvaluator(Protocol):
    """Port for evaluating actions against user feedback.

    See :class:`coremind.reflection.loop.FeedbackEvaluator` for the
    canonical protocol definition.
    """

    async def evaluate(
        self,
        actions: list[Action],
        intents: list[Intent],
    ) -> FeedbackEvaluationResult:
        """Score how the user reacted to ``actions``."""
        ...


class FeedbackEvaluatorImpl:
    """Default :class:`FeedbackEvaluator` implementation.

    Counts feedback outcomes from the supplied intents and actions.

    The evaluation logic:
    - ``approved``: intents with status ``"approved"`` or actions with
      result status ``"ok"`` or ``"noop"``.
    - ``rejected``: intents with status ``"rejected_invalid_signature"``
      or actions with result status ``"permanent_failure"`` /
      ``"rejected_invalid_signature"``.
    - ``reversed``: actions whose result status indicates a user reversal
      (none currently have a dedicated reversal status; counted as actions
      with ``"transient_failure"`` that were later succeeded, or actions
      explicitly marked as reversed by the approval gate).
    - ``dismissed``: intents with status ``"expired"`` or ``"dismissed"``.
    """

    async def evaluate(
        self,
        actions: list[Action],
        intents: list[Intent],
    ) -> FeedbackEvaluationResult:
        """Evaluate actions against user feedback.

        Args:
            actions: Actions dispatched in the reflection window.
            intents: Intents generated in the reflection window.

        Returns:
            Aggregated feedback counts.
        """
        approved = 0
        rejected = 0
        reversed_count = 0
        dismissed = 0

        # Count from intents
        for intent in intents:
            status = intent.status
            if status == "approved":
                approved += 1
            elif status in ("rejected_invalid_signature",):
                rejected += 1
            elif status in ("expired", "dismissed"):
                dismissed += 1

        # Count from actions
        for action in actions:
            if action.result is None:
                continue
            result_status = action.result.status
            if result_status in ("ok", "noop"):
                approved += 1
            elif result_status in ("permanent_failure", "rejected_invalid_signature"):
                rejected += 1
            elif result_status == "transient_failure":
                # Transient failures may be retried; count them as
                # potential reversals for now.
                reversed_count += 1

        total = approved + rejected + reversed_count + dismissed
        log.info(
            "reflection.feedback.evaluated",
            evaluated=total,
            approved=approved,
            rejected=rejected,
            reversed=reversed_count,
            dismissed=dismissed,
        )

        return FeedbackEvaluationResult(
            evaluated=total,
            approved=approved,
            rejected=rejected,
            reversed=reversed_count,
            dismissed=dismissed,
        )
