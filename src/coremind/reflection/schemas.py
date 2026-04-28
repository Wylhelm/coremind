"""Aggregated output schemas for the L7 reflection cycle.

These models are shared between :mod:`coremind.reflection.loop` and the
sibling modules that implement the prediction evaluator, calibration
updater, rule learner and report producer (Tasks 4.2-4.5).  Keeping them
in a dedicated schema module mirrors the layout used by the reasoning,
intention and action layers.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PredictionEvaluationResult(BaseModel):
    """Outcome counts from evaluating predictions against world history.

    Tasks 4.2/4.3 will refine this into a richer schema; the L7 loop only
    needs aggregate counts to render the report and to feed the
    calibrator.
    """

    model_config = ConfigDict(frozen=True)

    evaluated: int = Field(ge=0)
    correct: int = Field(ge=0)
    wrong: int = Field(ge=0)
    undetermined: int = Field(ge=0)


class FeedbackEvaluationResult(BaseModel):
    """Outcome counts from evaluating actions against user feedback."""

    model_config = ConfigDict(frozen=True)

    evaluated: int = Field(ge=0)
    approved: int = Field(ge=0)
    rejected: int = Field(ge=0)
    reversed: int = Field(ge=0)
    dismissed: int = Field(ge=0)


class CalibrationResult(BaseModel):
    """Calibration update summary.

    The full reliability diagram lives behind the calibrator port; the
    loop only carries the headline number into the report.

    Note:
        ``brier_score`` is bounded to ``[0.0, 1.0]`` because the current
        contract assumes binary outcomes (correct / wrong).  When Task 4.3
        extends calibration to multi-class predictions, this bound must
        be revisited.
    """

    model_config = ConfigDict(frozen=True)

    brier_score: float | None = Field(default=None, ge=0.0, le=1.0)
    sample_count: int = Field(ge=0)


class RuleLearningResult(BaseModel):
    """Rule-learning summary for one reflection cycle."""

    model_config = ConfigDict(frozen=True)

    proposed_rule_ids: list[str] = Field(default_factory=list)
    deprecated_rule_ids: list[str] = Field(default_factory=list)


class ReflectionReport(BaseModel):
    """The complete output of one reflection cycle.

    ``markdown`` may be empty when nothing happened in the window (e.g.
    no cycles, no intents, no actions): the report is still emitted so
    the cadence is observable.
    """

    model_config = ConfigDict(frozen=True)

    cycle_id: str = Field(min_length=1)
    window_start: datetime
    window_end: datetime
    cycles_evaluated: int = Field(ge=0)
    intents_evaluated: int = Field(ge=0)
    actions_evaluated: int = Field(ge=0)
    predictions: PredictionEvaluationResult
    feedback: FeedbackEvaluationResult
    calibration: CalibrationResult
    rules: RuleLearningResult
    markdown: str
