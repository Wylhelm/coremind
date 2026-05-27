"""Tests for the meta-cognition layer schemas and constants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coremind.meta.constants import (
    DEFAULT_POLICIES,
    FORBIDDEN_PARAMETER_PATHS,
    HARD_BOUNDS,
)
from coremind.meta.schemas import (
    AdjustmentPolicy,
    AdjustmentRecord,
    MetaConfig,
    MetaObservation,
    ProposedAdjustment,
    ValidationResult,
)


class TestMetaObservation:
    """Tests for MetaObservation model."""

    def test_defaults_auto_generated(self) -> None:
        obs = MetaObservation(kind="test", value=0.5, threshold=0.3, window_seconds=60)

        assert obs.observation_id
        assert obs.observed_at <= datetime.now(UTC)

    def test_two_observations_have_distinct_ids(self) -> None:
        obs_a = MetaObservation(kind="a", value=1.0, threshold=0.5, window_seconds=60)
        obs_b = MetaObservation(kind="b", value=2.0, threshold=0.5, window_seconds=60)

        assert obs_a.observation_id != obs_b.observation_id

    def test_frozen(self) -> None:
        obs = MetaObservation(kind="test", value=0.5, threshold=0.3, window_seconds=60)

        with pytest.raises(ValidationError):
            obs.value = 1.0

    def test_kind_must_be_nonempty(self) -> None:
        with pytest.raises(ValidationError):
            MetaObservation(kind="", value=0.5, threshold=0.3, window_seconds=60)

    def test_window_seconds_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            MetaObservation(kind="test", value=0.5, threshold=0.3, window_seconds=0)


class TestAdjustmentPolicy:
    """Tests for AdjustmentPolicy model."""

    def test_valid_policy(self) -> None:
        policy = AdjustmentPolicy(
            name="test_policy",
            description="A test policy",
            observation_kind="test_metric",
            trigger_condition="above",
            threshold=0.5,
            parameter_path="intention.min_salience",
            direction="increase",
            delta=0.05,
            min_value=0.2,
            max_value=0.7,
            cooldown_seconds=3600.0,
        )

        assert policy.name == "test_policy"
        assert policy.enabled is True
        assert policy.requires_user_approval is False

    def test_rejects_min_value_greater_than_max_value(self) -> None:
        with pytest.raises(ValidationError, match=r"min_value.*must be less than"):
            AdjustmentPolicy(
                name="bad",
                description="Bad bounds",
                observation_kind="test",
                trigger_condition="above",
                threshold=0.5,
                parameter_path="x.y",
                direction="increase",
                delta=0.1,
                min_value=0.8,
                max_value=0.2,
                cooldown_seconds=60.0,
            )

    def test_rejects_min_value_equal_to_max_value(self) -> None:
        with pytest.raises(ValidationError, match=r"min_value.*must be less than"):
            AdjustmentPolicy(
                name="equal",
                description="Equal bounds",
                observation_kind="test",
                trigger_condition="above",
                threshold=0.5,
                parameter_path="x.y",
                direction="increase",
                delta=0.1,
                min_value=0.5,
                max_value=0.5,
                cooldown_seconds=60.0,
            )

    def test_frozen(self) -> None:
        policy = AdjustmentPolicy(
            name="p",
            description="d",
            observation_kind="k",
            trigger_condition="above",
            threshold=0.5,
            parameter_path="x",
            direction="increase",
            delta=0.1,
            min_value=0.0,
            max_value=1.0,
            cooldown_seconds=60.0,
        )

        with pytest.raises(ValidationError):
            policy.threshold = 0.9


class TestAdjustmentRecord:
    """Tests for AdjustmentRecord model."""

    def test_defaults_auto_generated(self) -> None:
        record = AdjustmentRecord(
            policy_name="test_policy",
            parameter_path="intention.min_salience",
            old_value=0.35,
            new_value=0.30,
            reason="System too quiet",
            triggered_by_observation_id="obs-123",
        )

        assert record.adjustment_id
        assert record.applied_at <= datetime.now(UTC)
        assert record.rollback_at is None
        assert record.user_approved is False

    def test_frozen(self) -> None:
        record = AdjustmentRecord(
            policy_name="p",
            parameter_path="x",
            old_value=1,
            new_value=2,
            reason="r",
            triggered_by_observation_id="obs-1",
        )

        with pytest.raises(ValidationError):
            record.old_value = 99


class TestProposedAdjustment:
    """Tests for ProposedAdjustment model."""

    def test_construction(self) -> None:
        policy = AdjustmentPolicy(
            name="p",
            description="d",
            observation_kind="k",
            trigger_condition="above",
            threshold=0.5,
            parameter_path="x.y",
            direction="increase",
            delta=0.1,
            min_value=0.0,
            max_value=1.0,
            cooldown_seconds=60.0,
        )
        obs = MetaObservation(kind="k", value=0.6, threshold=0.5, window_seconds=60)

        proposed = ProposedAdjustment(
            policy=policy,
            observation=obs,
            parameter_path="x.y",
            old_value=0.3,
            new_value=0.4,
        )

        assert proposed.new_value == 0.4
        assert proposed.policy.name == "p"


class TestValidationResult:
    """Tests for ValidationResult model."""

    def test_valid_result(self) -> None:
        result = ValidationResult(valid=True)

        assert result.valid is True
        assert result.reason == ""

    def test_invalid_result_with_reason(self) -> None:
        result = ValidationResult(valid=False, reason="Path is forbidden")

        assert result.valid is False
        assert "forbidden" in result.reason


class TestMetaConfig:
    """Tests for MetaConfig model."""

    def test_defaults(self) -> None:
        config = MetaConfig()

        assert config.enabled is True
        assert config.observation_interval_seconds == 300.0
        assert config.max_adjustments_per_hour == 4
        assert config.require_observation_window_days == 1
        assert config.log_observations is True
        assert config.log_observations_retention_days == 30

    def test_frozen(self) -> None:
        config = MetaConfig()

        with pytest.raises(ValidationError):
            config.enabled = False


class TestConstants:
    """Tests for meta-loop constants."""

    def test_hard_bounds_all_have_min_less_than_max(self) -> None:
        for path, (min_v, max_v) in HARD_BOUNDS.items():
            assert min_v < max_v, f"Bad bounds for {path}"

    def test_forbidden_paths_no_duplicates(self) -> None:
        assert len(FORBIDDEN_PARAMETER_PATHS) == len(set(FORBIDDEN_PARAMETER_PATHS))

    def test_default_policies_have_unique_names(self) -> None:
        names = [p.name for p in DEFAULT_POLICIES]

        assert len(names) == len(set(names))

    def test_default_policies_all_enabled(self) -> None:
        for policy in DEFAULT_POLICIES:
            assert policy.enabled is True, f"Policy {policy.name} should be enabled"

    def test_default_policies_count(self) -> None:
        assert len(DEFAULT_POLICIES) == 7
