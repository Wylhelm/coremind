"""Reflection layer (L7).

Public surface:

- :class:`ReflectionLoop` — scheduled weekly self-evaluation cycle.
- :class:`ReflectionLoopConfig` — scheduler configuration.
- :class:`ReflectionReport` — aggregated cycle output.
- :class:`PredictionEvaluatorImpl` — default prediction evaluator (Task 4.2).
- :class:`Calibrator` — default calibration updater (Task 4.3).
- :class:`RuleLearnerImpl` — default rule learner (Task 4.4).
- :class:`MarkdownReportProducer` — default Markdown weekly report (Task 4.5).
- :class:`SurrealReflectionStore` — SurrealDB-backed persistence for L7.
"""

from coremind.reflection.calibration import (
    BUCKET_COUNT,
    DEFAULT_LAYER,
    MIN_BUCKET_SAMPLES_FOR_CORRECTION,
    CalibrationBucket,
    CalibrationStore,
    Calibrator,
    InMemoryCalibrationStore,
    ReliabilityDiagram,
    correct_confidence,
    empty_diagram,
)
from coremind.reflection.evaluator import (
    ConditionResolver,
    EventHistorySource,
    InMemoryPredictionEvaluationStore,
    PredictionEvaluation,
    PredictionEvaluationStore,
    PredictionEvaluatorImpl,
    Verdict,
)
from coremind.reflection.loop import (
    ActionFeed,
    CalibrationUpdater,
    CycleSource,
    FeedbackEvaluator,
    IntentSource,
    PredictionEvaluator,
    ReflectionLoop,
    ReflectionLoopConfig,
    ReportNotifier,
    ReportProducer,
    RuleLearner,
)
from coremind.reflection.report import MarkdownReportProducer
from coremind.reflection.rule_learner import (
    CandidateKey,
    CandidateLedger,
    CandidateObservation,
    CandidateStats,
    InMemoryCandidateLedger,
    InMemoryRuleProposalStore,
    RuleLearnerConfig,
    RuleLearnerImpl,
    RuleProposal,
    RuleProposalStore,
    RuleSource,
)
from coremind.reflection.schemas import (
    CalibrationResult,
    FeedbackEvaluationResult,
    PredictionEvaluationResult,
    ReflectionReport,
    RuleLearningResult,
)
from coremind.reflection.store import (
    SurrealCalibrationStore,
    SurrealPredictionEvaluationStore,
    SurrealReflectionStore,
)

__all__ = [
    "BUCKET_COUNT",
    "DEFAULT_LAYER",
    "MIN_BUCKET_SAMPLES_FOR_CORRECTION",
    "ActionFeed",
    "CalibrationBucket",
    "CalibrationResult",
    "CalibrationStore",
    "CalibrationUpdater",
    "Calibrator",
    "CandidateKey",
    "CandidateLedger",
    "CandidateObservation",
    "CandidateStats",
    "ConditionResolver",
    "CycleSource",
    "EventHistorySource",
    "FeedbackEvaluationResult",
    "FeedbackEvaluator",
    "InMemoryCalibrationStore",
    "InMemoryCandidateLedger",
    "InMemoryPredictionEvaluationStore",
    "InMemoryRuleProposalStore",
    "IntentSource",
    "MarkdownReportProducer",
    "PredictionEvaluation",
    "PredictionEvaluationResult",
    "PredictionEvaluationStore",
    "PredictionEvaluator",
    "PredictionEvaluatorImpl",
    "ReflectionLoop",
    "ReflectionLoopConfig",
    "ReflectionReport",
    "ReliabilityDiagram",
    "ReportNotifier",
    "ReportProducer",
    "RuleLearner",
    "RuleLearnerConfig",
    "RuleLearnerImpl",
    "RuleLearningResult",
    "RuleProposal",
    "RuleProposalStore",
    "RuleSource",
    "SurrealCalibrationStore",
    "SurrealPredictionEvaluationStore",
    "SurrealReflectionStore",
    "Verdict",
    "correct_confidence",
    "empty_diagram",
]
