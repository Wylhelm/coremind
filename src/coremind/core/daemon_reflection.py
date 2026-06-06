"""L7 Reflection Loop wiring for the CoreMind daemon.

Extracted from :mod:`coremind.core.daemon` to keep the orchestrator lean.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from coremind.action.journal import ActionJournal
from coremind.config import DaemonConfig
from coremind.intention.persistence import IntentStore
from coremind.memory.narrative import NarrativeMemory
from coremind.memory.procedural import ProceduralMemory
from coremind.notify.router import NotificationRouter
from coremind.reasoning.llm import LLM
from coremind.reasoning.persistence import JsonlCyclePersister
from coremind.reflection.calibration import (
    Calibrator,
    InMemoryCalibrationStore,
)
from coremind.reflection.evaluator import (
    BasicConditionResolver,
    InMemoryPredictionEvaluationStore,
    PredictionEvaluatorImpl,
)
from coremind.reflection.feedback import (
    FeedbackEvaluatorImpl,
)
from coremind.reflection.loop import (
    ReflectionLoop,
    ReflectionLoopConfig,
)
from coremind.reflection.notify import (
    ReflectionNotifier,
)
from coremind.reflection.report import (
    InMemoryReportStore,
    MarkdownReportProducer,
)
from coremind.reflection.rule_learner import (
    InMemoryCandidateLedger,
    InMemoryRuleProposalStore,
    RuleLearnerImpl,
)
from coremind.reflection.store import (
    SurrealReflectionStore,
)
from coremind.world.store import WorldStore

log = structlog.get_logger(__name__)


async def build_reflection_system(
    *,
    config: DaemonConfig,
    world_store: WorldStore,
    intents: IntentStore,
    journal: ActionJournal,
    reasoning_journal: Path,
    narrative_memory: NarrativeMemory,
    llm: LLM,
    notify_router: NotificationRouter,
    procedural_memory: ProceduralMemory,
) -> tuple[ReflectionLoop, object]:
    """Construct and start the L7 Reflection Loop.

    Tries SurrealDB-backed prediction/calibration stores first, falling
    back to in-memory stores if SurrealDB is unavailable.  The report
    store is always in-memory (surfaced by the dashboard via the
    returned reference).

    Args:
        config: Validated daemon configuration.
        world_store: World Model store used by the prediction evaluator
            as evidence history.
        intents: Intent store used by the feedback evaluator.
        journal: Action journal used by the feedback evaluator.
        reasoning_journal: Path to the JSONL reasoning-cycle log; passed
            as the cycle source for the reflection loop.
        narrative_memory: Narrative identity store; each reflection cycle
            refreshes the user's persistent life context through it.
        llm: LLM instance used for narrative refresh.
        notify_router: Notification router used by the notifier to
            deliver reflection reports.
        procedural_memory: Hash-chained procedural rule store (already
            loaded by the caller); used as the rule source for the
            rule learner.

    Returns:
        A tuple of ``(reflection_loop, report_store)``.  The
        ``reflection_loop`` is already started; the caller must call
        ``.stop()`` on shutdown.  The ``report_store`` implements the
        ``list_reports(*, limit)`` protocol expected by the dashboard.
    """
    # Shared in-memory stores reused in the fallback path.
    _in_memory_eval_store = InMemoryPredictionEvaluationStore()
    _in_memory_cal_store = InMemoryCalibrationStore()

    # Try SurrealDB-backed reflection store; fall back to in-memory
    # stores if SurrealDB is unavailable.
    reflection_store_ok = False
    try:
        reflection_store = SurrealReflectionStore(
            url=config.world_db_url,
            username=config.world_db_username,
            password=config.world_db_password,
        )
        await reflection_store.connect()
        await reflection_store.apply_schema()
        reflection_store_ok = True
    except Exception as exc:
        log.warning(
            "daemon.reflection_store_unavailable",
            detail="SurrealDB not available for reflection store; "
            "falling back to in-memory stores. Reflection data "
            "will not persist across restarts.",
            error=str(exc),
        )
        reflection_store = None

    # Build prediction evaluator with BasicConditionResolver.
    if reflection_store_ok:
        prediction_evaluator = PredictionEvaluatorImpl(
            history=world_store,  # type: ignore[arg-type]
            resolver=BasicConditionResolver(),
            store=reflection_store.predictions(),  # type: ignore[union-attr]
        )
        calibration_updater = Calibrator(
            eval_store=reflection_store.predictions(),  # type: ignore[union-attr]
            cal_store=reflection_store.calibration(),  # type: ignore[union-attr]
            layer="reasoning",
        )
    else:
        prediction_evaluator = PredictionEvaluatorImpl(
            history=world_store,  # type: ignore[arg-type]
            resolver=BasicConditionResolver(),
            store=_in_memory_eval_store,
        )
        calibration_updater = Calibrator(
            eval_store=_in_memory_eval_store,
            cal_store=_in_memory_cal_store,
            layer="reasoning",
        )

    # Build feedback evaluator.
    feedback_evaluator = FeedbackEvaluatorImpl()

    # Build rule learner with in-memory stores.
    rule_learner = RuleLearnerImpl(
        rule_source=procedural_memory,
        ledger=InMemoryCandidateLedger(),
        proposal_store=InMemoryRuleProposalStore(),
    )

    # Build report producer + in-memory report store (shared with dashboard).
    report_producer = MarkdownReportProducer(
        proposal_store=InMemoryRuleProposalStore(),
    )
    report_store = InMemoryReportStore()

    # Build reflection notifier (sends reports via Telegram/dashboard).
    reflection_notifier = ReflectionNotifier(
        port=notify_router,
        dashboard_url=f"http://10.0.0.253:{config.dashboard.port}/reflection",
    )

    reflection_loop = ReflectionLoop(
        cycle_source=JsonlCyclePersister(reasoning_journal),
        intent_source=intents,
        action_feed=journal,
        prediction_evaluator=prediction_evaluator,
        feedback_evaluator=feedback_evaluator,
        calibration_updater=calibration_updater,
        rule_learner=rule_learner,
        report_producer=report_producer,
        notifier=reflection_notifier,
        report_store=report_store,
        narrative_state=narrative_memory,
        narrative_llm=llm,
        config=ReflectionLoopConfig(
            interval_seconds=86400,  # 24 hours
            window_days=1,
            notify_on_cycle=True,
        ),
    )
    reflection_loop.start()
    log.info("daemon.reflection_loop_started")

    return reflection_loop, report_store
