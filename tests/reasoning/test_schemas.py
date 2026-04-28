"""Tests for reasoning output schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coremind.reasoning.schemas import (
    Anomaly,
    Pattern,
    Prediction,
    ReasoningOutput,
    TokenUsage,
)
from coremind.world.model import EntityRef


def test_pattern_requires_fields() -> None:
    p = Pattern(
        id="p1",
        description="daily commute",
        entities_involved=[EntityRef(type="user", id="me")],
        confidence=0.8,
        evidence=["ev-1"],
    )
    assert p.confidence == 0.8


def test_prediction_falsifiable_required() -> None:
    with pytest.raises(ValidationError):
        Prediction(
            id="x",
            hypothesis="",  # empty not allowed
            horizon_hours=1,
            confidence=0.5,
            falsifiable_by="observation",
        )


def test_anomaly_severity_literal() -> None:
    a = Anomaly(
        id="a1",
        description="temp spike",
        entity=EntityRef(type="ha_sensor", id="sensor.room"),
        severity="high",
        baseline_description="usually 21C",
    )
    assert a.severity == "high"


def test_reasoning_output_round_trip() -> None:
    out = ReasoningOutput(
        cycle_id="c1",
        model_used="ollama/llama3",
        patterns=[],
        anomalies=[],
        predictions=[],
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
    )
    dumped = out.model_dump_json()
    reloaded = ReasoningOutput.model_validate_json(dumped)
    assert reloaded.cycle_id == "c1"
    assert reloaded.token_usage.total_tokens == 30
