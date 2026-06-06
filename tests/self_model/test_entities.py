"""Tests for self-model entity schemas — serialization, validation, confidence tiers."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from coremind.self_model.entities import (
    GoalEntity,
    IdentityEntity,
    PersonEntity,
    PreferenceEntity,
    ProjectEntity,
    RoutineEntity,
    SelfFact,
)


class TestSelfFact:
    """Validate SelfFact model contracts."""

    def test_valid_declared_fact_roundtrips(self, sample_person_fact: SelfFact) -> None:
        data = sample_person_fact.model_dump(mode="json")
        restored = SelfFact.model_validate(data)

        assert restored.id == sample_person_fact.id
        assert restored.entity_type == "person"
        assert restored.confidence == 1.0
        assert restored.method == "declared"
        assert restored.active is True

    def test_frozen_model_rejects_mutation(self, sample_person_fact: SelfFact) -> None:
        with pytest.raises(ValidationError):
            sample_person_fact.confidence = 0.5  # type: ignore[assignment]

    def test_confidence_must_be_bounded(self) -> None:
        with pytest.raises(ValidationError, match="confidence"):
            SelfFact(
                id="01J000TEST",
                entity_type="person",
                entity_id="test",
                attribute="x",
                value="y",
                confidence=1.5,
                method="declared",
                source="user",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

    def test_negative_confidence_rejected(self) -> None:
        with pytest.raises(ValidationError, match="confidence"):
            SelfFact(
                id="01J000TEST",
                entity_type="person",
                entity_id="test",
                attribute="x",
                value="y",
                confidence=-0.1,
                method="declared",
                source="user",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

    def test_invalid_entity_type_rejected(self) -> None:
        with pytest.raises(ValidationError, match="entity_type"):
            SelfFact(
                id="01J000TEST",
                entity_type="invalid_type",  # type: ignore[arg-type]
                entity_id="test",
                attribute="x",
                value="y",
                confidence=0.5,
                method="observed",
                source="test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

    def test_invalid_method_rejected(self) -> None:
        with pytest.raises(ValidationError, match="method"):
            SelfFact(
                id="01J000TEST",
                entity_type="person",
                entity_id="test",
                attribute="x",
                value="y",
                confidence=0.5,
                method="guessed",  # type: ignore[arg-type]
                source="test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

    def test_empty_entity_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="entity_id"):
            SelfFact(
                id="01J000TEST",
                entity_type="person",
                entity_id="",
                attribute="x",
                value="y",
                confidence=0.5,
                method="observed",
                source="test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            SelfFact(
                id="01J000TEST",
                entity_type="person",
                entity_id="test",
                attribute="x",
                value="y",
                confidence=0.5,
                method="observed",
                source="test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                unknown_field="boom",  # type: ignore[call-arg]
            )

    def test_complex_json_value(self) -> None:
        fact = SelfFact(
            id="01J000COMPLEX",
            entity_type="identity",
            entity_id="tech",
            attribute="languages",
            value=["python", "typescript", "rust"],
            confidence=0.9,
            method="observed",
            source="coremind.plugin.github",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        assert fact.value == ["python", "typescript", "rust"]
        data = fact.model_dump(mode="json")
        assert data["value"] == ["python", "typescript", "rust"]

    def test_superseded_by_defaults_to_none(self, sample_person_fact: SelfFact) -> None:
        assert sample_person_fact.superseded_by is None


class TestPersonEntity:
    """Validate PersonEntity model."""

    def test_minimal_person(self) -> None:
        person = PersonEntity(
            entity_id="jeff",
            name="Jeff",
            relationship="ami",
        )

        assert person.location is None
        assert person.birthday is None
        assert person.last_contact is None

    def test_full_person_roundtrips(self) -> None:
        person = PersonEntity(
            entity_id="aurelie",
            name="Aurélie",
            relationship="fille",
            location="Montréal",
            birthday=date(2001, 5, 15),
            last_contact=datetime(2026, 5, 23, 18, 30, tzinfo=UTC),
            contact_frequency_days=3.5,
        )

        data = person.model_dump(mode="json")
        restored = PersonEntity.model_validate(data)

        assert restored.name == "Aurélie"
        assert restored.birthday == date(2001, 5, 15)
        assert restored.contact_frequency_days == 3.5


class TestGoalEntity:
    """Validate GoalEntity model."""

    def test_active_goal_with_progress(self) -> None:
        goal = GoalEntity(
            entity_id="retirement",
            description="Retraite à 65 ans dans un chalet",
            target_metric="savings_rate",
            deadline=date(2043, 6, 1),
            current_progress_pct=12.5,
            status="active",
        )

        assert goal.status == "active"
        assert goal.current_progress_pct == 12.5

    def test_progress_must_be_bounded(self) -> None:
        with pytest.raises(ValidationError, match="current_progress_pct"):
            GoalEntity(
                entity_id="test",
                description="test",
                current_progress_pct=150.0,
            )


class TestProjectEntity:
    """Validate ProjectEntity model."""

    def test_active_project(self) -> None:
        project = ProjectEntity(
            entity_id="coremind",
            name="CoreMind",
            current_phase="6A",
            progress_pct=10.0,
            last_commit=datetime(2026, 5, 28, 21, 0, tzinfo=UTC),
            days_inactive=0,
            status="active",
            intensity="high",
        )

        assert project.intensity == "high"
        assert project.days_inactive == 0

    def test_paused_project(self) -> None:
        project = ProjectEntity(
            entity_id="g-bot-immo",
            name="G-Bot Immo",
            status="paused",
            days_inactive=45,
            intensity="low",
        )

        assert project.status == "paused"


class TestRoutineEntity:
    """Validate RoutineEntity model."""

    def test_coding_routine(self) -> None:
        routine = RoutineEntity(
            entity_id="coding",
            name="Evening coding session",
            time_window="20:00-00:00",
            days=["mon", "tue", "wed", "thu", "fri"],
            frequency="daily",
            avg_duration_minutes=180.0,
        )

        assert routine.time_window == "20:00-00:00"
        assert len(routine.days or []) == 5


class TestIdentityEntity:
    """Validate IdentityEntity model."""

    def test_tech_identity(self) -> None:
        identity = IdentityEntity(
            entity_id="tech",
            domain="tech",
            attributes={
                "role": "architecte_ia",
                "languages": ["python", "typescript"],
                "stack": ["ollama", "docker", "surrealdb"],
            },
        )

        assert identity.attributes["role"] == "architecte_ia"


class TestPreferenceEntity:
    """Validate PreferenceEntity model."""

    def test_code_preference(self) -> None:
        pref = PreferenceEntity(
            entity_id="code_time",
            domain="code",
            attribute="prefers_evening",
            value=True,
            learned_from="github_commit_times",
        )

        assert pref.value is True
        assert pref.learned_from == "github_commit_times"
