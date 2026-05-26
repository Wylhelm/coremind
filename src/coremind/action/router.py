"""Action router — sends an intent down the right execution path.

The router is the single entrypoint the intention layer uses to dispatch an
:class:`Intent`.  It is responsible for:

1. Enforcing category overrides via the autonomy slider system
   (see :mod:`coremind.action.autonomy`).  Combines the user's per-domain
   trust (slider) with the LLM's confidence to produce the agency decision.
   Hard ASK/SAFE overrides and slider-based changes that differ from the
   LLM's assigned category are journaled as
   ``security.category.override_blocked`` meta-events for auditability.
2. Dispatching through the executor for ``safe`` / ``suggest`` categories.
3. Handing off to the approval gate for ``ask``.
4. Short-circuiting intents that carry no proposed action.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

from coremind.action.approvals import ApprovalGate
from coremind.action.autonomy import AutonomyConfig, resolve_agency
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
        autonomy_config: Per-domain autonomy slider configuration.
        user_ask_classes: Extra classes the user has declared as
            forced-``ask`` in config (merged into autonomy hard_ask).
    """

    def __init__(
        self,
        executor: Executor,
        approvals: ApprovalGate,
        intent_store: IntentStore,
        journal: ActionJournal,
        *,
        autonomy_config: AutonomyConfig | None = None,
        user_ask_classes: Iterable[str] = (),
    ) -> None:
        self._executor = executor
        self._approvals = approvals
        self._intents = intent_store
        self._journal = journal
        # Merge user_ask_classes into autonomy config's hard_ask.
        extra_ask = tuple(user_ask_classes)
        if autonomy_config is None:
            self._autonomy = AutonomyConfig(
                hard_ask=list(AutonomyConfig().hard_ask) + list(extra_ask),
            )
        elif extra_ask:
            self._autonomy = autonomy_config.model_copy(
                update={"hard_ask": list(autonomy_config.hard_ask) + list(extra_ask)},
            )
        else:
            self._autonomy = autonomy_config

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

        resolved_cat = resolve_agency(
            proposal.action_class,
            intent.confidence,
            self._autonomy,
        )

        if resolved_cat != intent.category:
            await self._journal.append_meta(
                "security.category.override_blocked",
                {
                    "intent_id": intent.id,
                    "action_class": proposal.action_class,
                    "original_category": intent.category,
                    "forced_category": resolved_cat,
                },
            )
            log.warning(
                "router.forced_category_override",
                intent_id=intent.id,
                action_class=proposal.action_class,
                original=intent.category,
                forced=resolved_cat,
            )
            intent.category = resolved_cat

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
