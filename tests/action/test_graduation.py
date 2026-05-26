"""Tests for the slider graduation evaluator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coremind.action.autonomy import AutonomyConfig, GraduationConfig
from coremind.action.graduation import (
    ActionOutcome,
    GraduationEvaluator,
    SliderGraduationProposal,
)


def _outcome(
    action_class: str = "light.turn_on",
    approved: bool = True,
    days_ago: int = 5,
) -> ActionOutcome:
    """Create an ActionOutcome for testing."""
    return ActionOutcome(
        action_class=action_class,
        approved=approved,
        timestamp=datetime.now(UTC) - timedelta(days=days_ago),
    )


class TestGraduationEvaluator:
    """Tests for GraduationEvaluator.evaluate()."""

    def test_proposal_generated_when_thresholds_met(self) -> None:
        config = GraduationConfig(
            min_approvals_before_promotion=5,
            min_approval_rate_for_promotion=0.8,
            min_observation_days=30,
        )
        evaluator = GraduationEvaluator(config)
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(10)]

        result = evaluator.evaluate("lights", 0.8, history)

        assert result is not None
        assert isinstance(result, SliderGraduationProposal)
        assert result.domain == "lights"
        assert result.current_slider == 0.8
        assert result.proposed_slider == 0.9  # +0.1 (max_promotion default)
        assert result.approval_rate == 1.0
        assert result.total_actions == 10
        assert result.approved_actions == 10

    def test_no_proposal_when_too_few_actions(self) -> None:
        config = GraduationConfig(min_approvals_before_promotion=10)
        evaluator = GraduationEvaluator(config)
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(5)]

        result = evaluator.evaluate("lights", 0.8, history)

        assert result is None

    def test_no_proposal_when_approval_rate_too_low(self) -> None:
        config = GraduationConfig(
            min_approvals_before_promotion=5,
            min_approval_rate_for_promotion=0.8,
            min_observation_days=30,
        )
        evaluator = GraduationEvaluator(config)
        # 4 approved + 6 denied = 40% approval rate
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(4)]
        history += [_outcome("light.turn_on", approved=False, days_ago=i) for i in range(6)]

        result = evaluator.evaluate("lights", 0.8, history)

        assert result is None

    def test_no_proposal_when_disabled(self) -> None:
        config = GraduationConfig(enabled=False)
        evaluator = GraduationEvaluator(config)
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(20)]

        result = evaluator.evaluate("lights", 0.8, history)

        assert result is None

    def test_no_proposal_when_already_at_max(self) -> None:
        config = GraduationConfig(min_approvals_before_promotion=5)
        evaluator = GraduationEvaluator(config)
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(10)]

        result = evaluator.evaluate("lights", 1.0, history)

        assert result is None

    def test_cooldown_prevents_rapid_promotions(self) -> None:
        config = GraduationConfig(
            min_approvals_before_promotion=5,
            promotion_cooldown_days=7,
            min_observation_days=30,
        )
        # Last promotion was 3 days ago — within cooldown.
        last_promotions = {"lights": datetime.now(UTC) - timedelta(days=3)}
        evaluator = GraduationEvaluator(config, last_promotions=last_promotions)
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(10)]

        result = evaluator.evaluate("lights", 0.8, history)

        assert result is None

    def test_cooldown_allows_after_expiry(self) -> None:
        config = GraduationConfig(
            min_approvals_before_promotion=5,
            promotion_cooldown_days=7,
            min_observation_days=30,
        )
        # Last promotion was 10 days ago — cooldown expired.
        last_promotions = {"lights": datetime.now(UTC) - timedelta(days=10)}
        evaluator = GraduationEvaluator(config, last_promotions=last_promotions)
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(10)]

        result = evaluator.evaluate("lights", 0.8, history)

        assert result is not None
        assert result.domain == "lights"

    def test_promotion_capped_at_max_per_proposal(self) -> None:
        config = GraduationConfig(
            min_approvals_before_promotion=5,
            max_promotion_per_proposal=0.1,
            min_observation_days=30,
        )
        evaluator = GraduationEvaluator(config)
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(10)]

        result = evaluator.evaluate("lights", 0.85, history)

        assert result is not None
        assert result.proposed_slider == 0.95  # 0.85 + 0.1

    def test_promotion_capped_at_1_0(self) -> None:
        config = GraduationConfig(
            min_approvals_before_promotion=5,
            max_promotion_per_proposal=0.2,
            min_observation_days=30,
        )
        evaluator = GraduationEvaluator(config)
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(10)]

        result = evaluator.evaluate("lights", 0.95, history)

        assert result is not None
        assert result.proposed_slider == 1.0  # capped

    def test_actions_outside_observation_window_excluded(self) -> None:
        config = GraduationConfig(
            min_approvals_before_promotion=5,
            min_observation_days=7,
        )
        evaluator = GraduationEvaluator(config)
        # 3 recent + 10 old (outside window) = only 3 considered.
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(3)]
        history += [_outcome("light.turn_on", approved=True, days_ago=30) for _ in range(10)]

        result = evaluator.evaluate("lights", 0.8, history)

        assert result is None  # Only 3 within window, need 5.

    def test_only_domain_actions_counted(self) -> None:
        config = GraduationConfig(
            min_approvals_before_promotion=5,
            min_observation_days=30,
        )
        evaluator = GraduationEvaluator(config)
        # 3 lights + 10 finance — evaluating "lights" should only see 3.
        history = [_outcome("light.turn_on", approved=True, days_ago=i) for i in range(3)]
        history += [_outcome("finance.check", approved=True, days_ago=i) for i in range(10)]

        result = evaluator.evaluate("lights", 0.8, history)

        assert result is None  # Only 3 for lights.


class TestEvaluateAll:
    """Tests for GraduationEvaluator.evaluate_all()."""

    def test_returns_proposals_for_qualifying_domains(self) -> None:
        config = GraduationConfig(
            min_approvals_before_promotion=5,
            min_observation_days=30,
        )
        evaluator = GraduationEvaluator(config)
        autonomy = AutonomyConfig(domains={"lights": 0.8, "media": 0.7, "finance": 0.1})
        history = [
            *[_outcome("light.turn_on", approved=True, days_ago=i) for i in range(10)],
            *[_outcome("media.play", approved=True, days_ago=i) for i in range(10)],
            # Only 2 finance actions — not enough.
            *[_outcome("finance.check", approved=True, days_ago=i) for i in range(2)],
        ]

        proposals = evaluator.evaluate_all(autonomy, history)

        domains = {p.domain for p in proposals}
        assert "lights" in domains
        assert "media" in domains
        assert "finance" not in domains

    def test_empty_history_returns_no_proposals(self) -> None:
        config = GraduationConfig()
        evaluator = GraduationEvaluator(config)
        autonomy = AutonomyConfig()

        proposals = evaluator.evaluate_all(autonomy, [])

        assert proposals == []
