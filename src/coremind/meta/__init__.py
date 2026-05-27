"""Meta-cognition layer (L8).

Public surface:
- :class:`MetaObservation` — a single measured metric about system performance.
- :class:`AdjustmentPolicy` — a rule that maps observations to parameter changes.
- :class:`AdjustmentRecord` — record of an applied adjustment.
- :class:`ProposedAdjustment` — adjustment proposed by the evaluator, pending validation.
- :class:`ValidationResult` — result of safety validation.
- :class:`MetaConfig` — configuration for the meta-loop.
- :class:`MetaObserver` — collects system performance observations.
- :class:`PolicyEvaluator` — matches observations to policies, proposes adjustments.
- :class:`MetaSafetyValidator` — enforces forbidden paths and hard bounds.
- :class:`MetaAdjuster` — applies validated adjustments to config.
- :class:`MetaLoop` — async orchestrator: observe → evaluate → validate → adjust.
- :data:`FORBIDDEN_PARAMETER_PATHS` — paths L8 cannot modify.
- :data:`HARD_BOUNDS` — min/max bounds per adjustable parameter.
- :data:`DEFAULT_POLICIES` — built-in adjustment policies.
"""

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
    ActionStoreProtocol,
    AdjustmentHistoryProtocol,
    ApprovalQueueProtocol,
    ConfigReaderProtocol,
    ConfigStoreProtocol,
    IntentionStoreProtocol,
    InvestigationSummary,
    MetaEventBusProtocol,
    MetaStoreProtocol,
    NarrativeStoreProtocol,
    PluginRegistryProtocol,
    PluginStats,
)
from coremind.meta.safety_validator import MetaSafetyValidator
from coremind.meta.schemas import (
    AdjustmentPolicy,
    AdjustmentRecord,
    MetaConfig,
    MetaObservation,
    MetaStatus,
    ProposedAdjustment,
    ValidationResult,
)
from coremind.meta.stores import (
    InMemoryApprovalQueue,
    InMemoryConfigStore,
    InMemoryMetaStore,
    LoggingMetaEventBus,
)

__all__ = [
    "DEFAULT_POLICIES",
    "FORBIDDEN_PARAMETER_PATHS",
    "HARD_BOUNDS",
    "ActionStoreProtocol",
    "AdjustmentHistoryProtocol",
    "AdjustmentPolicy",
    "AdjustmentRecord",
    "ApprovalQueueProtocol",
    "ConfigReaderProtocol",
    "ConfigStoreProtocol",
    "InMemoryApprovalQueue",
    "InMemoryConfigStore",
    "InMemoryMetaStore",
    "IntentionStoreProtocol",
    "InvestigationSummary",
    "LoggingMetaEventBus",
    "MetaAdjuster",
    "MetaConfig",
    "MetaEventBusProtocol",
    "MetaLoop",
    "MetaObservation",
    "MetaObserver",
    "MetaSafetyValidator",
    "MetaStatus",
    "MetaStoreProtocol",
    "NarrativeStoreProtocol",
    "PluginRegistryProtocol",
    "PluginStats",
    "PolicyEvaluator",
    "ProposedAdjustment",
    "ValidationResult",
]
