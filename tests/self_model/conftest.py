"""Shared fixtures for self-model tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coremind.self_model.entities import SelfFact


@pytest.fixture
def sample_person_fact() -> SelfFact:
    """A declared person fact for testing."""
    return SelfFact(
        id="01J0000000000000000000AAAA",
        entity_type="person",
        entity_id="aurelie",
        attribute="relationship",
        value="fille",
        confidence=1.0,
        method="declared",
        source="user",
        evidence=["conversation:2026-05-28"],
        created_at=datetime(2026, 5, 28, 10, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 28, 10, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def sample_routine_fact() -> SelfFact:
    """An observed routine fact for testing."""
    return SelfFact(
        id="01J0000000000000000000BBBB",
        entity_type="routine",
        entity_id="coding",
        attribute="time_window",
        value="20:00-00:00",
        confidence=0.85,
        method="observed",
        source="coremind.plugin.github",
        evidence=["event:abc123", "event:def456", "event:ghi789"],
        created_at=datetime(2026, 5, 20, 8, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 27, 22, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def sample_project_fact() -> SelfFact:
    """An observed project fact for testing."""
    return SelfFact(
        id="01J0000000000000000000CCCC",
        entity_type="project",
        entity_id="coremind",
        attribute="current_phase",
        value="6",
        confidence=0.95,
        method="observed",
        source="coremind.plugin.github",
        evidence=["event:xyz789"],
        created_at=datetime(2026, 5, 25, 14, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 28, 9, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def sample_synthesized_fact() -> SelfFact:
    """A synthesized (inferred) fact for testing confidence decay."""
    return SelfFact(
        id="01J0000000000000000000DDDD",
        entity_type="goal",
        entity_id="g-bot-immo",
        attribute="intent_vs_action_gap",
        value="high",
        confidence=0.6,
        method="synthesized",
        source="self_model.extractor",
        evidence=["event:immo001", "event:github002"],
        created_at=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
    )
