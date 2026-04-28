"""Calibration — Task 4.3.

Tracks the L4 reasoning layer's calibration: how well do the confidence
numbers it reports match the empirical success rate of its predictions?

For every prediction evaluated by :mod:`coremind.reflection.evaluator`
with a definite verdict (``correct`` or ``wrong``), we:

* place it into one of ten reliability buckets keyed by reported
  confidence (``[0.0, 0.1)``, ``[0.1, 0.2)``, …, ``[0.9, 1.0]``);
* update per-``(layer, model)`` running counts (samples, successes,
  sum-of-squared-errors) so the Brier score and the reliability diagram
  can be recomputed without re-reading every historical evaluation.

A well-calibrated system has each bucket's empirical success rate close
to the bucket midpoint.

The :func:`correct_confidence` helper is the calibration correction
function called for in the phase doc: given a fresh confidence and a
``(layer, model)`` pair, it returns the empirical rate of the matching
bucket once that bucket has accumulated at least
``MIN_BUCKET_SAMPLES_FOR_CORRECTION`` samples; otherwise it returns the
input unchanged.

Per the project's "no DB writes outside ``store.py``" rule, durable
storage of the reliability diagram lives behind the
:class:`CalibrationStore` port; the in-memory implementation shipped
here keeps the L7 loop runnable in tests, while the SurrealDB-backed
adapter is provided by :mod:`coremind.reflection.store`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, Self

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from coremind.errors import ReflectionError
from coremind.reasoning.schemas import ReasoningOutput
from coremind.reflection.evaluator import PredictionEvaluation, PredictionEvaluationStore
from coremind.reflection.loop import CalibrationUpdater
from coremind.reflection.schemas import CalibrationResult

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


BUCKET_COUNT: int = 10
"""Number of reliability buckets covering the ``[0.0, 1.0]`` range."""

MIN_BUCKET_SAMPLES_FOR_CORRECTION: int = 5
"""Minimum bucket population before :func:`correct_confidence` overrides
the input.  Below this threshold there is too little evidence to claim
the empirical rate is more trustworthy than the model's own confidence."""

DEFAULT_LAYER: str = "reasoning"
"""Layer label used by :class:`Calibrator` when none is supplied.  L4 is
the only confidence-emitting layer wired through prediction evaluation
in Phase 4; intention-layer calibration is a follow-up."""


type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CalibrationBucket(BaseModel):
    """One reliability bucket covering a confidence sub-range.

    Attributes:
        lower: Inclusive lower bound of the bucket.
        upper: Exclusive upper bound — except for the final bucket whose
            upper bound is inclusive so that ``confidence == 1.0``
            always lands somewhere.
        sample_count: Number of definite evaluations whose confidence
            fell in this bucket.
        success_count: Subset of ``sample_count`` whose verdict was
            ``correct``.
    """

    model_config = ConfigDict(frozen=True)

    lower: float = Field(ge=0.0, le=1.0)
    upper: float = Field(ge=0.0, le=1.0)
    sample_count: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.upper < self.lower:
            msg = f"bucket upper ({self.upper}) < lower ({self.lower})"
            raise ValueError(msg)
        if self.success_count > self.sample_count:
            msg = (
                f"bucket success_count ({self.success_count}) > sample_count ({self.sample_count})"
            )
            raise ValueError(msg)
        return self

    @property
    def midpoint(self) -> float:
        """Confidence midpoint of the bucket — the value a perfectly
        calibrated model would emit for this bucket's success rate."""
        return (self.lower + self.upper) / 2.0

    @property
    def empirical_rate(self) -> float | None:
        """Empirical success rate for the bucket, or ``None`` when the
        bucket has not yet been observed."""
        if self.sample_count == 0:
            return None
        return self.success_count / self.sample_count


class ReliabilityDiagram(BaseModel):
    """Calibration data for one ``(layer, model)`` pair.

    The diagram stores enough information to render a reliability plot
    (per-bucket midpoint vs empirical rate) and to recover the Brier
    score without re-reading the underlying evaluations:

    * ``brier_sum_squared_error`` is the running sum of
      ``(confidence - outcome)^2`` over all definite evaluations folded
      into the diagram, where ``outcome`` is ``1.0`` for ``correct`` and
      ``0.0`` for ``wrong``.
    * ``total_samples`` is the count of definite evaluations folded in.
    * ``last_evaluated_at`` is the maximum ``evaluated_at`` of every
      evaluation already folded into this diagram.  It is the
      per-``(layer, model)`` high-water mark used by :class:`Calibrator`
      to guarantee at-most-once folding across daemon restarts and
      across partial failures of a multi-model update.  Persisted
      atomically with the rest of the diagram by the calibration store.
    """

    model_config = ConfigDict(frozen=True)

    layer: str = Field(min_length=1)
    model: str = Field(min_length=1)
    buckets: list[CalibrationBucket]
    total_samples: int = Field(default=0, ge=0)
    brier_sum_squared_error: float = Field(default=0.0, ge=0.0)
    last_evaluated_at: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if len(self.buckets) != BUCKET_COUNT:
            msg = f"reliability diagram must have {BUCKET_COUNT} buckets, got {len(self.buckets)}"
            raise ValueError(msg)
        bucket_total = sum(b.sample_count for b in self.buckets)
        if bucket_total != self.total_samples:
            msg = (
                f"diagram total_samples={self.total_samples} disagrees with "
                f"bucket sum {bucket_total}"
            )
            raise ValueError(msg)
        if self.last_evaluated_at is not None and self.last_evaluated_at.tzinfo is None:
            msg = "last_evaluated_at must be timezone-aware"
            raise ValueError(msg)
        if self.total_samples == 0 and self.last_evaluated_at is not None:
            msg = "last_evaluated_at must be None when total_samples == 0"
            raise ValueError(msg)
        if self.total_samples > 0 and self.last_evaluated_at is None:
            msg = "last_evaluated_at must be set when total_samples > 0"
            raise ValueError(msg)
        return self

    @property
    def brier_score(self) -> float | None:
        """Mean squared error between confidence and outcome, or ``None``
        when no definite evaluations have been folded in."""
        if self.total_samples == 0:
            return None
        return self.brier_sum_squared_error / self.total_samples


def empty_diagram(layer: str, model: str) -> ReliabilityDiagram:
    """Return a fresh diagram with ``BUCKET_COUNT`` empty buckets."""
    width = 1.0 / BUCKET_COUNT
    buckets = [
        CalibrationBucket(lower=i * width, upper=(i + 1) * width) for i in range(BUCKET_COUNT)
    ]
    return ReliabilityDiagram(layer=layer, model=model, buckets=buckets)


def _bucket_index(confidence: float) -> int:
    """Return the bucket index for ``confidence`` in ``[0.0, 1.0]``.

    The final bucket is closed on the right so that ``confidence == 1.0``
    is folded into the top bucket rather than overflowing.
    """
    if not 0.0 <= confidence <= 1.0:
        msg = f"confidence {confidence} outside [0.0, 1.0]"
        raise ValueError(msg)
    if confidence >= 1.0:
        return BUCKET_COUNT - 1
    return int(confidence * BUCKET_COUNT)


def correct_confidence(
    confidence: float,
    diagram: ReliabilityDiagram | None,
    *,
    min_samples: int = MIN_BUCKET_SAMPLES_FOR_CORRECTION,
) -> float:
    """Adjust ``confidence`` toward the empirical rate of its bucket.

    Args:
        confidence: Reported confidence in ``[0.0, 1.0]``.
        diagram: Reliability diagram for the relevant ``(layer, model)``
            pair, or ``None`` when no calibration data exists yet.
        min_samples: Minimum bucket population before the empirical rate
            replaces the input.  Defaults to
            :data:`MIN_BUCKET_SAMPLES_FOR_CORRECTION`.

    Returns:
        The empirical success rate of the matching bucket when it has
        seen at least ``min_samples`` definite evaluations; otherwise
        the original ``confidence`` is returned unchanged.
    """
    if not 0.0 <= confidence <= 1.0:
        msg = f"confidence {confidence} outside [0.0, 1.0]"
        raise ValueError(msg)
    if diagram is None:
        return confidence
    bucket = diagram.buckets[_bucket_index(confidence)]
    if bucket.sample_count < min_samples or bucket.empirical_rate is None:
        return confidence
    return bucket.empirical_rate


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class CalibrationStore(Protocol):
    """Persists per-``(layer, model)`` reliability diagrams.

    Implementations must be safe for serial use by the reflection loop
    (only L7 writes calibration state) and idempotent on
    ``(layer, model)``: ``put`` overwrites the existing diagram for the
    pair.
    """

    async def get(self, layer: str, model: str) -> ReliabilityDiagram | None:
        """Return the diagram for ``(layer, model)`` or ``None`` if
        nothing has been stored yet."""
        ...

    async def put(self, diagram: ReliabilityDiagram) -> None:
        """Replace the stored diagram for
        ``(diagram.layer, diagram.model)``."""
        ...

    async def list_all(self) -> list[ReliabilityDiagram]:
        """Return every stored diagram.  Ordering is implementation-
        defined; callers that require a specific order should sort."""
        ...


class InMemoryCalibrationStore:
    """Process-local :class:`CalibrationStore` backing.

    Suitable for tests and for early Phase 4 wiring before the
    SurrealDB-backed adapter lands.  Idempotent on ``(layer, model)``.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ReliabilityDiagram] = {}

    async def get(self, layer: str, model: str) -> ReliabilityDiagram | None:
        return self._rows.get((layer, model))

    async def put(self, diagram: ReliabilityDiagram) -> None:
        self._rows[(diagram.layer, diagram.model)] = diagram

    async def list_all(self) -> list[ReliabilityDiagram]:
        return list(self._rows.values())


# ---------------------------------------------------------------------------
# Calibrator
# ---------------------------------------------------------------------------


class Calibrator(CalibrationUpdater):
    """Default :class:`CalibrationUpdater` implementation.

    Reads per-prediction evaluations persisted by the L7 evaluator, joins
    each row's ``cycle_id`` against the cycles passed in by the loop to
    recover ``model_used``, and folds the definite (``correct`` /
    ``wrong``) verdicts into the matching ``(layer, model)`` reliability
    diagram.  ``undetermined`` verdicts are excluded from both the
    buckets and the Brier score.

    Each diagram persists its own ``last_evaluated_at`` high-water mark
    atomically with its bucket counts.  On every :meth:`update` we:

    1. Read all stored diagrams to recover their watermarks.
    2. List every evaluation strictly newer than the global minimum
       watermark (or every evaluation if no diagram exists yet).
    3. For each model, fold only those evaluations whose
       ``evaluated_at`` is strictly greater than the *target diagram's*
       watermark.  Persisting the diagram advances the watermark
       atomically with the new bucket counts.

    This makes folding at-most-once across daemon restarts and across
    partial failures of a multi-model update — if writing model B's
    diagram fails, model A's already-persisted watermark prevents A's
    evaluations from being re-folded on the next cycle.

    Args:
        eval_store: Source of per-prediction evaluations (the same store
            written by :class:`coremind.reflection.evaluator.PredictionEvaluatorImpl`).
        cal_store: Persistence for reliability diagrams.
        layer: Layer label associated with predictions sourced from
            reasoning cycles.  Defaults to ``"reasoning"``; future work
            will plug intention-layer evaluations behind a separate
            instance.
        unknown_model_label: Label used when an evaluation cannot be
            mapped to a cycle in the current window (e.g. straggler row
            from an earlier window).  Folded into a dedicated diagram
            so the data is not silently dropped; a warning is logged so
            this does not silently accumulate.
        clock: Injectable clock for deterministic tests.  Currently only
            used for log fields; the per-diagram watermark drives the
            actual at-most-once invariant.
    """

    def __init__(
        self,
        eval_store: PredictionEvaluationStore,
        cal_store: CalibrationStore,
        *,
        layer: str = DEFAULT_LAYER,
        unknown_model_label: str = "unknown",
        clock: Clock = _utc_now,
    ) -> None:
        if not layer:
            raise ValueError("layer must be a non-empty string")
        if not unknown_model_label:
            raise ValueError("unknown_model_label must be a non-empty string")
        self._eval_store = eval_store
        self._cal_store = cal_store
        self._layer = layer
        self._unknown_model = unknown_model_label
        self._clock = clock

    async def update(
        self,
        cycles: list[ReasoningOutput],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> CalibrationResult:
        """Fold new evaluations into the reliability diagrams.

        Args:
            cycles: Reasoning cycles in the loop's current window, used
                to map evaluation ``cycle_id`` to the model that
                produced the prediction.  Cycles outside this window
                that still receive late evaluations are routed to the
                ``unknown`` diagram.
            window_start: Inclusive lower bound of the loop's reflection
                window.  Currently informational only — at-most-once is
                enforced by per-diagram watermarks, not the window.
            window_end: Exclusive upper bound of the loop's reflection
                window.

        Returns:
            A :class:`CalibrationResult` whose ``brier_score`` covers
            only the definite evaluations folded *in this update* — not
            the lifetime score.  The lifetime / per-model state lives in
            the calibration store and can be inspected via
            :meth:`CalibrationStore.list_all`.

        Raises:
            ReflectionError: When the eval store or calibration store
                fails.  Adapter exceptions are wrapped with cause.
        """
        global_floor = await self._global_floor()
        new_evaluations = await self._load_new_evaluations(global_floor)
        definite = self._partition_by_model(
            new_evaluations,
            cycles,
            window_start=window_start,
            window_end=window_end,
        )
        if not definite:
            self._log_no_samples(window_start, window_end)
            return CalibrationResult(brier_score=None, sample_count=0)

        cycle_brier_sum, cycle_samples = await self._fold_per_model(definite)
        if cycle_samples == 0:
            self._log_no_samples(window_start, window_end)
            return CalibrationResult(brier_score=None, sample_count=0)

        cycle_brier = cycle_brier_sum / cycle_samples
        log.info(
            "reflection.calibration.updated",
            layer=self._layer,
            sample_count=cycle_samples,
            models=sorted(definite.keys()),
            brier_score=cycle_brier,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )
        return CalibrationResult(brier_score=cycle_brier, sample_count=cycle_samples)

    async def _global_floor(self) -> datetime:
        """Compute the lower bound for the eval-store query.

        Equals ``min(diagram.last_evaluated_at) + 1µs`` over every
        persisted diagram, or the epoch when no diagram exists yet.
        Per-model filtering in :meth:`_fold_per_model` ensures each
        diagram only sees rows it has not folded.
        """
        try:
            stored = await self._cal_store.list_all()
        except Exception as exc:
            raise ReflectionError("failed to list calibration diagrams") from exc
        watermarks = [d.last_evaluated_at for d in stored if d.last_evaluated_at is not None]
        if not watermarks:
            return datetime.fromtimestamp(0, tz=UTC)
        return min(watermarks) + timedelta(microseconds=1)

    async def _load_new_evaluations(self, since: datetime) -> list[PredictionEvaluation]:
        """Fetch evaluations newer than ``since`` from the eval store."""
        try:
            return await self._eval_store.list_since(since)
        except Exception as exc:
            raise ReflectionError(
                "failed to load prediction evaluations for calibration",
            ) from exc

    def _partition_by_model(
        self,
        evaluations: list[PredictionEvaluation],
        cycles: list[ReasoningOutput],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, list[PredictionEvaluation]]:
        """Group definite evaluations by ``model`` for one diagram update each.

        Evaluations whose ``cycle_id`` is not present in ``cycles`` are
        routed to ``self._unknown_model``; a single warning per call is
        emitted so this leading indicator does not silently accumulate.
        """
        cycle_models = {c.cycle_id: c.model_used for c in cycles}
        by_model: dict[str, list[PredictionEvaluation]] = {}
        unknown_count = 0
        for ev in evaluations:
            if ev.verdict == "undetermined":
                continue
            mapped = cycle_models.get(ev.cycle_id)
            model = mapped if mapped is not None else self._unknown_model
            if mapped is None:
                unknown_count += 1
            by_model.setdefault(model, []).append(ev)
        if unknown_count > 0:
            log.warning(
                "reflection.calibration.unknown_model",
                unknown_count=unknown_count,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
            )
        return by_model

    async def _fold_per_model(
        self,
        by_model: dict[str, list[PredictionEvaluation]],
    ) -> tuple[float, int]:
        """Apply the per-model fold and return ``(brier_sse, samples)``."""
        cycle_brier_sum = 0.0
        cycle_samples = 0
        for model, evs in by_model.items():
            diagram = await self._load_or_init_diagram(model)
            wm = diagram.last_evaluated_at
            fresh = [ev for ev in evs if wm is None or ev.evaluated_at > wm]
            if not fresh:
                continue
            updated = _fold_evaluations(diagram, fresh)
            for ev in fresh:
                outcome = 1.0 if ev.verdict == "correct" else 0.0
                cycle_brier_sum += (ev.confidence - outcome) ** 2
                cycle_samples += 1
            try:
                await self._cal_store.put(updated)
            except Exception as exc:
                raise ReflectionError(
                    f"failed to persist calibration diagram for ({self._layer}, {model})",
                ) from exc
        return cycle_brier_sum, cycle_samples

    async def _load_or_init_diagram(self, model: str) -> ReliabilityDiagram:
        """Load the diagram for ``(self._layer, model)`` or seed an empty one."""
        try:
            diagram = await self._cal_store.get(self._layer, model)
        except Exception as exc:
            raise ReflectionError(
                f"failed to load calibration diagram for ({self._layer}, {model})",
            ) from exc
        return diagram if diagram is not None else empty_diagram(self._layer, model)

    def _log_no_samples(self, window_start: datetime, window_end: datetime) -> None:
        """Emit the ``no_new_samples`` info event for an empty cycle."""
        log.info(
            "reflection.calibration.no_new_samples",
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )

    async def correct(
        self,
        confidence: float,
        *,
        layer: str | None = None,
        model: str,
        min_samples: int = MIN_BUCKET_SAMPLES_FOR_CORRECTION,
    ) -> float:
        """Apply :func:`correct_confidence` using the stored diagram for
        ``(layer or self.layer, model)``.

        Args:
            confidence: Reported confidence in ``[0.0, 1.0]``.
            layer: Layer override; defaults to the calibrator's
                configured layer.
            model: Model identifier whose diagram should drive the
                correction.
            min_samples: Minimum bucket population before the empirical
                rate replaces the input.  Forwards to
                :func:`correct_confidence`.
        """
        target_layer = layer or self._layer
        try:
            diagram = await self._cal_store.get(target_layer, model)
        except Exception as exc:
            raise ReflectionError(
                f"failed to load calibration diagram for ({target_layer}, {model})",
            ) from exc
        return correct_confidence(confidence, diagram, min_samples=min_samples)


def _fold_evaluations(
    diagram: ReliabilityDiagram,
    evaluations: list[PredictionEvaluation],
) -> ReliabilityDiagram:
    """Return a new diagram with ``evaluations`` folded into its buckets.

    ``undetermined`` verdicts must be filtered by the caller — folding
    them here would understate the empirical success rate of every
    bucket they touched.

    The diagram's ``last_evaluated_at`` watermark is advanced to the
    maximum ``evaluated_at`` across the supplied evaluations and the
    existing watermark, so persisting the returned diagram atomically
    advances the per-``(layer, model)`` high-water mark.
    """
    if not evaluations:
        return diagram
    new_buckets = list(diagram.buckets)
    extra_sse = 0.0
    extra_samples = 0
    max_evaluated_at = diagram.last_evaluated_at
    for ev in evaluations:
        idx = _bucket_index(ev.confidence)
        outcome = 1.0 if ev.verdict == "correct" else 0.0
        existing = new_buckets[idx]
        new_buckets[idx] = existing.model_copy(
            update={
                "sample_count": existing.sample_count + 1,
                "success_count": existing.success_count + (1 if ev.verdict == "correct" else 0),
            }
        )
        extra_sse += (ev.confidence - outcome) ** 2
        extra_samples += 1
        if max_evaluated_at is None or ev.evaluated_at > max_evaluated_at:
            max_evaluated_at = ev.evaluated_at
    return ReliabilityDiagram(
        layer=diagram.layer,
        model=diagram.model,
        buckets=new_buckets,
        total_samples=diagram.total_samples + extra_samples,
        brier_sum_squared_error=diagram.brier_sum_squared_error + extra_sse,
        last_evaluated_at=max_evaluated_at,
    )
