"""Prediction evaluation — Task 4.2.

For each :class:`coremind.reasoning.schemas.Prediction` produced by L4,
the evaluator answers one question: did the ``falsifiable_by`` condition
resolve in the world model within ``horizon_hours`` of the prediction
being made?

The verdict is one of:

- ``"correct"``: the resolver determined the hypothesis was borne out.
- ``"wrong"``: the resolver determined the hypothesis was refuted.
- ``"undetermined"``: the horizon has not yet elapsed at evaluation
  time, or the resolver could not decide from the available evidence.

Per-prediction outcomes are persisted to a queryable
:class:`PredictionEvaluationStore` (a default in-memory implementation
is shipped here; a SurrealDB-backed store will live behind
``coremind.world.store`` per the project's "no DB writes outside
``store.py``" rule).  The aggregate counts feed the L7 reflection loop
and the calibrator (Task 4.3).

.. note::
   **Follow-up for Task 4.2:** the SurrealDB-backed
   :class:`PredictionEvaluationStore` adapter is not yet implemented
   here; the in-memory store keeps the L7 loop and Task 4.3 unblocked
   but is process-local.  Durable storage must land before calibration
   data is expected to span weeks.

Free-form interpretation of the ``falsifiable_by`` text is delegated to
a :class:`ConditionResolver` port: this module owns *only* the temporal
slicing, persistence, and aggregation.  Phase 4 wires an LLM-backed
resolver behind the same protocol; tests inject deterministic fakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

from coremind.errors import ReflectionError
from coremind.reasoning.schemas import Prediction, ReasoningOutput
from coremind.reflection.loop import PredictionEvaluator
from coremind.reflection.schemas import PredictionEvaluationResult
from coremind.world.model import WorldEventRecord

log = structlog.get_logger(__name__)


type Verdict = Literal["correct", "wrong", "undetermined"]
type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Per-prediction record
# ---------------------------------------------------------------------------


class PredictionEvaluation(BaseModel):
    """Outcome of evaluating a single :class:`Prediction`.

    Attributes:
        cycle_id: Reasoning cycle that emitted the prediction.
        prediction_id: Stable id of the prediction within the cycle.
        hypothesis: The prediction's natural-language hypothesis,
            captured for queryability without re-joining the cycle.
        falsifiable_by: The concrete observation that would refute the
            prediction (also captured for queryability).
        prediction_timestamp: When the prediction was made.
        horizon_end: ``prediction_timestamp + horizon_hours``.
        confidence: Reported confidence at prediction time, copied so
            calibration buckets remain stable even if a future cycle
            re-emits a related hypothesis.
        verdict: ``correct`` / ``wrong`` / ``undetermined``.
        rationale: Human-readable explanation of the verdict.  The
            evaluator truncates to 1000 characters at construction
            time; ``Field(max_length=1000)`` is a defensive backstop
            so a misbehaving caller cannot exceed the table budget.
        evaluated_at: When the verdict was produced.
    """

    model_config = ConfigDict(frozen=True)

    cycle_id: str = Field(min_length=1)
    prediction_id: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    falsifiable_by: str = Field(min_length=1)
    prediction_timestamp: datetime
    horizon_end: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    verdict: Verdict
    rationale: str = Field(default="", max_length=1000)
    evaluated_at: datetime


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class EventHistorySource(Protocol):
    """Read-only view onto L2 history used as evidence for evaluation."""

    async def events_in_window(
        self,
        after: datetime,
        before: datetime,
        limit: int = 1000,
    ) -> list[WorldEventRecord]:
        """Return events whose timestamp lies in ``(after, before]``.

        Implementations typically delegate to
        :meth:`coremind.world.store.WorldStore.events_in_window`.  The
        Protocol intentionally omits the underlying store's optional
        ``entity`` filter: prediction evaluation considers the whole
        world snapshot in the horizon, not a single entity slice.
        """
        ...


class ConditionResolver(Protocol):
    """Decides whether a prediction's ``falsifiable_by`` condition
    resolved given the evidence collected from L2."""

    async def resolve(
        self,
        prediction: Prediction,
        evidence: Sequence[WorldEventRecord],
    ) -> tuple[Verdict, str]:
        """Return the ``(verdict, rationale)`` for *prediction*.

        Returning ``"undetermined"`` is permitted when the resolver
        cannot decide from the available evidence.  Implementations
        must not raise on inconclusive evidence — they raise only on
        infrastructure failures, which the evaluator wraps as
        :class:`coremind.errors.ReflectionError`.
        """
        ...


class PredictionEvaluationStore(Protocol):
    """Persists per-prediction evaluations for later querying.

    Implementations must be idempotent on ``(cycle_id, prediction_id)``:
    re-running the same reflection window must not duplicate rows.
    """

    async def store(self, evaluations: Sequence[PredictionEvaluation]) -> None:
        """Persist *evaluations*, replacing any existing rows that share
        the same ``(cycle_id, prediction_id)`` key."""
        ...

    async def list_since(
        self,
        since: datetime,
        until: datetime | None = None,
    ) -> list[PredictionEvaluation]:
        """Return evaluations whose ``evaluated_at`` lies in
        ``[since, until)`` (or ``[since, ∞)`` if *until* is ``None``).

        Ordering is implementation-defined; callers that require a
        specific order should sort the result.
        """
        ...


# ---------------------------------------------------------------------------
# In-memory store (default)
# ---------------------------------------------------------------------------


class InMemoryPredictionEvaluationStore:
    """Process-local store backing :class:`PredictionEvaluationStore`.

    Suitable for tests and for early Phase 4 wiring before the SurrealDB
    table lands.  Idempotent on ``(cycle_id, prediction_id)``.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], PredictionEvaluation] = {}

    async def store(self, evaluations: Sequence[PredictionEvaluation]) -> None:
        """Replace rows that share ``(cycle_id, prediction_id)`` with
        the latest evaluation."""
        for ev in evaluations:
            self._rows[(ev.cycle_id, ev.prediction_id)] = ev

    async def list_since(
        self,
        since: datetime,
        until: datetime | None = None,
    ) -> list[PredictionEvaluation]:
        """Return evaluations whose ``evaluated_at`` lies in
        ``[since, until)``, sorted ascending by ``evaluated_at``."""
        rows = [
            ev
            for ev in self._rows.values()
            if ev.evaluated_at >= since and (until is None or ev.evaluated_at < until)
        ]
        rows.sort(key=lambda ev: ev.evaluated_at)
        return rows


# ---------------------------------------------------------------------------
# Concrete evaluator
# ---------------------------------------------------------------------------


class PredictionEvaluatorImpl(PredictionEvaluator):
    """Default implementation of the :class:`PredictionEvaluator` port.

    Args:
        history: Source of L2 events used as evidence for resolution.
        resolver: Port that decides whether a prediction's
            ``falsifiable_by`` condition was met given the evidence.
        store: Persistence port for per-prediction outcomes.
        evidence_limit: Maximum number of events fetched per prediction;
            keeps a single pathological prediction from blowing up the
            cycle's memory budget.  The events_in_window query already
            orders ascending by timestamp, so the cap drops the tail of
            the horizon — preferable to truncating the start.  When the
            cap is hit, a ``reflection.predictions.evidence_truncated``
            warning is logged so calibration drift on capped windows is
            attributable.
        max_concurrent_resolvers: Upper bound on in-flight calls to the
            :class:`ConditionResolver`.  Predictions are otherwise
            independent, so we resolve them concurrently behind a
            semaphore — a meaningful win once the resolver is
            LLM-backed.
        clock: Injectable clock for deterministic tests.
    """

    def __init__(
        self,
        history: EventHistorySource,
        resolver: ConditionResolver,
        store: PredictionEvaluationStore,
        *,
        evidence_limit: int = 1000,
        max_concurrent_resolvers: int = 8,
        clock: Clock = _utc_now,
    ) -> None:
        if max_concurrent_resolvers < 1:
            raise ValueError("max_concurrent_resolvers must be >= 1")
        self._history = history
        self._resolver = resolver
        self._store = store
        self._evidence_limit = evidence_limit
        self._concurrency = max_concurrent_resolvers
        self._clock = clock

    async def evaluate(
        self,
        cycles: list[ReasoningOutput],
        *,
        window_end: datetime,
    ) -> PredictionEvaluationResult:
        """Score every prediction in *cycles* as of *window_end*.

        Each prediction is classified as ``correct``/``wrong``/
        ``undetermined``, persisted to the store, and aggregated into
        the returned :class:`PredictionEvaluationResult`.

        Raises:
            ReflectionError: When the history source or the resolver
                fails. Persistence failures are also surfaced this way:
                a partially-evaluated cycle must not be left half-stored.
        """
        evaluated_at = self._clock()
        sem = asyncio.Semaphore(self._concurrency)

        async def _bounded(cycle: ReasoningOutput, prediction: Prediction) -> PredictionEvaluation:
            async with sem:
                return await self._evaluate_one(
                    cycle=cycle,
                    prediction=prediction,
                    window_end=window_end,
                    evaluated_at=evaluated_at,
                )

        tasks = [
            _bounded(cycle, prediction) for cycle in cycles for prediction in cycle.predictions
        ]
        evaluations: list[PredictionEvaluation] = (
            list(await asyncio.gather(*tasks)) if tasks else []
        )

        correct = sum(1 for ev in evaluations if ev.verdict == "correct")
        wrong = sum(1 for ev in evaluations if ev.verdict == "wrong")
        undetermined = sum(1 for ev in evaluations if ev.verdict == "undetermined")

        if evaluations:
            try:
                await self._store.store(evaluations)
            # Adapter boundary: store backends raise implementation-specific
            # types (DB driver errors, OS errors); we re-raise with cause.
            except Exception as exc:
                raise ReflectionError(
                    "failed to persist prediction evaluations",
                ) from exc

        cycle_ids = [c.cycle_id for c in cycles]
        log.info(
            "reflection.predictions.evaluated",
            evaluated=len(evaluations),
            correct=correct,
            wrong=wrong,
            undetermined=undetermined,
            window_end=window_end.isoformat(),
            cycle_id_first=cycle_ids[0] if cycle_ids else None,
            cycle_id_last=cycle_ids[-1] if cycle_ids else None,
            cycle_count=len(cycle_ids),
        )

        return PredictionEvaluationResult(
            evaluated=len(evaluations),
            correct=correct,
            wrong=wrong,
            undetermined=undetermined,
        )

    async def _evaluate_one(
        self,
        *,
        cycle: ReasoningOutput,
        prediction: Prediction,
        window_end: datetime,
        evaluated_at: datetime,
    ) -> PredictionEvaluation:
        """Evaluate a single *prediction*.

        The horizon is computed from the cycle's ``timestamp`` (when the
        prediction was made), not from ``evaluated_at``: the prediction
        commits to a window starting at its own birth.
        """
        horizon_end = cycle.timestamp + timedelta(hours=prediction.horizon_hours)

        if horizon_end > window_end:
            return PredictionEvaluation(
                cycle_id=cycle.cycle_id,
                prediction_id=prediction.id,
                hypothesis=prediction.hypothesis,
                falsifiable_by=prediction.falsifiable_by,
                prediction_timestamp=cycle.timestamp,
                horizon_end=horizon_end,
                confidence=prediction.confidence,
                verdict="undetermined",
                rationale="horizon not yet reached",
                evaluated_at=evaluated_at,
            )

        try:
            evidence = await self._history.events_in_window(
                after=cycle.timestamp,
                before=horizon_end,
                limit=self._evidence_limit,
            )
        # Adapter boundary: history backends raise implementation-specific
        # types (DB driver errors, network errors); we re-raise with cause.
        except Exception as exc:
            raise ReflectionError(
                f"failed to load evidence for prediction {prediction.id}",
            ) from exc

        if len(evidence) >= self._evidence_limit:
            log.warning(
                "reflection.predictions.evidence_truncated",
                cycle_id=cycle.cycle_id,
                prediction_id=prediction.id,
                horizon_end=horizon_end.isoformat(),
                evidence_limit=self._evidence_limit,
            )

        try:
            verdict, rationale = await self._resolver.resolve(prediction, evidence)
        # Adapter boundary: resolver implementations may raise LLM provider
        # errors, timeout errors, etc.; we re-raise with cause.
        except Exception as exc:
            raise ReflectionError(
                f"resolver failed on prediction {prediction.id}",
            ) from exc

        return PredictionEvaluation(
            cycle_id=cycle.cycle_id,
            prediction_id=prediction.id,
            hypothesis=prediction.hypothesis,
            falsifiable_by=prediction.falsifiable_by,
            prediction_timestamp=cycle.timestamp,
            horizon_end=horizon_end,
            confidence=prediction.confidence,
            verdict=verdict,
            rationale=rationale[:1000],
            evaluated_at=evaluated_at,
        )
