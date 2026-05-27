"""Pydantic models for the meta-cognition layer (L8).

All models are frozen value objects. No runtime logic, no I/O.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MetaObservation(BaseModel):
    """A single measured metric about system performance."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: str = Field(min_length=1)
    value: float
    threshold: float
    window_seconds: float = Field(gt=0.0)
    triggers_policy: bool = False
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class AdjustmentPolicy(BaseModel):
    """A rule that maps observations to parameter changes."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    observation_kind: str = Field(min_length=1)
    trigger_condition: Literal["above", "below", "between"]
    threshold: float
    threshold_upper: float | None = None
    parameter_path: str = Field(min_length=1)
    direction: Literal["increase", "decrease"]
    delta: float = Field(ge=0.0)
    min_value: float
    max_value: float
    cooldown_seconds: float = Field(gt=0.0)
    requires_user_approval: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def _bounds_are_valid(self) -> AdjustmentPolicy:
        if self.min_value >= self.max_value:
            msg = f"min_value ({self.min_value}) must be less than max_value ({self.max_value})"
            raise ValueError(msg)
        return self


class AdjustmentRecord(BaseModel):
    """Record of an applied adjustment."""

    model_config = ConfigDict(frozen=True)

    adjustment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    policy_name: str = Field(min_length=1)
    parameter_path: str = Field(min_length=1)
    old_value: Any
    new_value: Any
    reason: str = Field(min_length=1)
    triggered_by_observation_id: str = Field(min_length=1)
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rollback_at: datetime | None = None
    user_approved: bool = False
    user_approved_at: datetime | None = None


class ProposedAdjustment(BaseModel):
    """An adjustment proposed by the evaluator, not yet validated."""

    model_config = ConfigDict(frozen=True)

    policy: AdjustmentPolicy
    observation: MetaObservation
    parameter_path: str = Field(min_length=1)
    old_value: float
    new_value: float


class ValidationResult(BaseModel):
    """Result of safety validation."""

    model_config = ConfigDict(frozen=True)

    valid: bool
    reason: str = ""


class MetaConfig(BaseModel):
    """Configuration for the meta-loop."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    observation_interval_seconds: float = Field(default=300.0, gt=0.0)
    max_adjustments_per_hour: int = Field(default=4, ge=1)
    require_observation_window_days: int = Field(default=1, ge=1)
    log_observations: bool = True
    log_observations_retention_days: int = Field(default=30, ge=1)


class MetaStatus(BaseModel):
    """Summary status of the meta-loop for CLI and dashboard display."""

    model_config = ConfigDict(frozen=True)

    enabled: bool
    last_tick: datetime | None = None
    observations_count: int = 0
    adjustments_count: int = 0
    pending_proposals_count: int = 0
