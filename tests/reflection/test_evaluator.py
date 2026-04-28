"""Tests for the L7 prediction evaluator (Task 4.2)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
import structlog

from coremind.errors import ReflectionError
from coremind.reasoning.schemas import Prediction, ReasoningOutput, TokenUsage
from coremind.reflection.evaluator import (
    ConditionResolver,
    InMemoryPredictionEvaluationStore,
    PredictionEvaluation,
    PredictionEvaluatorImpl,
    Verdict,
)
from coremind.world.model import EntityRef, WorldEventRecord

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
_CYCLE_TS = _NOW - timedelta(days=2)


def _make_prediction(
    pid: str = "p1",
    *,
    horizon_hours: int = 24,
    confidence: float = 0.7,
) -> Prediction:
    return Prediction(
        id=pid,
        hypothesis=f"hypothesis {pid}",
        horizon_hours=horizon_hours,
        confidence=confidence,
        falsifiable_by=f"observation refuting {pid}",
    )


def _make_cycle(
    cycle_id: str = "c1",
    *,
    timestamp: datetime = _CYCLE_TS,
    predictions: list[Prediction] | None = None,
) -> ReasoningOutput:
    return ReasoningOutput(
        cycle_id=cycle_id,
        timestamp=timestamp,
        model_used="test/model",
        predictions=predictions if predictions is not None else [_make_prediction()],
        token_usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _make_event(event_id: str, ts: datetime) -> WorldEventRecord:
    return WorldEventRecord(
        id=event_id,
        timestamp=ts,
        source="test",
        source_version="0.0.0",
        signature=None,
        entity=EntityRef(type="user", id="alice"),
        attribute="state",
        value="awake",
        confidence=1.0,
    )


class _FakeHistory:
    def __init__(self, events: list[WorldEventRecord] | None = None) -> None:
        self._events = events or []
        self.calls: list[tuple[datetime, datetime, int]] = []

    async def events_in_window(
        self,
        after: datetime,
        before: datetime,
        limit: int = 1000,
    ) -> list[WorldEventRecord]:
        self.calls.append((after, before, limit))
        return [e for e in self._events if after < e.timestamp <= before][:limit]


class _ScriptedResolver:
    def __init__(self, script: dict[str, tuple[Verdict, str]]) -> None:
        self._script = script
        self.received: list[tuple[Prediction, Sequence[WorldEventRecord]]] = []

    async def resolve(
        self,
        prediction: Prediction,
        evidence: Sequence[WorldEventRecord],
    ) -> tuple[Verdict, str]:
        self.received.append((prediction, evidence))
        return self._script[prediction.id]


def _build(
    *,
    history: _FakeHistory | None = None,
    resolver: ConditionResolver | None = None,
    store: InMemoryPredictionEvaluationStore | None = None,
    clock_value: datetime = _NOW,
) -> tuple[
    PredictionEvaluatorImpl,
    _FakeHistory,
    InMemoryPredictionEvaluationStore,
]:
    history = history or _FakeHistory()
    store = store or InMemoryPredictionEvaluationStore()
    resolver = resolver or _ScriptedResolver({})
    evaluator = PredictionEvaluatorImpl(
        history,
        resolver,
        store,
        clock=lambda: clock_value,
    )
    return evaluator, history, store


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


async def test_empty_input_returns_zero_counts_and_skips_store() -> None:
    evaluator, history, store = _build()

    result = await evaluator.evaluate([], window_end=_NOW)

    assert result.evaluated == 0
    assert result.correct == 0
    assert result.wrong == 0
    assert result.undetermined == 0
    assert history.calls == []
    assert await store.list_since(_NOW - timedelta(days=30)) == []


async def test_horizon_not_yet_elapsed_marks_undetermined_without_resolver() -> None:
    # Cycle timestamp + 24h = NOW + 22h, far past window_end=_NOW.
    cycle = _make_cycle(
        timestamp=_NOW - timedelta(hours=2),
        predictions=[_make_prediction("p1", horizon_hours=24)],
    )
    resolver = _ScriptedResolver({})  # would KeyError if invoked
    evaluator, history, store = _build(resolver=resolver)

    result = await evaluator.evaluate([cycle], window_end=_NOW)

    assert result.evaluated == 1
    assert result.undetermined == 1
    assert result.correct == 0 and result.wrong == 0
    assert history.calls == []  # no evidence query when undetermined
    assert resolver.received == []
    rows = await store.list_since(_NOW - timedelta(days=30))
    assert len(rows) == 1
    assert rows[0].verdict == "undetermined"
    assert rows[0].rationale == "horizon not yet reached"


async def test_correct_and_wrong_verdicts_aggregate() -> None:
    cycle = _make_cycle(
        predictions=[
            _make_prediction("p1", horizon_hours=12, confidence=0.8),
            _make_prediction("p2", horizon_hours=12, confidence=0.4),
        ]
    )
    resolver = _ScriptedResolver(
        {
            "p1": ("correct", "observed wakeup"),
            "p2": ("wrong", "no humidity drop"),
        }
    )
    evaluator, _history, store = _build(resolver=resolver)

    result = await evaluator.evaluate([cycle], window_end=_NOW)

    assert result.evaluated == 2
    assert result.correct == 1
    assert result.wrong == 1
    assert result.undetermined == 0
    rows = sorted(
        await store.list_since(_NOW - timedelta(days=30)),
        key=lambda r: r.prediction_id,
    )
    assert [r.verdict for r in rows] == ["correct", "wrong"]
    assert [r.confidence for r in rows] == [0.8, 0.4]


async def test_evidence_window_passes_prediction_timestamp_to_horizon_end() -> None:
    cycle = _make_cycle(
        timestamp=_CYCLE_TS,
        predictions=[_make_prediction("p1", horizon_hours=6)],
    )
    history = _FakeHistory(
        [
            _make_event("e-before", _CYCLE_TS - timedelta(minutes=1)),
            _make_event("e-inside", _CYCLE_TS + timedelta(hours=2)),
            _make_event("e-after", _CYCLE_TS + timedelta(hours=10)),
        ]
    )
    resolver = _ScriptedResolver({"p1": ("correct", "ok")})
    evaluator, history, _ = _build(history=history, resolver=resolver)

    await evaluator.evaluate([cycle], window_end=_NOW)

    assert history.calls == [(_CYCLE_TS, _CYCLE_TS + timedelta(hours=6), 1000)]
    # Resolver only saw evidence within the horizon window.
    seen = resolver.received[0][1]
    assert [e.id for e in seen] == ["e-inside"]


async def test_resolver_can_return_undetermined() -> None:
    cycle = _make_cycle(predictions=[_make_prediction("p1", horizon_hours=6)])
    resolver = _ScriptedResolver({"p1": ("undetermined", "no signal observed")})
    evaluator, _, store = _build(resolver=resolver)

    result = await evaluator.evaluate([cycle], window_end=_NOW)

    assert result.undetermined == 1
    rows = await store.list_since(_NOW - timedelta(days=30))
    assert rows[0].rationale == "no signal observed"


async def test_history_failure_is_wrapped_as_reflection_error() -> None:
    class _BoomHistory:
        async def events_in_window(
            self,
            after: datetime,
            before: datetime,
            limit: int = 1000,
        ) -> list[WorldEventRecord]:
            raise RuntimeError("db gone")

    cycle = _make_cycle(predictions=[_make_prediction("p1", horizon_hours=6)])
    evaluator = PredictionEvaluatorImpl(
        _BoomHistory(),
        _ScriptedResolver({"p1": ("correct", "ok")}),
        InMemoryPredictionEvaluationStore(),
        clock=lambda: _NOW,
    )

    with pytest.raises(ReflectionError):
        await evaluator.evaluate([cycle], window_end=_NOW)


async def test_resolver_failure_is_wrapped_as_reflection_error() -> None:
    class _BoomResolver:
        async def resolve(
            self,
            prediction: Prediction,
            evidence: Sequence[WorldEventRecord],
        ) -> tuple[Verdict, str]:
            raise RuntimeError("llm down")

    cycle = _make_cycle(predictions=[_make_prediction("p1", horizon_hours=6)])
    evaluator = PredictionEvaluatorImpl(
        _FakeHistory(),
        _BoomResolver(),
        InMemoryPredictionEvaluationStore(),
        clock=lambda: _NOW,
    )

    with pytest.raises(ReflectionError):
        await evaluator.evaluate([cycle], window_end=_NOW)


async def test_store_failure_is_wrapped_as_reflection_error() -> None:
    class _BoomStore:
        async def store(
            self,
            evaluations: Sequence[PredictionEvaluation],
        ) -> None:
            raise RuntimeError("disk full")

        async def list_since(
            self,
            since: datetime,
            until: datetime | None = None,
        ) -> list[PredictionEvaluation]:
            return []

    cycle = _make_cycle(predictions=[_make_prediction("p1", horizon_hours=6)])
    evaluator = PredictionEvaluatorImpl(
        _FakeHistory(),
        _ScriptedResolver({"p1": ("correct", "ok")}),
        _BoomStore(),
        clock=lambda: _NOW,
    )

    with pytest.raises(ReflectionError):
        await evaluator.evaluate([cycle], window_end=_NOW)


async def test_in_memory_store_is_idempotent_on_cycle_prediction_key() -> None:
    cycle = _make_cycle(predictions=[_make_prediction("p1", horizon_hours=6)])
    resolver = _ScriptedResolver({"p1": ("correct", "first")})
    store = InMemoryPredictionEvaluationStore()
    evaluator = PredictionEvaluatorImpl(
        _FakeHistory(),
        resolver,
        store,
        clock=lambda: _NOW,
    )
    await evaluator.evaluate([cycle], window_end=_NOW)

    # Re-evaluate with a new verdict for the same prediction id.
    resolver._script["p1"] = ("wrong", "second")
    await evaluator.evaluate([cycle], window_end=_NOW)

    rows = await store.list_since(_NOW - timedelta(days=30))
    assert len(rows) == 1
    assert rows[0].verdict == "wrong"
    assert rows[0].rationale == "second"


async def test_long_rationale_is_truncated_to_thousand_chars() -> None:
    cycle = _make_cycle(predictions=[_make_prediction("p1", horizon_hours=6)])
    long_text = "x" * 2500
    resolver = _ScriptedResolver({"p1": ("correct", long_text)})
    evaluator, _, store = _build(resolver=resolver)

    await evaluator.evaluate([cycle], window_end=_NOW)

    rows = await store.list_since(_NOW - timedelta(days=30))
    assert len(rows[0].rationale) == 1000


async def test_list_since_window_filters_evaluated_at() -> None:
    store = InMemoryPredictionEvaluationStore()
    early = PredictionEvaluation(
        cycle_id="c1",
        prediction_id="p1",
        hypothesis="h",
        falsifiable_by="f",
        prediction_timestamp=_NOW - timedelta(days=10),
        horizon_end=_NOW - timedelta(days=9),
        confidence=0.5,
        verdict="correct",
        rationale="",
        evaluated_at=_NOW - timedelta(days=8),
    )
    late = early.model_copy(
        update={"prediction_id": "p2", "evaluated_at": _NOW - timedelta(days=1)}
    )
    await store.store([early, late])

    only_recent = await store.list_since(_NOW - timedelta(days=3))
    assert {ev.prediction_id for ev in only_recent} == {"p2"}

    bounded = await store.list_since(
        _NOW - timedelta(days=15),
        until=_NOW - timedelta(days=5),
    )
    assert {ev.prediction_id for ev in bounded} == {"p1"}


async def test_list_since_returns_rows_sorted_by_evaluated_at() -> None:
    store = InMemoryPredictionEvaluationStore()
    base = PredictionEvaluation(
        cycle_id="c1",
        prediction_id="p1",
        hypothesis="h",
        falsifiable_by="f",
        prediction_timestamp=_NOW - timedelta(days=10),
        horizon_end=_NOW - timedelta(days=9),
        confidence=0.5,
        verdict="correct",
        rationale="",
        evaluated_at=_NOW - timedelta(days=2),
    )
    earlier = base.model_copy(
        update={"prediction_id": "p-early", "evaluated_at": _NOW - timedelta(days=5)}
    )
    latest = base.model_copy(
        update={"prediction_id": "p-late", "evaluated_at": _NOW - timedelta(hours=1)}
    )
    # Insert out of order to make the test meaningful.
    await store.store([base, latest, earlier])

    rows = await store.list_since(_NOW - timedelta(days=30))
    assert [r.prediction_id for r in rows] == ["p-early", "p1", "p-late"]


async def test_evidence_truncation_emits_warning_log() -> None:
    cycle = _make_cycle(predictions=[_make_prediction("p1", horizon_hours=6)])
    # Three events inside the horizon, with evidence_limit=2 the cap is hit.
    history = _FakeHistory(
        [
            _make_event("e1", _CYCLE_TS + timedelta(hours=1)),
            _make_event("e2", _CYCLE_TS + timedelta(hours=2)),
            _make_event("e3", _CYCLE_TS + timedelta(hours=3)),
        ]
    )
    resolver = _ScriptedResolver({"p1": ("correct", "ok")})
    evaluator = PredictionEvaluatorImpl(
        history,
        resolver,
        InMemoryPredictionEvaluationStore(),
        evidence_limit=2,
        clock=lambda: _NOW,
    )

    with structlog.testing.capture_logs() as logs:
        await evaluator.evaluate([cycle], window_end=_NOW)

    truncation_logs = [e for e in logs if e["event"] == "reflection.predictions.evidence_truncated"]
    assert len(truncation_logs) == 1
    entry = truncation_logs[0]
    assert entry["log_level"] == "warning"
    assert entry["cycle_id"] == "c1"
    assert entry["prediction_id"] == "p1"
    assert entry["evidence_limit"] == 2


async def test_predictions_resolved_concurrently_under_semaphore() -> None:
    cycle = _make_cycle(predictions=[_make_prediction(f"p{i}", horizon_hours=6) for i in range(4)])

    inflight = 0
    peak = 0
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingResolver:
        async def resolve(
            self,
            prediction: Prediction,
            evidence: Sequence[WorldEventRecord],
        ) -> tuple[Verdict, str]:
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            started.set()
            await release.wait()
            inflight -= 1
            return ("correct", f"ok-{prediction.id}")

    evaluator = PredictionEvaluatorImpl(
        _FakeHistory(),
        _BlockingResolver(),
        InMemoryPredictionEvaluationStore(),
        max_concurrent_resolvers=2,
        clock=lambda: _NOW,
    )

    task = asyncio.create_task(evaluator.evaluate([cycle], window_end=_NOW))
    await started.wait()
    # Yield the loop a few times so all schedulable resolvers can enter.
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    result = await task

    assert result.evaluated == 4
    assert result.correct == 4
    # At most 2 resolvers were ever in-flight, but more than 1 ran in parallel.
    assert peak == 2


async def test_concurrency_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="max_concurrent_resolvers"):
        PredictionEvaluatorImpl(
            _FakeHistory(),
            _ScriptedResolver({}),
            InMemoryPredictionEvaluationStore(),
            max_concurrent_resolvers=0,
            clock=lambda: _NOW,
        )
