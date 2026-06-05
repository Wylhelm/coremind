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
import signal
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from coremind.reflection.loop import ReflectionLoop

from coremind.action.approvals import ApprovalGate
from coremind.action.executor import Executor
from coremind.action.journal import ActionJournal
from coremind.action.notification_journal import NotificationJournal
from coremind.action.router import ActionRouter
from coremind.config import DaemonConfig, load_config
from coremind.conversation.handler import ConversationHandler
from coremind.conversation.store import ConversationStore
from coremind.core.daemon_anomalies import create_anomaly_checker_task
from coremind.core.daemon_dashboard import start_dashboard
from coremind.core.daemon_effectors import build_effector_registry
from coremind.core.daemon_meta import build_meta_system
from coremind.core.daemon_notifications import build_notification_router
from coremind.core.daemon_reflection import build_reflection_system
from coremind.core.event_bus import EventBus
from coremind.crypto.signatures import ensure_daemon_keypair
from coremind.dashboard import DashboardServer
from coremind.dashboard.views import configure_dashboard_timezone
from coremind.errors import SignatureError, StoreError
from coremind.intention.loop import IntentionLoop, IntentionLoopConfig
from coremind.intention.persistence import IntentStore
from coremind.memory.narrative import NarrativeMemory
from coremind.memory.procedural import ProceduralMemory
from coremind.meta.loop import MetaLoop
from coremind.notify.adapters.dashboard import DashboardNotificationPort
from coremind.notify.port import (
    ApprovalAction,
    NotificationCategory,
    NotificationReceipt,
)
from coremind.notify.router import NotificationRouter
from coremind.personalization.config import get_timezone
from coremind.plugin_host.registry import PluginRegistry
from coremind.plugin_host.server import PluginHostServer
from coremind.prediction import PredictiveMemory
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
_APPROVED_DISPATCHER_INTERVAL_SECONDS = 30.0

# Maximum number of Telegram message→intent mappings to keep in the
# deduplication dict before pruning the oldest entry.
_MAX_TELEGRAM_MSG_MAPPINGS = 50


# ---------------------------------------------------------------------------
# Internal port — allows test doubles without depending on WorldStore directly
# ---------------------------------------------------------------------------


class _StorePort(Protocol):
    """Minimal interface consumed by the ingest loop and robust wrapper."""

    async def apply_event(self, event: WorldEventRecord) -> None:
        """Persist *event* to the World Model."""
        ...

    async def reconnect(self) -> None:
        """Reconnect to the backing store after a connection loss."""
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
        # Re-raise StoreError so the robust loop can trigger reconnection.
        # SignatureError is permanent (bad key) — StoreError may be transient
        # (connection loss) and should trigger recovery.
        raise


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
        self._meta_loop: MetaLoop | None = None
        self._predictive_memory: PredictiveMemory | None = None
        self._anomaly_checker_task: asyncio.Task[None] | None = None
        self._approval_expirer_task: asyncio.Task[None] | None = None
        self._approval_expirer_stop: asyncio.Event = asyncio.Event()
        self._approved_dispatcher_task: asyncio.Task[None] | None = None
        self._approved_dispatcher_stop: asyncio.Event = asyncio.Event()
        self._response_listener_task: asyncio.Task[None] | None = None
        self._response_listener_stop: asyncio.Event = asyncio.Event()
        self._conversation_listener_stop: asyncio.Event = asyncio.Event()
        self._conversation_listener_task: asyncio.Task[None] | None = None
        self._embedding_prune_task: asyncio.Task[None] | None = None
        self._embedding_prune_stop: asyncio.Event = asyncio.Event()
        self._embedding_pipeline_memory: object | None = None
        self._embedding_pipeline_encoder: object | None = None
        self._presence_detector_task: asyncio.Task[None] | None = None
        self._conversation_handler: ConversationHandler | None = None
        # Telegram message id → intent_id mapping for reply matching.
        self._telegram_msg_to_intent: dict[str, str] = {}
        self._last_notified_intent_id: str | None = None

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

        notify_router = build_notification_router(config, dashboard_port)

        # Wrap notify_router.notify to track channel_message_id → intent_id
        # mapping.  This lets the conversation listener match user replies
        # (via reply_to_message_id) back to the intent that triggered the
        # notification, even when the notify call originates from
        # ApprovalGate or Executor rather than the listener itself.
        _original_notify = notify_router.notify

        async def _tracked_notify(
            *,
            message: str,
            category: NotificationCategory,
            actions: list[ApprovalAction] | None,
            intent_id: str | None,
            action_class: str | None = None,
        ) -> NotificationReceipt:
            receipt = await _original_notify(
                message=message,
                category=category,
                actions=actions,
                intent_id=intent_id,
                action_class=action_class,
            )
            if intent_id and receipt and receipt.channel_message_id:
                self._telegram_msg_to_intent[receipt.channel_message_id] = intent_id
                self._last_notified_intent_id = intent_id
                # Prune dict if > _MAX_TELEGRAM_MSG_MAPPINGS entries
                if len(self._telegram_msg_to_intent) > _MAX_TELEGRAM_MSG_MAPPINGS:
                    oldest = next(iter(self._telegram_msg_to_intent))
                    del self._telegram_msg_to_intent[oldest]
            return receipt

        notify_router.notify = _tracked_notify  # type: ignore[method-assign]

        effector_resolver = build_effector_registry(notify_router)
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

        report_store: object | None = None

        if config.intention.enabled:
            reasoning_journal = config.audit_log_path.parent / "reasoning.log"
            pipeline_intention = None
            pipeline_reasoning = None
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
                event_bus=event_bus,
                predictive_memory=None,  # Set after semantic_memory init below
                pipeline=pipeline_intention,
                personalization=config.personalization,
                config=IntentionLoopConfig(
                    event_driven=config.intention.event_driven,
                    interval_seconds=config.intention.interval_seconds,
                    routine_interval_seconds=config.intention.routine_interval_seconds,
                    max_questions=config.intention.max_questions,
                    min_salience=config.intention.min_salience,
                    min_confidence=config.intention.min_confidence,
                ),
            )
            intention_loop.start()
            self._intention_loop = intention_loop
            log.info(
                "daemon.intention_loop_started",
                event_driven=config.intention.event_driven,
                interval_seconds=config.intention.interval_seconds,
                routine_interval_seconds=config.intention.routine_interval_seconds,
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

            predictive_memory: object | None = None
            if config.prediction.enabled and semantic_memory is not None:
                predictive_memory = PredictiveMemory(semantic_memory)
                log.info("daemon.predictive_memory_initialised")

            # ----------------------------------------------------------
            # Embedding pipeline (Phase 3E) — compressed prompts for L4/L5
            # ----------------------------------------------------------
            pipeline_intention = None
            pipeline_reasoning = None
            if config.embedding_pipeline.enabled:
                try:
                    from coremind.world.compressed_prompt import CompressedPromptBuilder
                    from coremind.world.differ import SnapshotDiffer
                    from coremind.world.embeddings import EmbeddingEncoder
                    from coremind.world.pipeline import WorldEncodingPipeline
                    from coremind.world.snapshot_memory import SnapshotMemory

                    embed_cfg = config.embedding_pipeline
                    llm_embed = config.llm.embedding

                    # Reuse the same embedder instance (OllamaEmbedder) from semantic memory
                    from coremind.memory.embeddings import OllamaEmbedder

                    pipeline_embedder = OllamaEmbedder(
                        endpoint=llm_embed.url,
                        model=llm_embed.model,
                        dimension=llm_embed.dimension,
                    )
                    shared_encoder = EmbeddingEncoder(
                        pipeline_embedder,
                        dimension=llm_embed.dimension,
                        cache_size=embed_cfg.cache_size,
                    )
                    shared_memory = SnapshotMemory(
                        embed_cfg.qdrant_url,
                        collection=embed_cfg.collection_name,
                        vector_size=llm_embed.dimension,
                        timeout_seconds=embed_cfg.timeout_seconds,
                    )
                    await shared_memory.ensure_collection()

                    prompt_builder = CompressedPromptBuilder(
                        memory=shared_memory, top_k=embed_cfg.top_k_similar
                    )

                    pipeline_intention = WorldEncodingPipeline(
                        encoder=shared_encoder,
                        differ=SnapshotDiffer(),
                        memory=shared_memory,
                        prompt_builder=prompt_builder,
                    )
                    pipeline_reasoning = WorldEncodingPipeline(
                        encoder=shared_encoder,
                        differ=SnapshotDiffer(),
                        memory=shared_memory,
                        prompt_builder=prompt_builder,
                    )
                    self._embedding_pipeline_memory = shared_memory
                    self._embedding_pipeline_encoder = shared_encoder
                    log.info("daemon.embedding_pipeline_initialised")
                    # Inject pipeline into already-created loops (they were created before pipeline init)
                    intention_loop._pipeline = pipeline_intention
                    log.info("daemon.pipeline_injected_into_intention")
                    # Start periodic pruning task
                    self._embedding_prune_stop.clear()
                    self._embedding_prune_task = asyncio.create_task(
                        self._prune_snapshot_embeddings(
                            shared_memory,
                            keep_count=embed_cfg.prune_keep_count,
                            interval=embed_cfg.prune_interval_seconds,
                        ),
                        name="coremind.embedding_prune",
                    )
                except Exception as exc:
                    log.warning("daemon.embedding_pipeline_unavailable", error=str(exc))

            reasoning_loop = ReasoningLoop(
                snapshot_provider=world_store,
                memory=semantic_memory,  # type: ignore[arg-type]
                llm=llm_4,
                persister=JsonlCyclePersister(reasoning_journal),
                narrative=narrative_memory,
                predictive_memory=None,  # Set after semantic_memory init below
                pipeline=pipeline_reasoning,
                config=reasoning_config,
                personalization=config.personalization,
            )
            reasoning_loop.start()
            self._reasoning_loop = reasoning_loop
            self._predictive_memory = predictive_memory  # type: ignore[assignment]
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
                intent_store=intents,
                personalization=config.personalization,
            )
            self._conversation_listener_stop = asyncio.Event()
            self._conversation_listener_task = asyncio.create_task(
                self._conversation_listener_loop(
                    notify_router, self._conversation_handler, approvals
                ),
                name="coremind.conversation.listener",
            )
            log.info("daemon.conversation_handler_started")

            # Wire conversation handler into intention loop for context injection
            if self._intention_loop is not None:
                self._intention_loop._conversation_handler = self._conversation_handler
                log.info("daemon.conversation_wired_to_intention")

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

            # Conversation auto-archive — prevent stale conversations from
            # polluting the intention loop context (e.g. old corrections that
            # cause the LLM to loop on apologies).
            conv_auto_archive_interval = 3600  # Check every hour

            async def _run_conv_auto_archive() -> None:
                """Periodically archive conversations older than TTL."""
                while True:
                    try:
                        if self._conversation_handler is not None:
                            count = await self._conversation_handler.archive_old_conversations()
                            if count:
                                log.info("conversation.auto_archive_done", archived=count)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("conversation.auto_archive_error")
                    await asyncio.sleep(conv_auto_archive_interval)

            self._conv_auto_archive_task = asyncio.create_task(
                _run_conv_auto_archive(),
                name="coremind.conversation.auto_archive",
            )
            log.info("daemon.conversation_auto_archive_started", interval_s=conv_auto_archive_interval)

            # Schedule anomaly alert checker with dedup via notification journal
            self._anomaly_checker_task = create_anomaly_checker_task(
                reasoning_journal_path=reasoning_journal,
                notify_journal=NotificationJournal(),
                notify_router=notify_router,
            )

            # ----------------------------------------------------------
            # L7 — Reflection Loop (24-hour cadence)
            # ----------------------------------------------------------
            procedural_store_path = config.audit_log_path.parent / "procedural.jsonl"
            procedural_memory = ProceduralMemory(procedural_store_path)
            await procedural_memory.load()
            log.info("daemon.procedural_memory_loaded", path=str(procedural_store_path))

            reflection_loop, report_store = await build_reflection_system(
                config=config,
                world_store=world_store,
                intents=intents,
                journal=journal,
                reasoning_journal=reasoning_journal,
                narrative_memory=narrative_memory,
                llm=llm_4,
                notify_router=notify_router,
                procedural_memory=procedural_memory,
            )
            self._reflection_loop = reflection_loop

        # ----------------------------------------------------------
        # L8: Meta-loop (self-improvement)
        # ----------------------------------------------------------
        meta_result = await build_meta_system(
            config=config,
            intents=intents,
            journal=journal,
            registry=registry,
            narrative_memory=narrative_memory,
        )
        self._meta_loop = meta_result.meta_loop

        if config.dashboard.enabled:
            configure_dashboard_timezone(get_timezone(config.personalization))
            _meta_source: object | None = None
            if self._meta_loop is not None:
                from coremind.dashboard.adapters_meta import DaemonMetaSource

                _meta_source = DaemonMetaSource(
                    meta_config=config.meta,
                    meta_store=meta_result.meta_store,
                    approval_queue=meta_result.meta_approval_queue,
                    adjuster=meta_result.meta_adjuster,
                )
            dashboard_server = await start_dashboard(
                config=config.dashboard,
                world_store=world_store,
                intents=intents,
                journal=journal,
                dashboard_port=dashboard_port,
                event_bus=event_bus,
                reasoning_log=config.audit_log_path.parent / "reasoning.log",
                reflection=report_store if config.intention.enabled else None,
                meta_source=_meta_source,
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
        if self._meta_loop is not None:
            await self._meta_loop.stop()
            self._meta_loop = None

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

        if self._embedding_prune_task is not None:
            self._embedding_prune_stop.set()
            self._embedding_prune_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._embedding_prune_task
            self._embedding_prune_task = None

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
        """Robust wrapper around _ingest_loop with automatic reconnect on crash.

        StoreError (typically from a broken SurrealDB connection) triggers a
        reconnection attempt before restarting the ingest loop.  Other
        exceptions are logged and the loop restarts after a short delay.
        """
        consecutive_store_errors = 0
        while True:
            try:
                consecutive_store_errors = 0
                await self._ingest_loop(event_bus, world_store)
            except asyncio.CancelledError:
                raise
            except StoreError:
                consecutive_store_errors += 1
                backoff = min(consecutive_store_errors * 2, 30)
                log.error(
                    "daemon.ingest_reconnecting",
                    attempt=consecutive_store_errors,
                    backoff_s=backoff,
                )
                try:
                    await world_store.reconnect()
                except Exception:
                    log.exception(
                        "daemon.ingest_reconnect_failed",
                        attempt=consecutive_store_errors,
                    )
                await asyncio.sleep(backoff)
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

    async def _prune_snapshot_embeddings(
        self,
        memory: object,
        *,
        keep_count: int,
        interval: float,
    ) -> None:
        """Periodically prune old snapshot embeddings from Qdrant.

        Runs until :attr:`_embedding_prune_stop` is set.  Errors are logged
        but never terminate the loop.

        Args:
            memory: SnapshotMemory instance with a ``prune()`` method.
            keep_count: Maximum embeddings to retain.
            interval: Seconds between prune sweeps.
        """
        while not self._embedding_prune_stop.is_set():
            try:
                pruned = await memory.prune(keep_count=keep_count)  # type: ignore[attr-defined]
                if pruned > 0:
                    log.info("embedding.pruned", count=pruned, kept=keep_count)
            except Exception:
                log.exception("embedding.prune_failed")
            try:
                await asyncio.wait_for(
                    self._embedding_prune_stop.wait(),
                    timeout=interval,
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
                    # Try to match intent from reply_to_message_id
                    matched_intent_id: str | None = None
                    matched_intent_desc: str | None = None
                    intents = self._intents

                    if (
                        update.reply_to_message_id
                        and update.reply_to_message_id in self._telegram_msg_to_intent
                    ):
                        matched_intent_id = self._telegram_msg_to_intent[update.reply_to_message_id]
                    elif self._last_notified_intent_id is not None:
                        # Fallback: use most recently notified intent
                        matched_intent_id = self._last_notified_intent_id

                    # If an intent was matched, load it and set status to conversation
                    if matched_intent_id and intents is not None:
                        matched_intent = await intents.get(matched_intent_id)
                        if matched_intent is not None:
                            matched_intent_desc = matched_intent.question.text
                            if matched_intent.status != "conversation":
                                matched_intent.status = "conversation"
                                await intents.save(matched_intent)
                                log.info(
                                    "conversation.intent_linked",
                                    intent_id=matched_intent_id,
                                    reply_to_msg_id=update.reply_to_message_id,
                                )

                    # Text message → conversation handler
                    response_text, _conv = await conversation_handler.handle_message(
                        update.text,
                        conversation_id=update.conversation_id,
                        user_id=update.responder,
                        intent_id=matched_intent_id,
                        intent_description=matched_intent_desc,
                    )
                    receipt = await notify_router.notify(
                        message=response_text,
                        category="info",
                        actions=None,
                        intent_id=matched_intent_id,
                    )
                    # Store mapping so future replies can be matched
                    if matched_intent_id and receipt and receipt.channel_message_id:
                        self._telegram_msg_to_intent[receipt.channel_message_id] = matched_intent_id
                        self._last_notified_intent_id = matched_intent_id
                        # Prune dict if > _MAX_TELEGRAM_MSG_MAPPINGS entries
                        if len(self._telegram_msg_to_intent) > _MAX_TELEGRAM_MSG_MAPPINGS:
                            oldest = next(iter(self._telegram_msg_to_intent))
                            del self._telegram_msg_to_intent[oldest]
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

