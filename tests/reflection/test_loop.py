"""Tests for the L7 ReflectionLoop scheduler and orchestration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from coremind.action.schemas import Action
from coremind.errors import ReflectionError
from coremind.intention.schemas import Intent, InternalQuestion
from coremind.reasoning.schemas import (
    Prediction,
    ReasoningOutput,
    TokenUsage,
)
from coremind.reflection.loop import (
    ReflectionLoop,
    ReflectionLoopConfig,
)
from coremind.reflection.schemas import (
    CalibrationResult,
    FeedbackEvaluationResult,
    PredictionEvaluationResult,
    ReflectionReport,
    RuleLearningResult,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


_FIXED_NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


def _make_cycle(cycle_id: str = "c1") -> ReasoningOutput:
    return ReasoningOutput(
        cycle_id=cycle_id,
        timestamp=_FIXED_NOW - timedelta(days=2),
        model_used="test/model",
        patterns=[],
        anomalies=[],
        predictions=[
            Prediction(
                id="p1",
                hypothesis="user wakes up at 7:30",
                horizon_hours=24,
                confidence=0.7,
                falsifiable_by="no wake event before 09:00",
            )
        ],
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _make_intent(intent_id: str = "i1") -> Intent:
    return Intent(
        id=intent_id,
        created_at=_FIXED_NOW - timedelta(days=1),
        question=InternalQuestion(id="q1", text="should I do x?"),
        proposed_action=None,
        salience=0.5,
        confidence=0.5,
        category="ask",
        status="done",
    )


def _make_action(action_id: str = "a1") -> Action:
    return Action(
        id=action_id,
        intent_id="i1",
        timestamp=_FIXED_NOW - timedelta(hours=12),
        category="safe",
        operation="plugin.test.noop",
        parameters={},
        action_class="test",
        expected_outcome="ok",
        confidence=0.9,
        signature="sig",
    )


class _FakeCycleSource:
    def __init__(self, cycles: list[ReasoningOutput]) -> None:
        self._cycles = cycles
        self.calls: list[tuple[datetime, datetime]] = []

    async def list_cycles(self, *, since: datetime, until: datetime) -> list[ReasoningOutput]:
        self.calls.append((since, until))
        return list(self._cycles)


class _FakeIntentSource:
    def __init__(self, intents: list[Intent]) -> None:
        self._intents = intents

    async def list_intents(self, *, since: datetime, until: datetime) -> list[Intent]:
        return list(self._intents)


class _FakeActionFeed:
    def __init__(self, actions: list[Action]) -> None:
        self._actions = actions

    async def list_actions(self, *, since: datetime, until: datetime) -> list[Action]:
        return list(self._actions)


class _FakePredictionEvaluator:
    def __init__(self) -> None:
        self.received: list[ReasoningOutput] = []

    async def evaluate(
        self,
        cycles: list[ReasoningOutput],
        *,
        window_end: datetime,
    ) -> PredictionEvaluationResult:
        self.received = cycles
        return PredictionEvaluationResult(evaluated=len(cycles), correct=1, wrong=0, undetermined=0)


class _FakeFeedbackEvaluator:
    async def evaluate(
        self,
        actions: list[Action],
        intents: list[Intent],
    ) -> FeedbackEvaluationResult:
        return FeedbackEvaluationResult(
            evaluated=len(actions),
            approved=len(actions),
            rejected=0,
            reversed=0,
            dismissed=0,
        )


class _FakeCalibrationUpdater:
    async def update(
        self,
        cycles: list[ReasoningOutput],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> CalibrationResult:
        del cycles, window_start, window_end
        return CalibrationResult(brier_score=0.12, sample_count=0)


class _FakeRuleLearner:
    async def learn(
        self,
        cycles: list[ReasoningOutput],
        intents: list[Intent],
        actions: list[Action],
    ) -> RuleLearningResult:
        return RuleLearningResult(
            proposed_rule_ids=["r-new-1"],
            deprecated_rule_ids=[],
        )


class _FakeReportProducer:
    def __init__(self) -> None:
        self.calls = 0

    async def produce(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        cycles: list[ReasoningOutput],
        intents: list[Intent],
        actions: list[Action],
        predictions: PredictionEvaluationResult,
        feedback: FeedbackEvaluationResult,
        calibration: CalibrationResult,
        rules: RuleLearningResult,
    ) -> str:
        self.calls += 1
        return f"# Reflection report\nwindow: {window_start} → {window_end}"


class _RecordingNotifier:
    def __init__(self) -> None:
        self.delivered: list[ReflectionReport] = []
        self.delivery_event = asyncio.Event()

    async def deliver(self, report: ReflectionReport) -> None:
        self.delivered.append(report)
        self.delivery_event.set()


def _build_loop(
    *,
    cycles: list[ReasoningOutput] | None = None,
    intents: list[Intent] | None = None,
    actions: list[Action] | None = None,
    notifier: _RecordingNotifier | None = None,
    config: ReflectionLoopConfig | None = None,
) -> tuple[
    ReflectionLoop,
    _FakeCycleSource,
    _FakePredictionEvaluator,
    _FakeReportProducer,
]:
    cycle_src = _FakeCycleSource(cycles or [_make_cycle()])
    intent_src = _FakeIntentSource(intents or [_make_intent()])
    action_src = _FakeActionFeed(actions or [_make_action()])
    pred = _FakePredictionEvaluator()
    fb = _FakeFeedbackEvaluator()
    cal = _FakeCalibrationUpdater()
    rl = _FakeRuleLearner()
    rp = _FakeReportProducer()
    loop = ReflectionLoop(
        cycle_src,
        intent_src,
        action_src,
        pred,
        fb,
        cal,
        rl,
        rp,
        notifier=notifier,
        config=config,
        clock=lambda: _FIXED_NOW,
    )
    return loop, cycle_src, pred, rp


# ---------------------------------------------------------------------------
# Behaviour tests
# ---------------------------------------------------------------------------


async def test_run_cycle_aggregates_ports_and_returns_report() -> None:
    notifier = _RecordingNotifier()
    loop, cycle_src, pred, report_producer = _build_loop(notifier=notifier)

    report = await loop.run_cycle()

    assert report.cycles_evaluated == 1
    assert report.intents_evaluated == 1
    assert report.actions_evaluated == 1
    assert report.predictions.evaluated == 1
    assert report.predictions.correct == 1
    assert report.calibration.brier_score == 0.12
    assert report.rules.proposed_rule_ids == ["r-new-1"]
    assert report.markdown.startswith("# Reflection report")
    assert report.window_end == _FIXED_NOW
    assert report.window_start == _FIXED_NOW - timedelta(days=7)
    # Each port was invoked exactly once with the same window.
    assert cycle_src.calls == [(report.window_start, report.window_end)]
    assert pred.received  # received the cycle list
    assert report_producer.calls == 1


async def test_run_cycle_delivers_report_when_notifier_configured() -> None:
    notifier = _RecordingNotifier()
    loop, _, _, _ = _build_loop(notifier=notifier)

    report = await loop.run_cycle()

    assert notifier.delivered == [report]


async def test_run_cycle_skips_delivery_when_notify_disabled() -> None:
    notifier = _RecordingNotifier()
    loop, _, _, _ = _build_loop(
        notifier=notifier,
        config=ReflectionLoopConfig(notify_on_cycle=False),
    )

    await loop.run_cycle()

    assert notifier.delivered == []


async def test_run_cycle_swallows_notifier_failure() -> None:
    class _BoomNotifier:
        async def deliver(self, report: ReflectionReport) -> None:
            raise RuntimeError("transport down")

    loop, _, _, _ = _build_loop(notifier=cast(_RecordingNotifier, _BoomNotifier()))

    # Notifier failures must not surface as ReflectionError.
    report = await loop.run_cycle()
    assert isinstance(report, ReflectionReport)


async def test_run_cycle_wraps_collection_failure_as_reflection_error() -> None:
    class _BrokenCycleSource:
        async def list_cycles(self, *, since: datetime, until: datetime) -> list[ReasoningOutput]:
            raise RuntimeError("db gone")

    loop = ReflectionLoop(
        cast(_FakeCycleSource, _BrokenCycleSource()),
        _FakeIntentSource([]),
        _FakeActionFeed([]),
        _FakePredictionEvaluator(),
        _FakeFeedbackEvaluator(),
        _FakeCalibrationUpdater(),
        _FakeRuleLearner(),
        _FakeReportProducer(),
        clock=lambda: _FIXED_NOW,
    )

    with pytest.raises(ReflectionError):
        await loop.run_cycle()


async def test_run_cycle_wraps_evaluator_failure_as_reflection_error() -> None:
    class _BrokenEvaluator:
        async def evaluate(
            self,
            cycles: list[ReasoningOutput],
            *,
            window_end: datetime,
        ) -> PredictionEvaluationResult:
            raise RuntimeError("evaluator boom")

    loop = ReflectionLoop(
        _FakeCycleSource([_make_cycle()]),
        _FakeIntentSource([]),
        _FakeActionFeed([]),
        cast(_FakePredictionEvaluator, _BrokenEvaluator()),
        _FakeFeedbackEvaluator(),
        _FakeCalibrationUpdater(),
        _FakeRuleLearner(),
        _FakeReportProducer(),
        clock=lambda: _FIXED_NOW,
    )

    with pytest.raises(ReflectionError):
        await loop.run_cycle()


async def test_scheduler_runs_cycle_and_stops_cleanly() -> None:
    notifier = _RecordingNotifier()
    loop, _, _, _ = _build_loop(
        notifier=notifier,
        config=ReflectionLoopConfig(interval_seconds=86400, window_days=1),
    )

    loop.start()
    try:
        # Wait deterministically for the first scheduled cycle to land.
        await asyncio.wait_for(notifier.delivery_event.wait(), timeout=2.0)
    finally:
        await loop.stop()

    assert len(notifier.delivered) >= 1


async def test_scheduler_stop_cancels_in_flight_cycle() -> None:
    """``stop()`` must abort a hung cycle so daemon shutdown is bounded."""

    cycle_started = asyncio.Event()

    class _HangingCycleSource:
        async def list_cycles(
            self,
            *,
            since: datetime,
            until: datetime,
        ) -> list[ReasoningOutput]:
            cycle_started.set()
            # Stall forever; only cancellation should unblock us.
            await asyncio.Event().wait()
            return []  # pragma: no cover

    loop = ReflectionLoop(
        cast(_FakeCycleSource, _HangingCycleSource()),
        _FakeIntentSource([]),
        _FakeActionFeed([]),
        _FakePredictionEvaluator(),
        _FakeFeedbackEvaluator(),
        _FakeCalibrationUpdater(),
        _FakeRuleLearner(),
        _FakeReportProducer(),
        config=ReflectionLoopConfig(interval_seconds=86400, window_days=1),
        clock=lambda: _FIXED_NOW,
    )

    loop.start()
    await asyncio.wait_for(cycle_started.wait(), timeout=2.0)
    # Without cancellation in stop(), this would block forever.
    await asyncio.wait_for(loop.stop(), timeout=2.0)


async def test_scheduler_does_not_crash_on_cycle_failure() -> None:
    """A failing cycle is logged but the loop's state stays usable."""

    failure_seen = asyncio.Event()

    class _FlakyEvaluator:
        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(
            self,
            cycles: list[ReasoningOutput],
            *,
            window_end: datetime,
        ) -> PredictionEvaluationResult:
            self.calls += 1
            if self.calls == 1:
                failure_seen.set()
                raise RuntimeError("transient")
            return PredictionEvaluationResult(evaluated=0, correct=0, wrong=0, undetermined=0)

    flaky = _FlakyEvaluator()
    notifier = _RecordingNotifier()
    loop = ReflectionLoop(
        _FakeCycleSource([_make_cycle()]),
        _FakeIntentSource([]),
        _FakeActionFeed([]),
        cast(_FakePredictionEvaluator, flaky),
        _FakeFeedbackEvaluator(),
        _FakeCalibrationUpdater(),
        _FakeRuleLearner(),
        _FakeReportProducer(),
        notifier=notifier,
        config=ReflectionLoopConfig(interval_seconds=86400, window_days=1),
        clock=lambda: _FIXED_NOW,
    )

    loop.start()
    try:
        # The scheduler swallows the first failure; wait deterministically
        # for it to occur before stopping.
        await asyncio.wait_for(failure_seen.wait(), timeout=2.0)
    finally:
        await loop.stop()

    assert flaky.calls == 1
    # Loop state is intact: a manual cycle on the same instance succeeds,
    # confirming the scheduler did not corrupt anything on the way down.
    report = await loop.run_cycle()
    assert report.predictions.evaluated == 0
