"""L8 Meta-loop factory for the CoreMind daemon.

Extracted from :meth:`CoreMindDaemon.start` to keep the daemon thin.
Provides a single factory function that wires together all meta-cognition
components: stores, observer, evaluator, validator, adjuster, and the
:class:`~coremind.meta.loop.MetaLoop` orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from coremind.action.journal import ActionJournal
from coremind.config import DaemonConfig
from coremind.intention.persistence import IntentStore
from coremind.memory.narrative import NarrativeMemory
from coremind.meta.adjuster import MetaAdjuster
from coremind.meta.constants import (
    DEFAULT_POLICIES,
    FORBIDDEN_PARAMETER_PATHS,
    HARD_BOUNDS,
)
from coremind.meta.evaluator import PolicyEvaluator
from coremind.meta.loop import MetaLoop
from coremind.meta.observer import MetaObserver
from coremind.meta.protocols import (
    AdjustmentHistoryProtocol,  # noqa: F401 — referenced in docstrings
    ConfigReaderProtocol,  # noqa: F401 — referenced in docstrings
)
from coremind.meta.safety_validator import MetaSafetyValidator
from coremind.meta.schemas import AdjustmentRecord
from coremind.meta.stores import (
    InMemoryApprovalQueue,
    InMemoryConfigStore,
    InMemoryMetaStore,
    LoggingMetaEventBus,
)
from coremind.plugin_host.registry import PluginRegistry

log = structlog.get_logger(__name__)


# ------------------------------------------------------------------
# Adapter classes (thin wrappers around in-memory stores)
# ------------------------------------------------------------------


class _AdjustmentHistoryAdapter:
    """Adapter from InMemoryMetaStore to
    :class:`~coremind.meta.protocols.AdjustmentHistoryProtocol`.
    """

    def __init__(self, store: InMemoryMetaStore) -> None:
        self._store = store

    def last_adjustment(self, parameter_path: str) -> AdjustmentRecord | None:
        for record in reversed(list(self._store._adjustments.values())):
            if record.parameter_path == parameter_path:
                return record
        return None


class _ConfigReaderAdapter:
    """Adapter from InMemoryConfigStore to
    :class:`~coremind.meta.protocols.ConfigReaderProtocol`.
    """

    def __init__(self, store: InMemoryConfigStore) -> None:
        self._store = store

    def get(self, dotted_path: str) -> float:
        try:
            val = self._store._data[dotted_path]
            return float(val)
        except (KeyError, TypeError, ValueError):
            return 0.0


# ------------------------------------------------------------------
# Result container
# ------------------------------------------------------------------


@dataclass
class MetaSystemResult:
    """Components returned by :func:`build_meta_system`.

    All fields are ``None`` when *config.meta.enabled* is ``False``.
    """

    meta_loop: MetaLoop | None
    meta_store: InMemoryMetaStore | None
    meta_config_store: InMemoryConfigStore | None
    meta_approval_queue: InMemoryApprovalQueue | None
    meta_adjuster: MetaAdjuster | None


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------


async def build_meta_system(
    config: DaemonConfig,
    intents: IntentStore,
    journal: ActionJournal,
    registry: PluginRegistry,
    narrative_memory: NarrativeMemory,
) -> MetaSystemResult:
    """Construct and start the L8 meta-cognition layer.

    Reads *config.meta.enabled* to decide whether to build components.
    When disabled, returns a result with all fields set to ``None``.

    Parameters
    ----------
    config:
        Full daemon configuration.  Only ``config.meta`` is consulted in
        this factory; the caller is expected to have already checked
        ``config.meta.enabled`` before calling.
    intents:
        Intention store used by the observer to analyse intent patterns.
    journal:
        Action journal used by the observer to trace execution outcomes.
    registry:
        Plugin registry used by the observer for per-plugin health stats.
    narrative_memory:
        Narrative memory used by the observer for token/investigation data.
    """
    if not config.meta.enabled:
        return MetaSystemResult(
            meta_loop=None,
            meta_store=None,
            meta_config_store=None,
            meta_approval_queue=None,
            meta_adjuster=None,
        )

    # -- Stores ------------------------------------------------------------
    meta_config_store = InMemoryConfigStore(
        {
            "intention.min_salience": config.intention.min_salience,
            "intention.min_confidence": config.intention.min_confidence,
            "intention.interval_seconds": float(config.intention.interval_seconds),
        }
    )
    meta_store = InMemoryMetaStore()
    meta_event_bus = LoggingMetaEventBus()
    meta_approval_queue = InMemoryApprovalQueue()

    # -- Observer ----------------------------------------------------------
    meta_observer = MetaObserver(
        intention_store=intents,
        action_store=journal,
        plugin_registry=registry,  # type: ignore[arg-type]
        narrative_store=narrative_memory,
    )

    # -- Evaluator ---------------------------------------------------------
    meta_evaluator = PolicyEvaluator(
        policies=DEFAULT_POLICIES,
        adjustment_history=_AdjustmentHistoryAdapter(meta_store),
        config_reader=_ConfigReaderAdapter(meta_config_store),
    )

    # -- Validator ---------------------------------------------------------
    meta_validator = MetaSafetyValidator(FORBIDDEN_PARAMETER_PATHS, HARD_BOUNDS)

    # -- Adjuster ----------------------------------------------------------
    meta_adjuster = MetaAdjuster(
        config_store=meta_config_store,
        meta_store=meta_store,
        event_bus=meta_event_bus,
    )

    # -- Loop --------------------------------------------------------------
    meta_loop = MetaLoop(
        observer=meta_observer,
        evaluator=meta_evaluator,
        validator=meta_validator,
        adjuster=meta_adjuster,
        meta_store=meta_store,
        approval_queue=meta_approval_queue,
        config=config.meta,
    )
    await meta_loop.start()
    log.info("daemon.meta_loop_started")

    return MetaSystemResult(
        meta_loop=meta_loop,
        meta_store=meta_store,
        meta_config_store=meta_config_store,
        meta_approval_queue=meta_approval_queue,
        meta_adjuster=meta_adjuster,
    )
