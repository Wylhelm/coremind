"""Action router — sends an intent down the right execution path.

The router is the single entrypoint the intention layer uses to dispatch an
:class:`Intent`.  It is responsible for:

1. Enforcing category overrides based on the action class
   (see :mod:`coremind.action.action_classes`).  The LLM-assigned category
   is the default, but SAFE / SUGGEST / ASK classes are always enforced.
   Overrides are journaled as ``security.category.override_blocked``
   meta-events so they are auditable.
2. Dispatching through the executor for ``safe`` / ``suggest`` categories.
3. Handing off to the approval gate for ``ask``.
4. Short-circuiting intents that carry no proposed action.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

from coremind.action.action_classes import get_forced_category
from coremind.action.approvals import ApprovalGate
from coremind.action.executor import Executor
from coremind.action.journal import ActionJournal
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import Intent

log = structlog.get_logger(__name__)


class ActionRouter:
    """Route :class:`Intent` objects by category and class.

    Args:
        executor: Executor for ``safe`` and ``suggest`` categories.
        approvals: Approval gate for ``ask`` categories.
        intent_store: Intent persistence.
        journal: Audit journal used for forced-category meta-events.
        user_ask_classes: Extra classes the user has declared as
            forced-``ask`` in config.
    """

    def __init__(
        self,
        executor: Executor,
        approvals: ApprovalGate,
        intent_store: IntentStore,
        journal: ActionJournal,
        *,
        user_ask_classes: Iterable[str] = (),
    ) -> None:
        self._executor = executor
        self._approvals = approvals
        self._intents = intent_store
        self._journal = journal
        self._user_ask_classes = tuple(user_ask_classes)

    async def route(self, intent: Intent) -> None:
        """Dispatch ``intent`` according to its (possibly forced) category.

        Args:
            intent: The intent to route.  ``status`` is updated in-place.
        """
        proposal = intent.proposed_action
        if proposal is None:
            # Pure question — no action surface; stored for reflection.
            await self._intents.save(intent)
            log.debug("router.pure_question", intent_id=intent.id)
            return

        forced_cat = get_forced_category(
            proposal.action_class, user_ask_classes=self._user_ask_classes
        )
        if forced_cat is not None and intent.category != forced_cat:
            await self._journal.append_meta(
                "security.category.override_blocked",
                {
                    "intent_id": intent.id,
                    "action_class": proposal.action_class,
                    "original_category": intent.category,
                    "forced_category": forced_cat,
                },
            )
            log.warning(
                "router.forced_category_override",
                intent_id=intent.id,
                action_class=proposal.action_class,
                original=intent.category,
                forced=forced_cat,
            )
            intent.category = forced_cat

        await self._intents.save(intent)

        if intent.category == "safe":
            await self._executor.execute(intent, notify="silent")
            return
        if intent.category == "suggest":
            await self._executor.execute_with_grace(intent)
            return
        if intent.category == "conversation":
            await self._executor.start_conversation(intent)
            return
        # ask
        await self._approvals.request(intent)
