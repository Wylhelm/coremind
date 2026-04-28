"""Tests for the L7 rule learner (Task 4.4)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from coremind.action.schemas import Action, ActionResult
from coremind.errors import ReflectionError
from coremind.memory.procedural import Rule
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
)

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


_ActionStatus = Literal[
    "dispatched",
    "ok",
    "noop",
    "transient_failure",
    "permanent_failure",
    "rejected_invalid_signature",
]


def _make_action(
    action_id: str,
    *,
    operation: str = "plugin.test.do",
    action_class: str = "test",
    intent_id: str = "i1",
    timestamp: datetime | None = None,
    status: _ActionStatus = "ok",
    settled: bool = True,
) -> Action:
    ts = timestamp or _NOW - timedelta(hours=1)
    result: ActionResult | None = None
    if settled:
        result = ActionResult(
            action_id=action_id,
            status=status,
            message="",
            output=None,
            completed_at=ts + timedelta(seconds=1),
        )
    return Action(
        id=action_id,
        intent_id=intent_id,
        timestamp=ts,
        category="safe",
        operation=operation,
        parameters={},
        action_class=action_class,
        expected_outcome="",
        confidence=0.9,
        signature="sig",
        result=result,
    )


def _make_rule(
    rule_id: str,
    *,
    operation: str = "plugin.test.do",
    applied_count: int = 0,
    success_rate: float = 1.0,
    confidence: float = 0.9,
) -> Rule:
    return Rule(
        id=rule_id,
        created_at=_NOW - timedelta(days=10),
        description=f"rule {rule_id}",
        trigger={"conditions": [], "logic": "all"},
        action={"operation": operation, "action_class": "test"},
        confidence=confidence,
        applied_count=applied_count,
        success_rate=success_rate,
        source="human",
    )


class _FakeRuleSource:
    def __init__(self, rules: list[Rule] | None = None) -> None:
        self._rules = rules or []
        self.calls = 0

    async def list_active_rules(self) -> list[Rule]:
        self.calls += 1
        return list(self._rules)


def _build(
    *,
    rules: list[Rule] | None = None,
    config: RuleLearnerConfig | None = None,
    ledger: CandidateLedger | None = None,
) -> tuple[
    RuleLearnerImpl,
    _FakeRuleSource,
    CandidateLedger,
    InMemoryRuleProposalStore,
]:
    rule_source = _FakeRuleSource(rules=rules)
    ledger = ledger or InMemoryCandidateLedger()
    store = InMemoryRuleProposalStore()
    learner = RuleLearnerImpl(
        rule_source,
        ledger,
        store,
        config=config,
        clock=lambda: _NOW,
    )
    return learner, rule_source, ledger, store


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_candidate_stats_requires_last_evaluated_at_when_populated() -> None:
    with pytest.raises(ValueError):
        CandidateStats(
            key=CandidateKey(action_class="c", operation="op"),
            evaluation_count=1,
            success_count=1,
            last_evaluated_at=None,
        )


def test_candidate_stats_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError):
        CandidateStats(
            key=CandidateKey(action_class="c", operation="op"),
            evaluation_count=1,
            success_count=1,
            last_evaluated_at=datetime(2026, 1, 1, 0, 0, 0),  # noqa: DTZ001 — testing the validator
        )


def test_promote_proposal_requires_proposed_rule() -> None:
    with pytest.raises(ValueError):
        RuleProposal(
            id="promote-x",
            kind="promote",
            description="d",
            proposed_rule=None,
            target_rule_id=None,
            observation_count=1,
            success_rate=1.0,
            window_start=_NOW,
            window_end=_NOW,
            created_at=_NOW,
        )


def test_deprecate_proposal_requires_target_rule_id() -> None:
    with pytest.raises(ValueError):
        RuleProposal(
            id="deprecate-x",
            kind="deprecate",
            description="d",
            proposed_rule=None,
            target_rule_id=None,
            observation_count=1,
            success_rate=0.1,
            window_start=_NOW,
            window_end=_NOW,
            created_at=_NOW,
        )


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


async def test_empty_inputs_emit_no_proposals() -> None:
    learner, rule_source, _ledger, store = _build()

    result = await learner.learn(cycles=[], intents=[], actions=[])

    assert result.proposed_rule_ids == []
    assert result.deprecated_rule_ids == []
    assert await store.list_pending() == []
    assert rule_source.calls == 1


async def test_promotion_requires_min_observations() -> None:
    learner, _src, _ledger, store = _build(
        config=RuleLearnerConfig(promotion_min_observations=3, promotion_min_success_rate=0.8),
    )
    actions = [_make_action(f"a{i}", status="ok") for i in range(2)]

    result = await learner.learn(cycles=[], intents=[], actions=actions)

    assert result.proposed_rule_ids == []
    assert await store.list_pending() == []


async def test_promotion_requires_min_success_rate() -> None:
    learner, _src, _ledger, store = _build(
        config=RuleLearnerConfig(promotion_min_observations=3, promotion_min_success_rate=0.8),
    )
    actions = [
        _make_action("a1", status="ok"),
        _make_action("a2", status="ok"),
        _make_action("a3", status="permanent_failure"),
    ]

    result = await learner.learn(cycles=[], intents=[], actions=actions)

    assert result.proposed_rule_ids == []
    assert await store.list_pending() == []


async def test_promotion_emits_proposal_when_thresholds_crossed() -> None:
    learner, _src, ledger, store = _build(
        config=RuleLearnerConfig(promotion_min_observations=3, promotion_min_success_rate=0.8),
    )
    actions = [_make_action(f"a{i}", status="ok") for i in range(3)]

    result = await learner.learn(cycles=[], intents=[], actions=actions)

    assert len(result.proposed_rule_ids) == 1
    [pid] = result.proposed_rule_ids
    assert pid == "promote-test-plugin.test.do"

    pending = await store.list_pending()
    assert len(pending) == 1
    proposal = pending[0]
    assert proposal.kind == "promote"
    assert proposal.target_rule_id is None
    assert proposal.proposed_rule is not None
    assert proposal.proposed_rule.source == "reflection"
    assert proposal.proposed_rule.action == {
        "operation": "plugin.test.do",
        "action_class": "test",
    }
    assert proposal.observation_count == 3
    assert proposal.success_rate == pytest.approx(1.0)

    # Ledger must have marked the candidate so the next cycle does not
    # re-emit the proposal.
    stats = await ledger.list_all()
    assert len(stats) == 1
    assert stats[0].proposal_emitted is True


async def test_promotion_idempotent_across_cycles() -> None:
    learner, _src, _ledger, store = _build(
        config=RuleLearnerConfig(promotion_min_observations=3, promotion_min_success_rate=0.8),
    )
    actions = [_make_action(f"a{i}", status="ok") for i in range(3)]
    await learner.learn(cycles=[], intents=[], actions=actions)

    # Second cycle with fresh actions for the same key — must not
    # re-propose.
    more_actions = [_make_action(f"b{i}", status="ok") for i in range(3)]
    result = await learner.learn(cycles=[], intents=[], actions=more_actions)

    assert result.proposed_rule_ids == []
    assert len(await store.list_pending()) == 1


async def test_promotion_accumulates_across_windows() -> None:
    learner, _src, _ledger, store = _build(
        config=RuleLearnerConfig(promotion_min_observations=3, promotion_min_success_rate=0.8),
    )
    # First window: only 2 successes, below threshold.
    await learner.learn(
        cycles=[],
        intents=[],
        actions=[_make_action(f"a{i}", status="ok") for i in range(2)],
    )
    assert await store.list_pending() == []

    # Second window: one more success — total now 3, threshold met.
    result = await learner.learn(
        cycles=[],
        intents=[],
        actions=[_make_action("b1", status="ok")],
    )
    assert len(result.proposed_rule_ids) == 1
    assert result.proposed_rule_ids[0] == "promote-test-plugin.test.do"


async def test_promotion_skipped_when_active_rule_already_covers_operation() -> None:
    rule = _make_rule("existing", operation="plugin.test.do", success_rate=0.95, applied_count=10)
    learner, _src, _ledger, store = _build(
        rules=[rule],
        config=RuleLearnerConfig(promotion_min_observations=3, promotion_min_success_rate=0.8),
    )
    actions = [_make_action(f"a{i}", status="ok") for i in range(3)]

    result = await learner.learn(cycles=[], intents=[], actions=actions)

    assert result.proposed_rule_ids == []
    assert await store.list_pending() == []


async def test_in_flight_actions_are_excluded_from_stats() -> None:
    learner, _src, ledger, _store = _build(
        config=RuleLearnerConfig(promotion_min_observations=3, promotion_min_success_rate=0.8),
    )
    actions = [
        _make_action("a1", status="ok"),
        _make_action("a2", settled=False),
        _make_action("a3", settled=False),
    ]

    result = await learner.learn(cycles=[], intents=[], actions=actions)

    assert result.proposed_rule_ids == []
    stats = await ledger.list_all()
    assert len(stats) == 1
    assert stats[0].evaluation_count == 1
    assert stats[0].success_count == 1


async def test_deprecation_flags_low_success_rule() -> None:
    rule = _make_rule(
        "rule-1",
        operation="plugin.test.do",
        applied_count=10,
        success_rate=0.2,
    )
    learner, _src, _ledger, store = _build(
        rules=[rule],
        config=RuleLearnerConfig(deprecation_min_evaluations=5, deprecation_max_success_rate=0.3),
    )

    result = await learner.learn(cycles=[], intents=[], actions=[])

    assert result.deprecated_rule_ids == ["deprecate-rule-1"]
    pending = await store.list_pending()
    assert len(pending) == 1
    proposal = pending[0]
    assert proposal.kind == "deprecate"
    assert proposal.target_rule_id == "rule-1"
    assert proposal.proposed_rule is None
    assert proposal.observation_count == 10
    assert proposal.success_rate == pytest.approx(0.2)


async def test_deprecation_skipped_when_below_min_evaluations() -> None:
    rule = _make_rule("rule-1", applied_count=2, success_rate=0.0)
    learner, _src, _ledger, store = _build(
        rules=[rule],
        config=RuleLearnerConfig(deprecation_min_evaluations=5, deprecation_max_success_rate=0.3),
    )

    result = await learner.learn(cycles=[], intents=[], actions=[])

    assert result.deprecated_rule_ids == []
    assert await store.list_pending() == []


async def test_deprecation_skipped_when_success_rate_above_threshold() -> None:
    rule = _make_rule("rule-1", applied_count=10, success_rate=0.5)
    learner, _src, _ledger, _store = _build(
        rules=[rule],
        config=RuleLearnerConfig(deprecation_min_evaluations=5, deprecation_max_success_rate=0.3),
    )

    result = await learner.learn(cycles=[], intents=[], actions=[])

    assert result.deprecated_rule_ids == []


async def test_ledger_dedupes_repeat_action_ids() -> None:
    ledger = InMemoryCandidateLedger()
    obs = CandidateObservation(
        key=CandidateKey(action_class="test", operation="plugin.test.do"),
        success=True,
        observed_at=_NOW,
        action_id="a1",
    )
    await ledger.update([obs])
    await ledger.update([obs])
    stats = await ledger.list_all()
    assert stats[0].evaluation_count == 1
    assert stats[0].success_count == 1


async def test_learner_wraps_ledger_failure_in_reflection_error() -> None:
    class _BoomLedger:
        async def update(
            self, observations: Sequence[CandidateObservation]
        ) -> list[CandidateStats]:
            raise RuntimeError("kaboom")

        async def list_all(self) -> list[CandidateStats]:
            return []

        async def mark_proposed(self, keys: Sequence[CandidateKey]) -> None:
            return None

    learner, _src, _ledger, _store = _build(ledger=_BoomLedger())
    with pytest.raises(ReflectionError):
        await learner.learn(
            cycles=[],
            intents=[],
            actions=[_make_action("a1", status="ok")],
        )


async def test_promotion_and_deprecation_emit_in_same_cycle() -> None:
    rule = _make_rule(
        "old-rule",
        operation="plugin.other.do",
        applied_count=10,
        success_rate=0.1,
    )
    learner, _src, _ledger, store = _build(
        rules=[rule],
        config=RuleLearnerConfig(
            promotion_min_observations=2,
            promotion_min_success_rate=0.5,
            deprecation_min_evaluations=5,
            deprecation_max_success_rate=0.3,
        ),
    )
    actions = [_make_action(f"a{i}", status="ok") for i in range(2)]

    result = await learner.learn(cycles=[], intents=[], actions=actions)

    assert len(result.proposed_rule_ids) == 1
    assert len(result.deprecated_rule_ids) == 1
    assert len(await store.list_pending()) == 2
