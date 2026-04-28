"""Tests for the Markdown reflection report producer (Task 4.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coremind.action.schemas import Action
from coremind.intention.schemas import Intent, InternalQuestion
from coremind.memory.procedural import Rule
from coremind.reasoning.schemas import (
    Anomaly,
    Pattern,
    ReasoningOutput,
    TokenUsage,
)
from coremind.reflection.loop import ReportProducer
from coremind.reflection.report import MarkdownReportProducer
from coremind.reflection.rule_learner import (
    InMemoryRuleProposalStore,
    RuleProposal,
)
from coremind.reflection.schemas import (
    CalibrationResult,
    FeedbackEvaluationResult,
    PredictionEvaluationResult,
    RuleLearningResult,
)
from coremind.world.model import EntityRef

_WINDOW_END = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
_WINDOW_START = _WINDOW_END - timedelta(days=7)


def _empty_results() -> tuple[
    PredictionEvaluationResult,
    FeedbackEvaluationResult,
    CalibrationResult,
    RuleLearningResult,
]:
    return (
        PredictionEvaluationResult(evaluated=0, correct=0, wrong=0, undetermined=0),
        FeedbackEvaluationResult(evaluated=0, approved=0, rejected=0, reversed=0, dismissed=0),
        CalibrationResult(brier_score=None, sample_count=0),
        RuleLearningResult(),
    )


def _cycle(cycle_id: str, model: str = "anthropic/claude-opus-4-7") -> ReasoningOutput:
    return ReasoningOutput(
        cycle_id=cycle_id,
        timestamp=_WINDOW_END - timedelta(days=2),
        model_used=model,
        patterns=[],
        anomalies=[],
        predictions=[],
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _intent(intent_id: str, *, category: str = "ask", status: str = "done") -> Intent:
    return Intent(
        id=intent_id,
        created_at=_WINDOW_END - timedelta(days=1),
        question=InternalQuestion(id=f"q-{intent_id}", text=f"question {intent_id}?"),
        proposed_action=None,
        salience=0.5,
        confidence=0.5,
        category=category,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
    )


def _action(action_id: str = "a1") -> Action:
    return Action(
        id=action_id,
        intent_id="i1",
        timestamp=_WINDOW_END - timedelta(hours=12),
        category="safe",
        operation="plugin.test.noop",
        parameters={},
        action_class="test",
        expected_outcome="ok",
        confidence=0.9,
        signature="sig",
    )


def _promote_proposal(proposal_id: str = "promote-test-noop") -> RuleProposal:
    rule = Rule(
        id="rule-test-noop",
        created_at=_WINDOW_END,
        description="codify plugin.test.noop",
        trigger={"conditions": [], "logic": "all"},
        action={"operation": "plugin.test.noop"},
        confidence=0.0,
        source="reflection",
    )
    return RuleProposal(
        id=proposal_id,
        kind="promote",
        description="Operation 'plugin.test.noop' succeeded 4/4 times",
        proposed_rule=rule,
        observation_count=4,
        success_rate=1.0,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        created_at=_WINDOW_END,
    )


def _deprecate_proposal(proposal_id: str = "deprecate-rule-x") -> RuleProposal:
    return RuleProposal(
        id=proposal_id,
        kind="deprecate",
        description="Rule 'rule-x' success rate fell to 10% after 10 applications",
        target_rule_id="rule-x",
        observation_count=10,
        success_rate=0.1,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        created_at=_WINDOW_END,
    )


@pytest.mark.asyncio
async def test_empty_window_renders_with_explicit_zero_lines() -> None:
    pred, fb, cal, rules = _empty_results()
    producer = MarkdownReportProducer()

    md = await producer.produce(
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        cycles=[],
        intents=[],
        actions=[],
        predictions=pred,
        feedback=fb,
        calibration=cal,
        rules=rules,
    )

    assert md.startswith("# CoreMind — Weekly Reflection\n")
    assert "Week of 2026-04-13 → 2026-04-20" in md
    assert "0 reasoning cycles executed this week" in md
    assert "0 intents generated this week" in md
    assert "0 actions dispatched this week" in md
    assert "No predictions evaluated this week" in md
    assert "Brier score: not yet available" in md
    assert "No new patterns or anomalies surfaced this week" in md
    assert "No new rule proposals this week" in md
    assert "No rules flagged for deprecation this week" in md
    assert "No outstanding questions" in md
    assert md.endswith("\n")


@pytest.mark.asyncio
async def test_full_report_aggregates_inputs() -> None:
    cycles = [
        _cycle("c1"),
        _cycle("c2"),
        _cycle("c3", model="openai/gpt-4o"),
    ]
    cycles[0] = cycles[0].model_copy(
        update={
            "patterns": [
                Pattern(
                    id="pat1",
                    description="Morning wake at 7:30 on weekdays",
                    entities_involved=[EntityRef(type="person", id="user")],
                    confidence=0.85,
                )
            ],
            "anomalies": [
                Anomaly(
                    id="anom1",
                    description="Bedroom humidity dropped to 32%",
                    entity=EntityRef(type="sensor", id="bedroom-humidity"),
                    severity="medium",
                    baseline_description="usually 40-50%",
                )
            ],
        }
    )
    intents = [
        _intent("i1", category="safe", status="done"),
        _intent("i2", category="suggest", status="done"),
        _intent("i3", category="ask", status="pending_approval"),
        _intent("i4", category="ask", status="done"),
    ]
    actions = [_action("a1"), _action("a2")]
    predictions = PredictionEvaluationResult(evaluated=10, correct=7, wrong=2, undetermined=1)
    feedback = FeedbackEvaluationResult(
        evaluated=2, approved=2, rejected=0, reversed=1, dismissed=0
    )
    calibration = CalibrationResult(brier_score=0.142, sample_count=42)
    rules = RuleLearningResult(
        proposed_rule_ids=["promote-test-noop"],
        deprecated_rule_ids=["deprecate-rule-x"],
    )

    store = InMemoryRuleProposalStore()
    await store.store([_promote_proposal(), _deprecate_proposal()])
    producer = MarkdownReportProducer(proposal_store=store)

    md = await producer.produce(
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        cycles=cycles,
        intents=intents,
        actions=actions,
        predictions=predictions,
        feedback=feedback,
        calibration=calibration,
        rules=rules,
    )

    # Highlights
    assert "3 reasoning cycles executed" in md
    assert "anthropic/claude-opus-4-7: 2" in md
    assert "openai/gpt-4o: 1" in md
    assert "4 intents generated, 2 eligible for autonomous execution, 2 required approval" in md
    assert "2 actions dispatched, 1 reversed by the user" in md
    # Predictions + calibration
    assert "10 predictions evaluated" in md
    assert "7 correct, 2 wrong, 1 undetermined" in md
    assert "Brier score: 0.142" in md
    assert "(over 42 samples)" in md
    # Patterns / anomalies
    assert "## New patterns and anomalies" in md
    assert "Morning wake at 7:30 on weekdays (confidence 85%)" in md
    assert "Anomaly (medium): Bedroom humidity dropped to 32%" in md
    # Rule proposals enriched from store
    assert "1. Operation 'plugin.test.noop' succeeded 4/4 times" in md
    assert "1. Rule 'rule-x' success rate fell to 10% after 10 applications" in md
    # Pending questions: every ask-class intent (any status) and any
    # non-ask intent gated on approval. Internal-only states on safe/
    # suggest intents stay out of the user-facing section.
    assert "- question i3?" in md
    assert "- question i4?" in md
    assert "- question i1?" not in md
    assert "- question i2?" not in md


@pytest.mark.asyncio
async def test_no_proposal_store_falls_back_to_ids() -> None:
    pred, fb, cal, _ = _empty_results()
    rules = RuleLearningResult(
        proposed_rule_ids=["promote-foo-bar"],
        deprecated_rule_ids=["deprecate-rule-y"],
    )
    producer = MarkdownReportProducer()

    md = await producer.produce(
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        cycles=[],
        intents=[],
        actions=[],
        predictions=pred,
        feedback=fb,
        calibration=cal,
        rules=rules,
    )

    assert "1. promote-foo-bar (details unavailable)" in md
    assert "1. deprecate-rule-y (details unavailable)" in md


@pytest.mark.asyncio
async def test_proposal_store_failure_degrades_gracefully() -> None:
    class _BrokenStore:
        async def store(self, proposals):  # type: ignore[no-untyped-def]
            raise RuntimeError("never called")

        async def list_pending(self) -> list[RuleProposal]:
            raise RuntimeError("backend down")

    pred, fb, cal, _ = _empty_results()
    rules = RuleLearningResult(
        proposed_rule_ids=["promote-test-noop"],
        deprecated_rule_ids=[],
    )
    producer = MarkdownReportProducer(proposal_store=_BrokenStore())  # type: ignore[arg-type]

    md = await producer.produce(
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        cycles=[],
        intents=[],
        actions=[],
        predictions=pred,
        feedback=fb,
        calibration=cal,
        rules=rules,
    )

    assert "1. promote-test-noop (details unavailable)" in md


@pytest.mark.asyncio
async def test_patterns_dedupe_across_cycles() -> None:
    pat = Pattern(
        id="pat1",
        description="repeating pattern",
        entities_involved=[EntityRef(type="person", id="user")],
        confidence=0.5,
    )
    c1 = _cycle("c1").model_copy(update={"patterns": [pat]})
    c2 = _cycle("c2").model_copy(update={"patterns": [pat]})

    pred, fb, cal, rules = _empty_results()
    producer = MarkdownReportProducer()
    md = await producer.produce(
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        cycles=[c1, c2],
        intents=[],
        actions=[],
        predictions=pred,
        feedback=fb,
        calibration=cal,
        rules=rules,
    )

    assert md.count("repeating pattern") == 1


@pytest.mark.asyncio
async def test_satisfies_report_producer_protocol() -> None:
    producer: ReportProducer = MarkdownReportProducer()
    pred, fb, cal, rules = _empty_results()
    md = await producer.produce(
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        cycles=[],
        intents=[],
        actions=[],
        predictions=pred,
        feedback=fb,
        calibration=cal,
        rules=rules,
    )
    assert "CoreMind" in md
