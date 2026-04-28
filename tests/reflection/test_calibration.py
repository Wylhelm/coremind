"""Tests for the L7 calibration module (Task 4.3)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from coremind.errors import ReflectionError
from coremind.reasoning.schemas import Prediction, ReasoningOutput, TokenUsage
from coremind.reflection.calibration import (
    BUCKET_COUNT,
    MIN_BUCKET_SAMPLES_FOR_CORRECTION,
    CalibrationBucket,
    Calibrator,
    InMemoryCalibrationStore,
    ReliabilityDiagram,
    correct_confidence,
    empty_diagram,
)
from coremind.reflection.evaluator import (
    InMemoryPredictionEvaluationStore,
    PredictionEvaluation,
    Verdict,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
_CYCLE_TS = _NOW - timedelta(days=2)


def _cycle(cycle_id: str, *, model: str = "anthropic/claude-opus-4-7") -> ReasoningOutput:
    return ReasoningOutput(
        cycle_id=cycle_id,
        timestamp=_CYCLE_TS,
        model_used=model,
        predictions=[
            Prediction(
                id="p1",
                hypothesis="hyp",
                horizon_hours=24,
                confidence=0.5,
                falsifiable_by="x",
            )
        ],
        token_usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _eval(
    cycle_id: str,
    pid: str,
    *,
    confidence: float,
    verdict: Verdict,
    evaluated_at: datetime = _NOW,
) -> PredictionEvaluation:
    return PredictionEvaluation(
        cycle_id=cycle_id,
        prediction_id=pid,
        hypothesis="h",
        falsifiable_by="o",
        prediction_timestamp=_CYCLE_TS,
        horizon_end=_CYCLE_TS + timedelta(hours=24),
        confidence=confidence,
        verdict=verdict,
        rationale="r",
        evaluated_at=evaluated_at,
    )


def _clock() -> datetime:
    return _NOW


_WINDOW_END: datetime = _NOW
_WINDOW_START: datetime = _NOW - timedelta(days=7)


# ---------------------------------------------------------------------------
# Schema-level behaviour
# ---------------------------------------------------------------------------


def test_empty_diagram_has_ten_consecutive_buckets() -> None:
    diagram = empty_diagram("reasoning", "m")

    assert len(diagram.buckets) == BUCKET_COUNT
    assert diagram.total_samples == 0
    assert diagram.brier_score is None
    # Bucket boundaries tile [0, 1] without gaps or overlaps.
    for i, b in enumerate(diagram.buckets):
        assert b.lower == pytest.approx(i / BUCKET_COUNT)
        assert b.upper == pytest.approx((i + 1) / BUCKET_COUNT)
        assert b.sample_count == 0
        assert b.empirical_rate is None


def test_bucket_rejects_success_count_above_sample_count() -> None:
    with pytest.raises(ValueError, match="success_count"):
        CalibrationBucket(lower=0.0, upper=0.1, sample_count=1, success_count=2)


def test_diagram_rejects_wrong_bucket_count() -> None:
    short = [CalibrationBucket(lower=0.0, upper=0.5)]
    with pytest.raises(ValueError, match="must have"):
        ReliabilityDiagram(layer="reasoning", model="m", buckets=short)


def test_diagram_rejects_total_sample_mismatch() -> None:
    buckets = [
        CalibrationBucket(lower=i / BUCKET_COUNT, upper=(i + 1) / BUCKET_COUNT)
        for i in range(BUCKET_COUNT)
    ]
    with pytest.raises(ValueError, match="disagrees"):
        ReliabilityDiagram(layer="reasoning", model="m", buckets=buckets, total_samples=3)


def test_correct_confidence_returns_input_when_no_diagram() -> None:
    assert correct_confidence(0.7, None) == 0.7


def test_correct_confidence_returns_input_when_bucket_below_threshold() -> None:
    diagram = empty_diagram("reasoning", "m")
    # only one sample in bucket 7
    diagram = diagram.model_copy(
        update={
            "buckets": [
                b.model_copy(update={"sample_count": 1, "success_count": 1}) if i == 7 else b
                for i, b in enumerate(diagram.buckets)
            ],
            "total_samples": 1,
            "brier_sum_squared_error": 0.09,
        }
    )

    assert correct_confidence(0.75, diagram, min_samples=5) == 0.75


def test_correct_confidence_returns_empirical_rate_when_threshold_met() -> None:
    diagram = empty_diagram("reasoning", "m")
    # bucket 7 is [0.7, 0.8): 10 samples, 6 successes -> empirical 0.6
    diagram = diagram.model_copy(
        update={
            "buckets": [
                b.model_copy(update={"sample_count": 10, "success_count": 6}) if i == 7 else b
                for i, b in enumerate(diagram.buckets)
            ],
            "total_samples": 10,
            "brier_sum_squared_error": 1.0,
        }
    )

    assert correct_confidence(0.75, diagram) == pytest.approx(0.6)


def test_correct_confidence_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="outside"):
        correct_confidence(1.5, None)


# ---------------------------------------------------------------------------
# Calibrator behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_buckets_definite_evaluations_and_excludes_undetermined() -> None:
    eval_store = InMemoryPredictionEvaluationStore()
    cal_store = InMemoryCalibrationStore()
    await eval_store.store(
        [
            _eval("c1", "p1", confidence=0.75, verdict="correct"),
            _eval("c1", "p2", confidence=0.75, verdict="wrong"),
            _eval("c1", "p3", confidence=0.05, verdict="wrong"),
            _eval("c1", "p4", confidence=0.95, verdict="correct"),
            # Undetermined must be excluded from buckets and Brier.
            _eval("c1", "p5", confidence=0.5, verdict="undetermined"),
        ]
    )

    calibrator = Calibrator(eval_store, cal_store, clock=_clock)
    cycles = [_cycle("c1", model="m1")]

    result = await calibrator.update(
        cycles,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    assert result.sample_count == 4
    # Brier sum of squared errors over the four definite evaluations equals
    # 0.0625 + 0.5625 + 0.0025 + 0.0025 = 0.63; mean across four is 0.1575.
    assert result.brier_score == pytest.approx(0.1575)

    diagram = await cal_store.get("reasoning", "m1")
    assert diagram is not None
    assert diagram.total_samples == 4
    assert diagram.brier_score == pytest.approx(0.1575)
    # Bucket [0.7, 0.8): 2 samples, 1 success
    assert diagram.buckets[7].sample_count == 2
    assert diagram.buckets[7].success_count == 1
    # Bucket [0.0, 0.1): 1 sample, 0 successes
    assert diagram.buckets[0].sample_count == 1
    assert diagram.buckets[0].success_count == 0
    # Bucket [0.9, 1.0]: confidence=0.95 -> 1 sample, 1 success
    assert diagram.buckets[9].sample_count == 1
    assert diagram.buckets[9].success_count == 1


@pytest.mark.asyncio
async def test_update_partitions_by_model() -> None:
    eval_store = InMemoryPredictionEvaluationStore()
    cal_store = InMemoryCalibrationStore()
    await eval_store.store(
        [
            _eval("c1", "p1", confidence=0.8, verdict="correct"),
            _eval("c2", "p1", confidence=0.2, verdict="wrong"),
        ]
    )
    cycles = [_cycle("c1", model="m1"), _cycle("c2", model="m2")]

    calibrator = Calibrator(eval_store, cal_store, clock=_clock)
    result = await calibrator.update(
        cycles,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    assert result.sample_count == 2
    diagrams = sorted(await cal_store.list_all(), key=lambda d: d.model)
    assert [d.model for d in diagrams] == ["m1", "m2"]
    assert diagrams[0].total_samples == 1
    assert diagrams[1].total_samples == 1


@pytest.mark.asyncio
async def test_update_high_water_mark_prevents_double_counting() -> None:
    eval_store = InMemoryPredictionEvaluationStore()
    cal_store = InMemoryCalibrationStore()

    first_ts = _NOW - timedelta(hours=2)
    second_ts = _NOW

    await eval_store.store(
        [
            _eval(
                "c1",
                "p1",
                confidence=0.6,
                verdict="correct",
                evaluated_at=first_ts,
            )
        ]
    )

    clock_value = first_ts + timedelta(seconds=1)

    def clock() -> datetime:
        return clock_value

    calibrator = Calibrator(eval_store, cal_store, clock=clock)
    first = await calibrator.update(
        [_cycle("c1", model="m1")],
        window_start=_WINDOW_START,
        window_end=clock_value,
    )
    assert first.sample_count == 1

    # New evaluation arrives later; old one must not be re-counted.
    await eval_store.store(
        [
            _eval(
                "c1",
                "p2",
                confidence=0.4,
                verdict="wrong",
                evaluated_at=second_ts,
            )
        ]
    )
    clock_value = second_ts + timedelta(seconds=1)
    second = await calibrator.update(
        [_cycle("c1", model="m1")],
        window_start=_WINDOW_START,
        window_end=clock_value,
    )
    assert second.sample_count == 1

    diagram = await cal_store.get("reasoning", "m1")
    assert diagram is not None
    assert diagram.total_samples == 2  # 1 + 1, not 1 + 2


@pytest.mark.asyncio
async def test_update_with_no_definite_evaluations_returns_empty_result() -> None:
    eval_store = InMemoryPredictionEvaluationStore()
    cal_store = InMemoryCalibrationStore()
    await eval_store.store([_eval("c1", "p1", confidence=0.5, verdict="undetermined")])

    calibrator = Calibrator(eval_store, cal_store, clock=_clock)
    result = await calibrator.update(
        [_cycle("c1")],
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    assert result.sample_count == 0
    assert result.brier_score is None
    assert await cal_store.list_all() == []


@pytest.mark.asyncio
async def test_update_routes_evaluations_without_matching_cycle_to_unknown_model() -> None:
    eval_store = InMemoryPredictionEvaluationStore()
    cal_store = InMemoryCalibrationStore()
    await eval_store.store([_eval("c-orphan", "p1", confidence=0.7, verdict="correct")])

    calibrator = Calibrator(eval_store, cal_store, clock=_clock)
    # Cycles list is empty: the eval cannot be mapped to a model.
    result = await calibrator.update(
        [],
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    assert result.sample_count == 1
    diagrams = await cal_store.list_all()
    assert len(diagrams) == 1
    assert diagrams[0].model == "unknown"


@pytest.mark.asyncio
async def test_update_wraps_eval_store_failures_as_reflection_error() -> None:
    class _BoomEvalStore:
        async def store(self, evaluations: Sequence[PredictionEvaluation]) -> None:
            return None

        async def list_since(
            self,
            since: datetime,
            until: datetime | None = None,
        ) -> list[PredictionEvaluation]:
            raise RuntimeError("db gone")

    calibrator = Calibrator(_BoomEvalStore(), InMemoryCalibrationStore(), clock=_clock)
    with pytest.raises(ReflectionError, match="failed to load"):
        await calibrator.update(
            [],
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
        )


@pytest.mark.asyncio
async def test_correct_uses_stored_diagram() -> None:
    eval_store = InMemoryPredictionEvaluationStore()
    cal_store = InMemoryCalibrationStore()
    # Seed bucket [0.7, 0.8) with enough samples to trigger correction.
    await eval_store.store(
        [
            _eval(f"c{i}", "p1", confidence=0.75, verdict="correct" if i < 6 else "wrong")
            for i in range(MIN_BUCKET_SAMPLES_FOR_CORRECTION + 5)
        ]
    )
    cycles = [_cycle(f"c{i}", model="m1") for i in range(MIN_BUCKET_SAMPLES_FOR_CORRECTION + 5)]

    calibrator = Calibrator(eval_store, cal_store, clock=_clock)
    await calibrator.update(
        cycles,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    corrected = await calibrator.correct(0.75, model="m1")
    diagram = await cal_store.get("reasoning", "m1")
    assert diagram is not None
    expected = diagram.buckets[7].empirical_rate
    assert expected is not None
    assert corrected == pytest.approx(expected)
    assert corrected != 0.75  # correction actually moved the value


def test_calibrator_rejects_empty_layer() -> None:
    with pytest.raises(ValueError, match="layer"):
        Calibrator(
            InMemoryPredictionEvaluationStore(),
            InMemoryCalibrationStore(),
            layer="",
        )


# ---------------------------------------------------------------------------
# Watermark persistence (Task 4.3 review fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watermark_survives_restart_and_prevents_double_counting() -> None:
    """A fresh Calibrator instance must not re-fold history.

    The high-water mark lives on the persisted diagram, so reconstructing
    the calibrator (e.g. after a daemon restart) does not double-count
    evaluations that were already folded.
    """
    eval_store = InMemoryPredictionEvaluationStore()
    cal_store = InMemoryCalibrationStore()
    await eval_store.store(
        [
            _eval(
                "c1",
                "p1",
                confidence=0.8,
                verdict="correct",
                evaluated_at=_NOW - timedelta(hours=2),
            ),
            _eval(
                "c1",
                "p2",
                confidence=0.2,
                verdict="wrong",
                evaluated_at=_NOW - timedelta(hours=1),
            ),
        ]
    )
    cycles = [_cycle("c1", model="m1")]

    first = Calibrator(eval_store, cal_store, clock=_clock)
    result = await first.update(cycles, window_start=_WINDOW_START, window_end=_WINDOW_END)
    assert result.sample_count == 2

    # Simulate a daemon restart: brand-new instance, same persisted store.
    restarted = Calibrator(eval_store, cal_store, clock=_clock)
    second = await restarted.update(cycles, window_start=_WINDOW_START, window_end=_WINDOW_END)
    assert second.sample_count == 0  # nothing new

    diagram = await cal_store.get("reasoning", "m1")
    assert diagram is not None
    assert diagram.total_samples == 2  # unchanged across restart


@pytest.mark.asyncio
async def test_partial_failure_does_not_double_count_succeeded_models() -> None:
    """When persisting model B fails, model A's evals stay folded once.

    The per-(layer, model) watermark is written atomically with the
    diagram, so a subsequent retry sees model A's watermark already
    advanced and does not re-fold its evaluations.
    """
    eval_store = InMemoryPredictionEvaluationStore()
    await eval_store.store(
        [
            _eval(
                "c1",
                "p1",
                confidence=0.8,
                verdict="correct",
                evaluated_at=_NOW - timedelta(hours=2),
            ),
            _eval(
                "c2",
                "p1",
                confidence=0.2,
                verdict="wrong",
                evaluated_at=_NOW - timedelta(hours=1),
            ),
        ]
    )

    class _FlakyCalStore(InMemoryCalibrationStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_for: set[str] = set()

        async def put(self, diagram: ReliabilityDiagram) -> None:
            if diagram.model in self.fail_for:
                raise RuntimeError(f"boom on {diagram.model}")
            await super().put(diagram)

    cal_store = _FlakyCalStore()
    cal_store.fail_for = {"m2"}
    cycles = [_cycle("c1", model="m1"), _cycle("c2", model="m2")]

    calibrator = Calibrator(eval_store, cal_store, clock=_clock)
    with pytest.raises(ReflectionError, match="failed to persist"):
        await calibrator.update(cycles, window_start=_WINDOW_START, window_end=_WINDOW_END)

    # m1's diagram persisted; m2's did not.
    m1 = await cal_store.get("reasoning", "m1")
    m2 = await cal_store.get("reasoning", "m2")
    assert m1 is not None
    assert m1.total_samples == 1
    assert m2 is None

    # Retry succeeds and only folds m2's evaluation; m1 is not re-folded.
    cal_store.fail_for = set()
    retry = await calibrator.update(cycles, window_start=_WINDOW_START, window_end=_WINDOW_END)
    assert retry.sample_count == 1

    m1_after = await cal_store.get("reasoning", "m1")
    m2_after = await cal_store.get("reasoning", "m2")
    assert m1_after is not None
    assert m1_after.total_samples == 1  # not double-counted
    assert m2_after is not None
    assert m2_after.total_samples == 1


@pytest.mark.asyncio
async def test_correct_threshold_is_overridable() -> None:
    """``Calibrator.correct`` must propagate ``min_samples`` to
    :func:`correct_confidence` so callers can tune the threshold."""
    eval_store = InMemoryPredictionEvaluationStore()
    cal_store = InMemoryCalibrationStore()
    # Seed bucket [0.7, 0.8) with exactly 2 samples, both correct.
    await eval_store.store(
        [
            _eval("c1", "p1", confidence=0.75, verdict="correct"),
            _eval("c2", "p1", confidence=0.75, verdict="correct"),
        ]
    )
    cycles = [_cycle("c1", model="m1"), _cycle("c2", model="m1")]
    calibrator = Calibrator(eval_store, cal_store, clock=_clock)
    await calibrator.update(cycles, window_start=_WINDOW_START, window_end=_WINDOW_END)

    # Default threshold (5) → too few samples, returns input unchanged.
    assert await calibrator.correct(0.75, model="m1") == 0.75
    # Lowered threshold → returns the empirical rate (1.0).
    assert await calibrator.correct(0.75, model="m1", min_samples=2) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_diagram_persists_last_evaluated_at() -> None:
    """The folded diagram exposes the watermark on its
    ``last_evaluated_at`` field for downstream observability."""
    eval_store = InMemoryPredictionEvaluationStore()
    cal_store = InMemoryCalibrationStore()
    latest = _NOW - timedelta(minutes=5)
    await eval_store.store(
        [
            _eval(
                "c1",
                "p1",
                confidence=0.5,
                verdict="correct",
                evaluated_at=_NOW - timedelta(hours=1),
            ),
            _eval(
                "c1",
                "p2",
                confidence=0.5,
                verdict="wrong",
                evaluated_at=latest,
            ),
        ]
    )
    calibrator = Calibrator(eval_store, cal_store, clock=_clock)
    await calibrator.update(
        [_cycle("c1", model="m1")],
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    diagram = await cal_store.get("reasoning", "m1")
    assert diagram is not None
    assert diagram.last_evaluated_at == latest


def test_diagram_rejects_watermark_when_empty() -> None:
    buckets = [
        CalibrationBucket(lower=i / BUCKET_COUNT, upper=(i + 1) / BUCKET_COUNT)
        for i in range(BUCKET_COUNT)
    ]
    with pytest.raises(ValueError, match="last_evaluated_at"):
        ReliabilityDiagram(
            layer="reasoning",
            model="m",
            buckets=buckets,
            total_samples=0,
            last_evaluated_at=_NOW,
        )


def test_diagram_requires_watermark_when_populated() -> None:
    buckets = [
        CalibrationBucket(
            lower=i / BUCKET_COUNT,
            upper=(i + 1) / BUCKET_COUNT,
            sample_count=1 if i == 0 else 0,
            success_count=1 if i == 0 else 0,
        )
        for i in range(BUCKET_COUNT)
    ]
    with pytest.raises(ValueError, match="last_evaluated_at"):
        ReliabilityDiagram(
            layer="reasoning",
            model="m",
            buckets=buckets,
            total_samples=1,
            brier_sum_squared_error=0.25,
            last_evaluated_at=None,
        )
