"""CoreMind daemon — top-level orchestration entry point.

Wires together all subsystems:

    L1 Plugin Host  →  EventBus  →  L2 World Store
                                    L5 Intention / L6 Action (optional)

:class:`CoreMindDaemon` is intentionally thin: it constructs the components,
threads their dependencies together, and drives the ingest loop.  No business
logic lives here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from coremind.reflection.loop import ReflectionLoop

from coremind.action.approvals import ApprovalGate
from coremind.action.effectors import (
    CalendarEffector,
    EffectorRegistry,
    GmailEffector,
    HomeAssistantEffector,
    NotificationEffector,
    VikunjaEffector,
)
from coremind.action.executor import Executor
from coremind.action.journal import ActionJournal
from coremind.action.router import ActionRouter
from coremind.config import DaemonConfig, DashboardConfig, load_config
from coremind.conversation.handler import ConversationHandler
from coremind.conversation.store import ConversationStore
from coremind.core.event_bus import EventBus
from coremind.crypto.signatures import ensure_daemon_keypair
from coremind.dashboard import (
    DashboardAuth,
    DashboardDataSources,
    DashboardServer,
)
from coremind.errors import SignatureError, StoreError
from coremind.intention.loop import IntentionLoop, IntentionLoopConfig
from coremind.intention.persistence import IntentStore
from coremind.memory.narrative import NarrativeMemory
from coremind.memory.procedural import ProceduralMemory
from coremind.notify.adapters.dashboard import DashboardNotificationPort
from coremind.notify.port import NotificationPort, UserRef
from coremind.notify.quiet_hours import QuietHoursFilter, QuietHoursPolicy
from coremind.notify.router import NotificationRouter
from coremind.plugin_host.registry import PluginRegistry
from coremind.plugin_host.server import PluginHostServer
from coremind.reasoning.llm import LLM, LayerConfig, LLMConfig
from coremind.reasoning.loop import ReasoningLoop, ReasoningLoopConfig
from coremind.reasoning.persistence import JsonlCyclePersister
from coremind.world.model import WorldEventRecord
from coremind.world.store import WorldStore

log = structlog.get_logger(__name__)


# Sweep interval for the approval-expiration scheduler.  Pending approvals
# carry their own TTL (default 24 h); the sweep merely transitions any whose
# deadline has elapsed since the last tick.
_APPROVAL_EXPIRER_INTERVAL_SECONDS = 60.0

# Poll interval for the approved-intent dispatcher.  Approval responses
# (CLI, Telegram, dashboard, …) flip an intent to ``approved``; this loop
# picks them up and hands them to the executor.
_APPROVED_DISPATCHER_INTERVAL_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Internal port — allows test doubles without depending on WorldStore directly
# ---------------------------------------------------------------------------


class _StorePort(Protocol):
    """Minimal interface consumed by the ingest loop."""

    async def apply_event(self, event: WorldEventRecord) -> None:
        """Persist *event* to the World Model."""
        ...


# ---------------------------------------------------------------------------
# Per-event ingest logic (module-level for direct testability)
# ---------------------------------------------------------------------------


async def _handle_event(event: WorldEventRecord, world_store: _StorePort) -> None:
    """Persist a single event from the EventBus to the World Model.

    Meta-events (``signature=None``) are silently skipped; they are internal
    bus bookkeeping and must never land in L2.

    Args:
        event: The event to persist.
        world_store: The World Model store to write to.
    """
    if event.signature is None:
        log.debug("ingest.meta_event_skipped", attribute=event.attribute)
        return

    # Invariant: WorldStore.apply_event wraps all DB-level failures in StoreError
    # and all signature failures in SignatureError.  Any other exception is an
    # unhandled bug in the store adapter and should crash the ingest loop so
    # it surfaces immediately rather than silently corrupting state.
    try:
        await world_store.apply_event(event)
    except SignatureError:
        log.warning(
            "ingest.bad_signature",
            plugin=event.source,
            event_id=event.id,
        )
    except StoreError:
        log.error("ingest.store_error", event_id=event.id, exc_info=True)


# ---------------------------------------------------------------------------
# Daemon orchestrator
# ---------------------------------------------------------------------------


def _make_narrative_getter(narrative_memory):  # type: ignore[no-untyped-def]
    """Return an async callable that fetches the current narrative text."""

    async def _get() -> str:
        if narrative_memory is None:
            return ""
        return narrative_memory._render_for_prompt()  # type: ignore[no-any-return]

    return _get


class CoreMindDaemon:
    """Top-level orchestrator for the CoreMind cognitive daemon.

    Constructs and wires together all subsystems, drives the ingest loop,
    and handles graceful shutdown on SIGINT/SIGTERM.
    """

    def __init__(self) -> None:
        self._plugin_host: PluginHostServer | None = None
        self._world_store: WorldStore | None = None
        self._ingest_task: asyncio.Task[None] | None = None
        self._journal: ActionJournal | None = None
        self._intents: IntentStore | None = None
        self._notify_router: NotificationRouter | None = None
        self._dashboard_port: DashboardNotificationPort | None = None
        self._dashboard_server: DashboardServer | None = None
        self._executor: Executor | None = None
        self._approvals: ApprovalGate | None = None
        self._router: ActionRouter | None = None
        self._intention_loop: IntentionLoop | None = None
        self._reasoning_loop: ReasoningLoop | None = None
        self._reflection_loop: ReflectionLoop | None = None
        self._anomaly_checker_task: asyncio.Task[None] | None = None
        self._approval_expirer_task: asyncio.Task[None] | None = None
        self._approval_expirer_stop: asyncio.Event = asyncio.Event()
        self._approved_dispatcher_task: asyncio.Task[None] | None = None
        self._approved_dispatcher_stop: asyncio.Event = asyncio.Event()
        self._response_listener_task: asyncio.Task[None] | None = None
        self._response_listener_stop: asyncio.Event = asyncio.Event()
        self._conversation_listener_stop: asyncio.Event = asyncio.Event()
        self._conversation_listener_task: asyncio.Task[None] | None = None
        self._presence_detector_task: asyncio.Task[None] | None = None
        self._conversation_handler: ConversationHandler | None = None

    async def start(self) -> None:
        """Initialise all subsystems and begin the ingest loop.

        Must be called before :meth:`run_forever` or :meth:`stop`.

        Raises:
            CoreMindError: If any subsystem fails to initialise.
        """
        config: DaemonConfig = load_config()
        daemon_private = ensure_daemon_keypair()
        daemon_public = daemon_private.public_key()

        registry = PluginRegistry(max_plugins=config.max_plugins)

        world_store = WorldStore(
            url=config.world_db_url,
            username=config.world_db_username,
            password=config.world_db_password,
            key_resolver=registry.resolve_key,
        )
        await world_store.connect()
        await world_store.apply_schema()

        event_bus = EventBus()

        def _no_secrets(_name: str) -> str | None:
            return None

        plugin_host = PluginHostServer(
            socket_path=config.plugin_socket,
            registry=registry,
            event_bus=event_bus,
            secrets_resolver=_no_secrets,  # Phase 4 introduces a real SecretsStore
        )
        await plugin_host.start()

        # ----------------------------------------------------------
        # L5 / L6 wiring
        # ----------------------------------------------------------
        journal = ActionJournal(
            config.audit_log_path,
            daemon_private,
            daemon_public,
        )
        await journal.load()
        intents = IntentStore(config.intent_store_path)

        # The dashboard adapter is shared between the notification router
        # (which delivers ``ask``-class prompts to it) and the web
        # dashboard's data sources (which surfaces the same prompts as
        # approve/deny rows).  Constructed once here so both wirings see
        # the same ``pending()`` state.
        dashboard_port = DashboardNotificationPort()

        notify_router = _build_notification_router(config, dashboard_port)

        effector_resolver = _build_effector_registry(notify_router)
        executor = Executor(
            journal,
            intents,
            effector_resolver,
            notify_port=notify_router,
        )
        approvals = ApprovalGate(notify_router, intents, journal, executor)
        router = ActionRouter(
            executor,
            approvals,
            intents,
            journal,
            user_ask_classes=tuple(config.intention.user_ask_classes),
        )

        self._world_store = world_store
        self._plugin_host = plugin_host
        self._journal = journal
        self._intents = intents
        self._notify_router = notify_router
        self._dashboard_port = dashboard_port
        self._executor = executor
        self._approvals = approvals
        self._router = router

        ingest_task = asyncio.create_task(
            self._ingest_loop_robust(event_bus, world_store),
            name="coremind.ingest",
        )
        ingest_task.add_done_callback(self._on_ingest_done)
        self._ingest_task = ingest_task

        self._approval_expirer_stop = asyncio.Event()
        self._approval_expirer_task = asyncio.create_task(
            self._approval_expirer_loop(approvals),
            name="coremind.approvals.expirer",
        )

        self._approved_dispatcher_stop = asyncio.Event()
        self._approved_dispatcher_task = asyncio.create_task(
            self._approved_dispatcher_loop(approvals),
            name="coremind.approvals.dispatcher",
        )

        # Response listener REMOVED — merged into _conversation_listener_loop
        # which uses subscribe_all() to avoid poll_offset race.

        if config.intention.enabled:
            reasoning_journal = config.audit_log_path.parent / "reasoning.log"
            llm_cfg = LLMConfig()
            if hasattr(config, "llm") and config.llm.intention.model:
                llm_cfg.intention = LayerConfig(
                    model=config.llm.intention.model,
                    max_completion_tokens=getattr(config.llm.intention, "max_tokens", 2048)
                    if hasattr(config.llm, "intention")
                    else 2048,
                )
            llm = LLM(llm_cfg)
            intention_loop = IntentionLoop(
                snapshot_provider=world_store,
                reasoning_feed=JsonlCyclePersister(reasoning_journal),
                intent_store=intents,
                llm=llm,
                router=router,
                config=IntentionLoopConfig(
                    interval_seconds=config.intention.interval_seconds,
                    max_questions=config.intention.max_questions,
                    min_salience=config.intention.min_salience,
                    min_confidence=config.intention.min_confidence,
                ),
            )
            intention_loop.start()
            self._intention_loop = intention_loop
            log.info(
                "daemon.intention_loop_started",
                interval_seconds=config.intention.interval_seconds,
            )

            # ----------------------------------------------------------
            # L4 — Reasoning Loop (30-minute cadence)
            # ----------------------------------------------------------
            reasoning_journal = config.audit_log_path.parent / "reasoning.log"
            reasoning_config = ReasoningLoopConfig(
                interval_seconds=1800,  # 30 minutes
                layer="reasoning_heavy",
                template_system="reasoning.heavy.system.v2",
                template_user="reasoning.heavy.user.v2",
            )
            # Configure LLM layers for reasoning and reflection
            if hasattr(config, "llm") and config.llm is not None:
                if hasattr(config.llm, "reasoning") and config.llm.reasoning.model:
                    llm_cfg.reasoning_heavy = LayerConfig(
                        model=config.llm.reasoning.model,
                        max_completion_tokens=getattr(config.llm.reasoning, "max_tokens", 2048),
                    )
                if hasattr(config.llm, "reflection") and config.llm.reflection.model:
                    llm_cfg.reflection = LayerConfig(
                        model=config.llm.reflection.model,
                    )
            # Re-create the LLM with the enriched config (now includes
            # reasoning and reflection layers alongside intention).
            llm_4 = LLM(llm_cfg)
            # Create semantic memory (Qdrant + Ollama embeddings)
            from coremind.memory.embeddings import OllamaEmbedder
            from coremind.memory.qdrant_store import QdrantVectorStore
            from coremind.memory.semantic import SemanticMemory

            try:
                embedder = OllamaEmbedder(
                    endpoint="http://10.0.0.175:11434", model="nomic-embed-text", dimension=768
                )
                qdrant_store = QdrantVectorStore()
                semantic_memory = SemanticMemory(qdrant_store, embedder, vector_size=768)
                await semantic_memory.initialise()
                log.info("daemon.semantic_memory_initialised")
            except Exception as exc:
                log.warning("daemon.semantic_memory_unavailable", error=str(exc))
                semantic_memory = None

            narrative_memory = NarrativeMemory()
            await narrative_memory.load()
            log.info("daemon.narrative_memory_loaded")

            reasoning_loop = ReasoningLoop(
                snapshot_provider=world_store,
                memory=semantic_memory,  # type: ignore[arg-type]
                llm=llm_4,
                persister=JsonlCyclePersister(reasoning_journal),
                narrative=narrative_memory,
                config=reasoning_config,
            )
            reasoning_loop.start()
            self._reasoning_loop = reasoning_loop
            log.info(
                "daemon.reasoning_loop_started",
                interval_seconds=1800,
            )

            # Conversation layer (Pillar #1) — use Gemini Flash for reliability
            conv_llm = LLM(
                LLMConfig(
                    reasoning_fast=LayerConfig(
                        model="ollama/deepseek-v4-flash:cloud",
                        max_completion_tokens=800,
                    )
                )
            )
            conv_store = ConversationStore()
            self._conversation_handler = ConversationHandler(
                llm=conv_llm,
                store=conv_store,
                get_narrative=_make_narrative_getter(narrative_memory),  # type: ignore[no-untyped-call]
            )
            self._conversation_listener_stop = asyncio.Event()
            self._conversation_listener_task = asyncio.create_task(
                self._conversation_listener_loop(
                    notify_router, self._conversation_handler, approvals
                ),
                name="coremind.conversation.listener",
            )
            log.info("daemon.conversation_handler_started")

            # Presence detector (Pillar #2 — Temporal Patterns)
            from coremind.presence.detector import PresenceDetector

            presence_detector = PresenceDetector(
                world_store,
                intents,
                router,
                alert_minutes=120,  # Alert after 2h of continuous presence
                check_interval=300,  # Check every 5 min
            )

            async def _run_presence_detector() -> None:
                """Robust wrapper: auto-restart presence detector on crash."""
                while True:
                    try:
                        await presence_detector.run()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("presence_detector.crashed_restarting")
                        await asyncio.sleep(30)

            self._presence_detector_task = asyncio.create_task(
                _run_presence_detector(),
                name="coremind.presence.detector",
            )
            log.info("daemon.presence_detector_started")

            # Schedule anomaly alert checker — runs 5s after each reasoning
            # cycle to push high-severity anomalies to Telegram.
            reasoning_journal_path = reasoning_journal

            async def _check_anomalies() -> None:
                """Watch reasoning.log for high-severity anomalies and alert."""
                import json as _json

                _last_pos = 0
                while True:
                    try:
                        if reasoning_journal_path.exists():
                            with open(reasoning_journal_path) as _f:
                                _f.seek(_last_pos)
                                for _line in _f:
                                    try:
                                        _cycle = _json.loads(_line)
                                    except _json.JSONDecodeError:
                                        continue
                                    for _a in _cycle.get("anomalies", []):
                                        if _a.get("severity") == "high":
                                            await notify_router.notify(
                                                actions=None,
                                                intent_id=None,
                                                message=(
                                                    f"🚨 **High-Severity Anomaly**\n\n"
                                                    f"{_a['description']}\n\n"
                                                    f"Baseline: {_a.get('baseline_description', 'N/A')}\n"
                                                    f"Cycle: `{_cycle.get('cycle_id', '?')[:16]}`"
                                                ),
                                                category="suggest",
                                                action_class="anomaly_alert",
                                            )
                                _last_pos = _f.tell()
                        await asyncio.sleep(15)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await asyncio.sleep(30)

            self._anomaly_checker_task = asyncio.create_task(
                _check_anomalies(), name="coremind.anomaly_checker"
            )

            # ----------------------------------------------------------
            # L7 — Reflection Loop (24-hour cadence)
            # ----------------------------------------------------------
            from coremind.reflection.calibration import (
                Calibrator,
            )
            from coremind.reflection.evaluator import (
                BasicConditionResolver,
                PredictionEvaluatorImpl,
            )
            from coremind.reflection.feedback import (
                FeedbackEvaluatorImpl,
            )
            from coremind.reflection.rule_learner import (
                InMemoryCandidateLedger,
                InMemoryRuleProposalStore,
                RuleLearnerImpl,
            )

            # Wire procedural memory (hash-chained rule store)
            procedural_store_path = config.audit_log_path.parent / "procedural.jsonl"
            procedural_memory = ProceduralMemory(procedural_store_path)
            await procedural_memory.load()
            log.info("daemon.procedural_memory_loaded", path=str(procedural_store_path))
            from coremind.reflection.loop import (
                ReflectionLoop,
                ReflectionLoopConfig,
            )
            from coremind.reflection.report import (
                MarkdownReportProducer,
            )
            from coremind.reflection.store import (
                SurrealReflectionStore,
            )

            # Try SurrealDB-backed reflection store; fall back to
            # in-memory stores if SurrealDB is unavailable.
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

            # Build prediction evaluator with BasicConditionResolver
            # Build calibration updater
            # (shared in-memory stores for the fallback case)
            from coremind.reflection.calibration import (
                InMemoryCalibrationStore,
            )
            from coremind.reflection.evaluator import (
                InMemoryPredictionEvaluationStore,
            )

            _in_memory_eval_store = InMemoryPredictionEvaluationStore()
            _in_memory_cal_store = InMemoryCalibrationStore()

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

            # Build feedback evaluator
            feedback_evaluator = FeedbackEvaluatorImpl()

            # Build rule learner with in-memory stores (ProceduralMemory
            # wiring is a follow-up).
            rule_learner = RuleLearnerImpl(
                rule_source=procedural_memory,
                ledger=InMemoryCandidateLedger(),
                proposal_store=InMemoryRuleProposalStore(),
            )

            # Build report producer
            report_producer = MarkdownReportProducer(
                proposal_store=InMemoryRuleProposalStore(),
            )

            # Build reflection notifier (sends reports via Telegram/dashboard)
            from coremind.reflection.notify import (
                ReflectionNotifier,
            )

            reflection_notifier = ReflectionNotifier(
                port=notify_router,
            )

            reflection_loop = ReflectionLoop(
                cycle_source=JsonlCyclePersister(reasoning_journal),  # type: ignore[arg-type]
                intent_source=intents,  # type: ignore[arg-type]
                action_feed=journal,  # type: ignore[arg-type]
                prediction_evaluator=prediction_evaluator,
                feedback_evaluator=feedback_evaluator,
                calibration_updater=calibration_updater,
                rule_learner=rule_learner,
                report_producer=report_producer,
                notifier=reflection_notifier,
                narrative_state=narrative_memory,
                narrative_llm=llm_4,
                config=ReflectionLoopConfig(
                    interval_seconds=86400,  # 24 hours
                    window_days=1,
                    notify_on_cycle=True,
                ),
            )
            reflection_loop.start()
            self._reflection_loop = reflection_loop
            log.info("daemon.reflection_loop_started")

        if config.dashboard.enabled:
            dashboard_server = await _start_dashboard(
                config=config.dashboard,
                world_store=world_store,
                intents=intents,
                journal=journal,
                dashboard_port=dashboard_port,
                event_bus=event_bus,
                reasoning_log=config.audit_log_path.parent / "reasoning.log",
            )
            self._dashboard_server = dashboard_server

        log.info(
            "daemon.started",
            socket=str(config.plugin_socket),
            audit_log=str(config.audit_log_path),
            intention_enabled=config.intention.enabled,
            dashboard_enabled=config.dashboard.enabled,
        )

    async def stop(self) -> None:
        """Gracefully shut down all subsystems.

        Safe to call even when :meth:`start` was never invoked.  Idempotent.
        """
        if self._intention_loop is not None:
            await self._intention_loop.stop()
            self._intention_loop = None

        if self._reasoning_loop is not None:
            await self._reasoning_loop.stop()
            self._reasoning_loop = None

        if self._reflection_loop is not None:
            await self._reflection_loop.stop()
            self._reflection_loop = None

        if self._anomaly_checker_task is not None:
            self._anomaly_checker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._anomaly_checker_task
            self._anomaly_checker_task = None

        if self._dashboard_server is not None:
            await self._dashboard_server.stop()
            self._dashboard_server = None

        if self._approval_expirer_task is not None:
            self._approval_expirer_stop.set()
            self._approval_expirer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._approval_expirer_task
            self._approval_expirer_task = None

        if self._response_listener_task is not None:
            self._response_listener_stop.set()
            self._response_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._response_listener_task
            self._response_listener_task = None

        if self._response_listener_task is not None:
            self._response_listener_stop.set()
            self._response_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._response_listener_task
            self._response_listener_task = None

        if self._conversation_listener_task is not None:
            self._conversation_listener_stop.set()
            self._conversation_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._conversation_listener_task
            self._conversation_listener_task = None

        if self._presence_detector_task is not None:
            self._presence_detector_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._presence_detector_task
            self._presence_detector_task = None

        if self._approved_dispatcher_task is not None:
            self._approved_dispatcher_stop.set()
            self._approved_dispatcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._approved_dispatcher_task
            self._approved_dispatcher_task = None

        if self._ingest_task is not None:
            self._ingest_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ingest_task
            self._ingest_task = None

        if self._plugin_host is not None:
            await self._plugin_host.stop()
            self._plugin_host = None

        if self._world_store is not None:
            await self._world_store.close()
            self._world_store = None

        log.info("daemon.stopped")

    async def run_forever(self) -> None:
        """Start the daemon and block until SIGINT or SIGTERM is received.

        Calls :meth:`start` then waits for a termination signal before calling
        :meth:`stop`.
        """
        await self.start()

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        log.info("daemon.running")
        try:
            await stop_event.wait()
        finally:
            await self.stop()

    def _on_ingest_done(self, task: asyncio.Task[None]) -> None:
        """Done callback for the ingest task.

        Logs a fatal-level message if the task exits for any reason other than
        cancellation, so unexpected crashes are immediately visible.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.critical(
                "daemon.ingest_crashed",
                exc_info=exc,
                detail="Ingest loop exited unexpectedly; daemon may be degraded.",
            )

    async def _ingest_loop_robust(self, event_bus: EventBus, world_store: _StorePort) -> None:
        """Robust wrapper around _ingest_loop with automatic restart on crash."""
        while True:
            try:
                await self._ingest_loop(event_bus, world_store)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("daemon.ingest_crashed_restarting")
                await asyncio.sleep(5)

    async def _ingest_loop(self, event_bus: EventBus, world_store: _StorePort) -> None:
        """Drain the EventBus and persist every arriving event to the World Model.

        Runs until the task is cancelled.  Signature failures and store errors
        are logged but do not terminate the loop.

        Args:
            event_bus: The event bus to consume.
            world_store: The World Model store to write to.
        """
        subscription = event_bus.subscribe()
        try:
            async for event in subscription:
                await _handle_event(event, world_store)
        finally:
            # Suppress errors from aclose() so they do not mask the
            # CancelledError (or other cause) that stopped the loop.
            with contextlib.suppress(Exception):
                await subscription.aclose()

    async def _approval_expirer_loop(self, approvals: ApprovalGate) -> None:
        """Periodically expire stale pending approvals.

        Runs until cancelled or :attr:`_approval_expirer_stop` is set.  Errors
        from :meth:`ApprovalGate.expire_stale` are logged but never terminate
        the loop — the sweep is idempotent and a transient failure is recovered
        on the next tick.

        Args:
            approvals: The approval gate whose pending requests are swept.
        """
        while not self._approval_expirer_stop.is_set():
            try:
                expired = await approvals.expire_stale()
                if expired:
                    log.info("approvals.swept", expired=expired)
            except Exception:
                log.exception("approvals.expirer_failed")
            try:
                await asyncio.wait_for(
                    self._approval_expirer_stop.wait(),
                    timeout=_APPROVAL_EXPIRER_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue

    async def _response_listener_loop(
        self, notify_router: NotificationRouter, approvals: ApprovalGate
    ) -> None:
        """Listen for approval responses from all callback-capable ports.

        Telegram callbacks, dashboard API submissions, and CLI responses all
        flow through the notification router's ``subscribe_responses()``
        stream.  This loop feeds every response to the approval gate so
        inline-button clicks and API calls actually take effect.

        Runs until cancelled or :attr:`_response_listener_stop` is set.
        Individual response-handling errors are logged but never terminate
        the loop — a bad response cannot block legitimate ones.

        Args:
            notify_router: The notification router to subscribe to.
            approvals: The approval gate to feed responses into.
        """
        async for response in notify_router.subscribe_responses():
            if self._response_listener_stop.is_set():
                break
            try:
                await approvals.handle_response(response)
            except Exception:
                log.exception(
                    "approvals.handle_response_failed",
                    intent_id=response.intent_id,
                    decision=response.decision,
                    responder=response.responder.id,
                )

    async def _conversation_listener_loop(
        self,
        notify_router: NotificationRouter,
        conversation_handler: ConversationHandler,
        approvals: ApprovalGate,
    ) -> None:
        """Single Telegram listener: dispatches both text messages AND approval responses.

        Uses subscribe_all() to avoid poll_offset race between the old
        response_listener and conversation_listener.
        """
        from coremind.conversation.schemas import InboundTextMessage

        # Find the Telegram port
        telegram_port = None
        for port in [notify_router._primary, *notify_router._fallbacks]:
            if hasattr(port, "subscribe_all"):
                telegram_port = port
                break

        if telegram_port is None:
            log.warning("conversation.no_telegram_port_found")
            return

        async for update in telegram_port.subscribe_all():
            if self._conversation_listener_stop.is_set():
                break
            try:
                if isinstance(update, InboundTextMessage):
                    # Text message → conversation handler
                    response_text, _conv = await conversation_handler.handle_message(
                        update.text,
                        conversation_id=update.conversation_id,
                        user_id=update.responder,
                    )
                    await notify_router.notify(
                        message=response_text,
                        category="info",
                        actions=None,
                        intent_id=None,
                    )
                else:
                    # ApprovalResponse → approval gate
                    try:
                        await approvals.handle_response(update)
                    except Exception:
                        log.exception(
                            "approvals.handle_response_failed",
                            intent_id=update.intent_id,
                        )
            except Exception:
                log.exception("conversation.listener_error")

    async def _approved_dispatcher_loop(self, approvals: ApprovalGate) -> None:
        """Periodically dispatch intents that have been approved.

        Approval responses arriving via the CLI or any
        :class:`~coremind.notify.port.NotificationPort` flip an intent to
        ``status="approved"`` without executing it.  This loop hands every
        such intent to the executor, ensuring CLI-originated approvals are
        honoured while the daemon is running.  Errors from individual
        dispatches are logged but never terminate the loop.

        Args:
            approvals: The approval gate whose approved intents are dispatched.
        """
        while not self._approved_dispatcher_stop.is_set():
            try:
                dispatched = await approvals.dispatch_approved()
                if dispatched:
                    log.info("approvals.dispatched", count=dispatched)
            except Exception:
                log.exception("approvals.dispatcher_failed")
            try:
                await asyncio.wait_for(
                    self._approved_dispatcher_stop.wait(),
                    timeout=_APPROVED_DISPATCHER_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _build_notification_router(
    config: DaemonConfig,
    dashboard_port: DashboardNotificationPort,
) -> NotificationRouter:
    """Construct the notification router from ``config``.

    Args:
        config: The validated daemon configuration.
        dashboard_port: The dashboard adapter the router should route to.
            Passed in so the daemon can keep a reference for the web
            dashboard's data sources, rather than constructing one inside.

    Currently supports dashboard + telegram adapters.  Telegram is wired only
    when ``config.notify.telegram.enabled`` is true; otherwise the dashboard
    port is used as primary with no fallbacks.

    Secrets loading is deferred to Phase 4's SecretsStore; if Telegram is
    enabled but the bot token is unavailable, the adapter falls back to a
    disabled state at notify time.
    """
    ports: dict[str, NotificationPort] = {"dashboard": dashboard_port}

    if config.notify.telegram.enabled and config.notify.telegram.chat_id:
        # Lazy import to avoid pulling aiohttp on pure-dashboard deployments.
        # Phase 3 note: bot token resolution via SecretsStore lands in Phase 4.
        # For now operators must export COREMIND_TELEGRAM_BOT_TOKEN.
        import os

        from coremind.notify.adapters.telegram import (
            TelegramNotificationPort,
        )

        token = os.environ.get("COREMIND_TELEGRAM_BOT_TOKEN", "")
        if token:
            ports["telegram"] = TelegramNotificationPort(
                token,
                config.notify.telegram.chat_id,
            )

    primary = ports.get(config.notify.primary) or ports["dashboard"]
    fallbacks = [ports[name] for name in config.notify.fallbacks if name in ports]

    policy = QuietHoursPolicy(
        timezone=config.quiet_hours.timezone,
        quiet_start=config.quiet_hours.quiet_start,
        quiet_end=config.quiet_hours.quiet_end,
    )
    quiet = QuietHoursFilter(policy) if config.quiet_hours.enabled else _AllowAllFilter()
    return NotificationRouter(primary, fallbacks, quiet)


class _AllowAllFilter(QuietHoursFilter):
    """Quiet-hours filter that never defers — used when the policy is disabled."""

    def __init__(self) -> None:
        from datetime import time as _time

        super().__init__(QuietHoursPolicy(quiet_start=_time(0, 0), quiet_end=_time(0, 0)))


def _build_effector_registry(
    notify_router: NotificationRouter,
) -> EffectorRegistry:
    """Build the in-process effector registry with all available effectors.

    Each effector wraps an external API and implements :class:`EffectorPort`.
    The registry doubles as an :class:`EffectorResolver` callable, so it can
    be passed directly to :class:`Executor`.

    When the future Phase 3.5 gRPC reverse-channel lands, this function can
    be replaced by one that builds per-plugin gRPC effector stubs.  For now,
    in-process effectors are pragmatic and sufficient.
    """
    registry = EffectorRegistry()

    # Notification effector — wraps the existing notification router
    notifier = NotificationEffector(notify_router)
    registry.register("coremind.plugin.notification.send", notifier)
    registry.register("coremind.plugin.notification.send_sms", notifier)
    # Alias: LLM sometimes generates different operation names for the same thing
    registry.register("coremind.plugin.telegram.send_message", notifier)
    registry.register("coremind.plugin.task_manager.remind", notifier)

    # Home Assistant effector
    ha = HomeAssistantEffector()
    registry.register_many(
        [
            "coremind.plugin.homeassistant.get_state",
            "coremind.plugin.homeassistant.get_history",
            "coremind.plugin.homeassistant.turn_on",
            "coremind.plugin.homeassistant.turn_off",
            "coremind.plugin.homeassistant.light.turn_off",
            "coremind.plugin.homeassistant.create_automation",
            "coremind.plugin.homeassistant.send_notification",
            "coremind.plugin.homeassistant.get_printer_estimated_pages",
            "coremind.plugin.homeassistant.set_temperature",
        ],
        ha,
    )

    # Vikunja task manager effector
    vikunja = VikunjaEffector()
    registry.register_many(
        [
            "coremind.plugin.vikunja.list_tasks",
            "coremind.plugin.vikunja.get_tasks",
        ],
        vikunja,
    )

    # Gmail effector (via gog CLI)
    gmail = GmailEffector()
    registry.register_many(
        [
            "coremind.plugin.gmail.fetch_unread",
            "coremind.plugin.gmail.search_emails",
        ],
        gmail,
    )

    # Calendar effector (Google Calendar via gog)
    calendar = CalendarEffector()
    registry.register_many(
        [
            "coremind.plugin.calendar.fetch_upcoming_events",
            "coremind.plugin.calendar.get_next_payday",
        ],
        calendar,
    )

    log.info("effector_registry.built", operation_count=len(registry._effectors))
    return registry


# ---------------------------------------------------------------------------
# Dashboard wiring
# ---------------------------------------------------------------------------


# Directory holding per-secret files (chmod 600).  Mirrors the convention
# already used for the daemon keypair under ``~/.coremind/``.
_SECRETS_DIR = Path.home() / ".coremind" / "secrets"

# Minimum length required for the dashboard's bearer token.  The
# :class:`DashboardAuth` model enforces the same lower bound; we mirror it
# here so the daemon refuses to start the dashboard with an obviously
# under-strength token rather than failing at request time.
_MIN_DASHBOARD_TOKEN_LENGTH = 16


def _resolve_dashboard_secret(name: str) -> str | None:
    """Return the dashboard's bearer token, or ``None`` if not configured.

    Resolution order:

    1. ``COREMIND_DASHBOARD_API_TOKEN`` environment variable — operator-friendly
       for development; never written to disk.
    2. ``~/.coremind/secrets/<name>`` — the canonical, persistent location.
       The file is read in text mode and stripped; surrounding whitespace
       is tolerated.

    Args:
        name: The secret identifier (typically ``"dashboard_api_token"``).
    """
    env_value = os.environ.get("COREMIND_DASHBOARD_API_TOKEN")
    if env_value:
        return env_value.strip()
    path = _SECRETS_DIR / name
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        log.warning("dashboard.secret_read_failed", path=str(path), error=str(exc))
        return None


def _build_dashboard_auth(config: DashboardConfig) -> DashboardAuth | None:
    """Construct a :class:`DashboardAuth` from config, or ``None`` if absent.

    A missing or under-length token disables approval submissions
    (the dashboard remains read-only).  The function logs a structured
    warning so operators can spot a misconfiguration in the journal.
    """
    token = _resolve_dashboard_secret(config.api_token_secret)
    if not token or len(token) < _MIN_DASHBOARD_TOKEN_LENGTH:
        log.warning(
            "dashboard.auth_disabled",
            reason="missing_or_short_token",
            secret_name=config.api_token_secret,
        )
        return None
    # Default the allowed-origins list to the bind address when the
    # operator hasn't customised it; that matches the most common
    # deployment (browser hits ``http://127.0.0.1:9900`` directly).
    origins = config.allowed_origins or (f"http://{config.host}:{config.port}",)
    return DashboardAuth(
        api_token=token,
        operator=UserRef(
            id=config.operator_id,
            display_name=config.operator_display_name,
        ),
        allowed_origins=origins,
    )


async def _start_dashboard(
    *,
    config: DashboardConfig,
    world_store: WorldStore,
    intents: IntentStore,
    journal: ActionJournal,
    dashboard_port: DashboardNotificationPort,
    event_bus: EventBus,
    reasoning_log: Path,
) -> DashboardServer:
    """Construct and start the read-only web dashboard.

    Args:
        config: Validated dashboard configuration.
        world_store: World Model store; surfaces entities, relationships,
            and recent events.
        intents: Intent store; surfaces the pending/queued intents.
        journal: Action journal; surfaces audit entries via ``read_recent``.
        dashboard_port: Shared :class:`DashboardNotificationPort` instance —
            the notification router writes to it, the dashboard reads
            ``pending()`` from it.  Sharing one instance is what keeps the
            UI's pending-approval list in sync with reality.
        event_bus: In-process :class:`EventBus`; powers the SSE live tail.
        reasoning_log: Path to the JSONL reasoning-cycle log; surfaces the
            ``/reasoning`` page.

    Returns:
        A started :class:`DashboardServer`.  Stopping is the caller's job.
    """
    sources = DashboardDataSources(
        world=world_store,
        cycles=JsonlCyclePersister(reasoning_log),
        intents=intents,
        journal=journal,
        # Reflection-report archive is not yet exposed via a list-able port
        # (Phase 4 follow-up); the page renders an empty state for now.
        reflection=None,
        notifications=dashboard_port,
        events=event_bus,
    )
    auth = _build_dashboard_auth(config)
    server = DashboardServer(
        sources,
        auth=auth,
        host=config.host,
        port=config.port,
    )
    await server.start()
    log.info(
        "daemon.dashboard_started",
        host=config.host,
        port=config.port,
        auth_enabled=auth is not None,
    )
    return server
