"""Tests for PolicyEvaluator — matches observations to policies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from coremind.meta.evaluator import PolicyEvaluator
from coremind.meta.schemas import AdjustmentPolicy, AdjustmentRecord, MetaObservation

# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


class _FakeHistory:
    """In-memory adjustment history for tests."""

    def __init__(self, records: dict[str, AdjustmentRecord] | None = None) -> None:
        self._records = records or {}

    def last_adjustment(self, parameter_path: str) -> AdjustmentRecord | None:
        return self._records.get(parameter_path)


class _FakeConfig:
    """In-memory config reader for tests."""

    def __init__(self, values: dict[str, float] | None = None) -> None:
        self._values = values or {}

    def get(self, dotted_path: str) -> float:
        return self._values.get(dotted_path, 0.0)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_policy(
    *,
    name: str = "test_policy",
    observation_kind: str = "test_metric",
    trigger_condition: Literal["above", "below", "between"] = "above",
    threshold: float = 0.5,
    threshold_upper: float | None = None,
    parameter_path: str = "intention.min_salience",
    direction: Literal["increase", "decrease"] = "increase",
    delta: float = 0.05,
    min_value: float = 0.0,
    max_value: float = 1.0,
    cooldown_seconds: float = 3600.0,
    requires_user_approval: bool = False,
    enabled: bool = True,
) -> AdjustmentPolicy:
    return AdjustmentPolicy(
        name=name,
        description=f"Test policy: {name}",
        observation_kind=observation_kind,
        trigger_condition=trigger_condition,
        threshold=threshold,
        threshold_upper=threshold_upper,
        parameter_path=parameter_path,
        direction=direction,
        delta=delta,
        min_value=min_value,
        max_value=max_value,
        cooldown_seconds=cooldown_seconds,
        requires_user_approval=requires_user_approval,
        enabled=enabled,
    )


def _make_obs(
    *,
    kind: str = "test_metric",
    value: float = 0.8,
    threshold: float = 0.5,
    metadata: dict[str, object] | None = None,
) -> MetaObservation:
    return MetaObservation(
        kind=kind,
        value=value,
        threshold=threshold,
        window_seconds=3600.0,
        metadata=metadata or {},
    )


def _make_record(
    *,
    parameter_path: str = "intention.min_salience",
    applied_at: datetime | None = None,
) -> AdjustmentRecord:
    return AdjustmentRecord(
        policy_name="test",
        parameter_path=parameter_path,
        old_value=0.3,
        new_value=0.35,
        reason="test",
        triggered_by_observation_id="obs-1",
        applied_at=applied_at or datetime.now(UTC),
    )


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_triggers_policy_above_threshold() -> None:
    """Observation above threshold produces a proposal."""
    policy = _make_policy(trigger_condition="above", threshold=0.5)
    obs = _make_obs(value=0.8)
    config = _FakeConfig({"intention.min_salience": 0.4})

    evaluator = PolicyEvaluator([policy], _FakeHistory(), config)
    proposals = evaluator.evaluate([obs])

    assert len(proposals) == 1
    assert proposals[0].old_value == 0.4
    assert proposals[0].new_value == 0.45


def test_does_not_trigger_below_threshold() -> None:
    """Observation below threshold produces nothing."""
    policy = _make_policy(trigger_condition="above", threshold=0.5)
    obs = _make_obs(value=0.3)

    evaluator = PolicyEvaluator([policy], _FakeHistory(), _FakeConfig())
    proposals = evaluator.evaluate([obs])

    assert proposals == []


def test_triggers_policy_below_threshold() -> None:
    """Below-condition triggers when value is under threshold."""
    policy = _make_policy(trigger_condition="below", threshold=1.0, direction="decrease")
    obs = _make_obs(value=0.5)
    config = _FakeConfig({"intention.min_salience": 0.4})

    evaluator = PolicyEvaluator([policy], _FakeHistory(), config)
    proposals = evaluator.evaluate([obs])

    assert len(proposals) == 1
    assert proposals[0].new_value == pytest.approx(0.35)


def test_respects_cooldown() -> None:
    """Policy not triggered if last adjustment is within cooldown."""
    policy = _make_policy(cooldown_seconds=7200.0)
    obs = _make_obs(value=0.8)
    recent = _make_record(applied_at=datetime.now(UTC) - timedelta(seconds=100))
    history = _FakeHistory({"intention.min_salience": recent})

    evaluator = PolicyEvaluator([policy], history, _FakeConfig({"intention.min_salience": 0.4}))
    proposals = evaluator.evaluate([obs])

    assert proposals == []


def test_fires_after_cooldown_elapsed() -> None:
    """Policy triggers once cooldown has fully elapsed."""
    policy = _make_policy(cooldown_seconds=3600.0)
    obs = _make_obs(value=0.8)
    old = _make_record(applied_at=datetime.now(UTC) - timedelta(seconds=7200))
    history = _FakeHistory({"intention.min_salience": old})
    config = _FakeConfig({"intention.min_salience": 0.4})

    evaluator = PolicyEvaluator([policy], history, config)
    proposals = evaluator.evaluate([obs])

    assert len(proposals) == 1


def test_skips_disabled_policy() -> None:
    """Disabled policies are never triggered."""
    policy = _make_policy(enabled=False)
    obs = _make_obs(value=0.8)

    config = _FakeConfig({"intention.min_salience": 0.4})
    evaluator = PolicyEvaluator([policy], _FakeHistory(), config)
    proposals = evaluator.evaluate([obs])

    assert proposals == []


def test_resolves_placeholder_in_path() -> None:
    """<plugin_id> in parameter_path is substituted from metadata."""
    policy = _make_policy(
        observation_kind="plugin_error_rate",
        parameter_path="plugins.<plugin_id>.poll_interval_seconds",
        delta=0.0,
        min_value=30.0,
        max_value=86400.0,
    )
    obs = _make_obs(kind="plugin_error_rate", value=0.8, metadata={"plugin_id": "homeassistant"})
    config = _FakeConfig({"plugins.homeassistant.poll_interval_seconds": 120.0})

    evaluator = PolicyEvaluator([policy], _FakeHistory(), config)
    proposals = evaluator.evaluate([obs])

    assert len(proposals) == 1
    assert proposals[0].parameter_path == "plugins.homeassistant.poll_interval_seconds"
    assert proposals[0].new_value == 240.0  # doubled


def test_clamps_to_policy_bounds() -> None:
    """new_value is clamped to [min_value, max_value]."""
    policy = _make_policy(
        direction="increase",
        delta=0.5,
        min_value=0.0,
        max_value=0.7,
    )
    obs = _make_obs(value=0.8)
    config = _FakeConfig({"intention.min_salience": 0.6})

    evaluator = PolicyEvaluator([policy], _FakeHistory(), config)
    proposals = evaluator.evaluate([obs])

    assert len(proposals) == 1
    assert proposals[0].new_value == 0.7  # clamped to max


def test_poll_interval_multiplied_not_added() -> None:
    """poll_interval paths with delta=0.0 use 2x/0.5x logic."""
    # Increase case: doubles
    policy_up = _make_policy(
        name="throttle",
        observation_kind="plugin_error_rate",
        parameter_path="plugins.weather.poll_interval_seconds",
        direction="increase",
        delta=0.0,
        min_value=30.0,
        max_value=86400.0,
    )
    obs = _make_obs(kind="plugin_error_rate", value=0.8)
    config = _FakeConfig({"plugins.weather.poll_interval_seconds": 300.0})

    proposals = PolicyEvaluator([policy_up], _FakeHistory(), config).evaluate([obs])

    assert len(proposals) == 1
    assert proposals[0].new_value == 600.0

    # Decrease case: halves
    policy_down = _make_policy(
        name="restore",
        observation_kind="plugin_error_rate",
        trigger_condition="below",
        threshold=0.5,
        parameter_path="plugins.weather.poll_interval_seconds",
        direction="decrease",
        delta=0.0,
        min_value=30.0,
        max_value=86400.0,
    )
    obs_low = _make_obs(kind="plugin_error_rate", value=0.02)
    config_high = _FakeConfig({"plugins.weather.poll_interval_seconds": 600.0})

    proposals = PolicyEvaluator([policy_down], _FakeHistory(), config_high).evaluate([obs_low])

    assert len(proposals) == 1
    assert proposals[0].new_value == 300.0


def test_no_proposal_when_value_unchanged() -> None:
    """If clamping makes new_value == old_value, skip."""
    policy = _make_policy(
        direction="increase",
        delta=0.05,
        min_value=0.0,
        max_value=0.7,
    )
    obs = _make_obs(value=0.8)
    # old_value is already at max, so clamped new == old
    config = _FakeConfig({"intention.min_salience": 0.7})

    evaluator = PolicyEvaluator([policy], _FakeHistory(), config)
    proposals = evaluator.evaluate([obs])

    assert proposals == []


def test_multiple_policies_same_observation() -> None:
    """Multiple policies can match a single observation."""
    policy_a = _make_policy(name="policy_a", parameter_path="intention.min_salience")
    policy_b = _make_policy(
        name="policy_b",
        parameter_path="intention.min_confidence",
        min_value=0.0,
        max_value=1.0,
    )
    obs = _make_obs(value=0.8)
    config = _FakeConfig(
        {
            "intention.min_salience": 0.4,
            "intention.min_confidence": 0.5,
        }
    )

    evaluator = PolicyEvaluator([policy_a, policy_b], _FakeHistory(), config)
    proposals = evaluator.evaluate([obs])

    assert len(proposals) == 2
    paths = {p.parameter_path for p in proposals}
    assert paths == {"intention.min_salience", "intention.min_confidence"}
