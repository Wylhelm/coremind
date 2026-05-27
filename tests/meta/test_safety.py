"""Tests for MetaSafetyValidator — enforces forbidden paths and hard bounds."""

from __future__ import annotations

from coremind.meta.constants import FORBIDDEN_PARAMETER_PATHS, HARD_BOUNDS
from coremind.meta.safety_validator import MetaSafetyValidator
from coremind.meta.schemas import (
    AdjustmentPolicy,
    MetaObservation,
    ProposedAdjustment,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_DUMMY_POLICY = AdjustmentPolicy(
    name="dummy",
    description="Dummy policy for tests",
    observation_kind="test",
    trigger_condition="above",
    threshold=0.5,
    parameter_path="x.y",
    direction="increase",
    delta=0.1,
    min_value=0.0,
    max_value=1.0,
    cooldown_seconds=60.0,
)

_DUMMY_OBS = MetaObservation(
    kind="test",
    value=0.8,
    threshold=0.5,
    window_seconds=3600.0,
)


def _make_proposal(
    *,
    parameter_path: str = "intention.min_salience",
    new_value: float = 0.5,
    old_value: float = 0.4,
) -> ProposedAdjustment:
    return ProposedAdjustment(
        policy=_DUMMY_POLICY,
        observation=_DUMMY_OBS,
        parameter_path=parameter_path,
        old_value=old_value,
        new_value=new_value,
    )


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_rejects_every_forbidden_path() -> None:
    """Iterate all FORBIDDEN_PARAMETER_PATHS and verify rejection."""
    validator = MetaSafetyValidator(FORBIDDEN_PARAMETER_PATHS, HARD_BOUNDS)

    for pattern in FORBIDDEN_PARAMETER_PATHS:
        # Turn globs into concrete paths
        test_path = pattern.replace("*", "anything")
        proposal = _make_proposal(parameter_path=test_path, new_value=0.5)
        result = validator.validate(proposal)

        assert not result.valid, f"Should have blocked: {pattern}"
        assert "forbidden" in result.reason.lower()


def test_rejects_above_hard_max() -> None:
    """Value above hard max is rejected."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)
    proposal = _make_proposal(
        parameter_path="intention.min_salience",
        new_value=0.85,  # max is 0.70
    )

    result = validator.validate(proposal)

    assert not result.valid
    assert "above" in result.reason.lower()


def test_rejects_below_hard_min() -> None:
    """Value below hard min is rejected."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)
    proposal = _make_proposal(
        parameter_path="intention.min_salience",
        new_value=0.10,  # min is 0.20
    )

    result = validator.validate(proposal)

    assert not result.valid
    assert "below" in result.reason.lower()


def test_accepts_value_within_bounds() -> None:
    """Value within bounds passes."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)
    proposal = _make_proposal(
        parameter_path="intention.min_salience",
        new_value=0.45,
    )

    result = validator.validate(proposal)

    assert result.valid


def test_wildcard_bounds_match() -> None:
    """Glob patterns in HARD_BOUNDS match concrete paths."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)
    proposal = _make_proposal(
        parameter_path="plugins.homeassistant.poll_interval_seconds",
        new_value=100000.0,  # above 86400 max
    )

    result = validator.validate(proposal)

    assert not result.valid
    assert "above" in result.reason.lower()


def test_unforbidden_path_passes() -> None:
    """A path not in forbidden list is allowed."""
    validator = MetaSafetyValidator(FORBIDDEN_PARAMETER_PATHS, {})
    proposal = _make_proposal(
        parameter_path="intention.min_salience",
        new_value=0.5,
    )

    result = validator.validate(proposal)

    assert result.valid


def test_enforces_all_hard_bounds() -> None:
    """Every entry in HARD_BOUNDS is enforceable (min and max)."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)

    for path_pattern, (min_v, max_v) in HARD_BOUNDS.items():
        concrete_path = path_pattern.replace("*", "test")

        # Exceeds max
        result = validator.validate(
            _make_proposal(parameter_path=concrete_path, new_value=max_v + 1)
        )
        assert not result.valid, f"Should block max for {path_pattern}"

        # Below min
        result = validator.validate(
            _make_proposal(parameter_path=concrete_path, new_value=min_v - 1)
        )
        assert not result.valid, f"Should block min for {path_pattern}"

        # Within bounds passes
        mid = (min_v + max_v) / 2
        result = validator.validate(_make_proposal(parameter_path=concrete_path, new_value=mid))
        assert result.valid, f"Should allow mid for {path_pattern}"


def test_exact_bound_at_boundary_values() -> None:
    """Values exactly at min or max are accepted (bounds are inclusive)."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)

    proposal_at_min = _make_proposal(
        parameter_path="intention.min_salience",
        new_value=0.20,  # exactly at min
    )
    assert validator.validate(proposal_at_min).valid

    proposal_at_max = _make_proposal(
        parameter_path="intention.min_salience",
        new_value=0.70,  # exactly at max
    )
    assert validator.validate(proposal_at_max).valid


def test_unknown_path_with_no_bounds_passes() -> None:
    """A path with no matching bound is allowed."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)
    proposal = _make_proposal(
        parameter_path="some.unknown.path",
        new_value=999.0,
    )

    result = validator.validate(proposal)

    assert result.valid
