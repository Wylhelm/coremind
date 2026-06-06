"""Tests for SelfModelStore — CRUD round-trips, deduplication, confidence decay.

These tests use a mock DB adapter to test store logic without SurrealDB.
Integration tests with a real SurrealDB instance are marked separately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from coremind.self_model.entities import SelfFact
from coremind.self_model.errors import SelfModelStoreError
from coremind.self_model.store import SelfModelStore, _extract_rows


class FakeDB:
    """In-memory fake SurrealDB client for unit testing."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._query_results: list[Any] = []

    async def query(self, query: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Simulate SurrealDB query responses."""
        if self._query_results:
            return self._query_results.pop(0)
        return [{"result": [], "status": "OK"}]

    def set_next_results(self, *results: Any) -> None:
        """Queue results for upcoming queries."""
        self._query_results.extend(results)


@pytest.fixture
def fake_db() -> FakeDB:
    return FakeDB()


@pytest.fixture
def store(fake_db: FakeDB) -> SelfModelStore:
    return SelfModelStore(db=fake_db)


class TestSelfModelStoreInitialize:
    """Test schema initialization."""

    @pytest.mark.asyncio
    async def test_initialize_succeeds(self, store: SelfModelStore, fake_db: FakeDB) -> None:
        fake_db.set_next_results([{"result": None, "status": "OK"}])

        await store.initialize()
        # No exception = success

    @pytest.mark.asyncio
    async def test_initialize_wraps_db_errors(self, store: SelfModelStore, fake_db: FakeDB) -> None:
        fake_db.query = AsyncMock(side_effect=RuntimeError("connection lost"))  # type: ignore[method-assign]

        with pytest.raises(SelfModelStoreError, match="Failed to initialize"):
            await store.initialize()


class TestSelfModelStoreUpsert:
    """Test fact upsert with deduplication."""

    @pytest.mark.asyncio
    async def test_upsert_new_fact(
        self, store: SelfModelStore, fake_db: FakeDB, sample_person_fact: SelfFact
    ) -> None:
        # First query: find existing → none
        # Second query: CREATE
        fake_db.set_next_results(
            [{"result": [], "status": "OK"}],
            [{"result": [], "status": "OK"}],
        )

        result = await store.upsert_fact(sample_person_fact)

        assert result.id == sample_person_fact.id

    @pytest.mark.asyncio
    async def test_upsert_skips_lower_confidence(
        self, store: SelfModelStore, fake_db: FakeDB, sample_person_fact: SelfFact
    ) -> None:
        # Existing fact at confidence 1.0
        fake_db.set_next_results(
            [{"result": [sample_person_fact.model_dump(mode="json")], "status": "OK"}],
        )

        # Try to upsert with lower confidence
        lower = SelfFact(
            id="01J000NEW",
            entity_type="person",
            entity_id="aurelie",
            attribute="relationship",
            value="daughter",
            confidence=0.7,
            method="observed",
            source="test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        result = await store.upsert_fact(lower)

        # Should return the existing higher-confidence fact
        assert result.id == sample_person_fact.id
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_upsert_supersedes_lower_confidence_existing(
        self, store: SelfModelStore, fake_db: FakeDB, sample_routine_fact: SelfFact
    ) -> None:
        # Existing fact at confidence 0.85
        fake_db.set_next_results(
            [{"result": [sample_routine_fact.model_dump(mode="json")], "status": "OK"}],
            [{"result": [], "status": "OK"}],  # UPDATE (supersede old)
            [{"result": [], "status": "OK"}],  # CREATE (new fact)
        )

        higher = SelfFact(
            id="01J000HIGHER",
            entity_type="routine",
            entity_id="coding",
            attribute="time_window",
            value="19:00-23:30",
            confidence=0.92,
            method="observed",
            source="coremind.plugin.github",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        result = await store.upsert_fact(higher)

        assert result.id == "01J000HIGHER"
        assert result.confidence == 0.92

    @pytest.mark.asyncio
    async def test_upsert_wraps_db_errors(
        self, store: SelfModelStore, fake_db: FakeDB, sample_person_fact: SelfFact
    ) -> None:
        fake_db.query = AsyncMock(side_effect=RuntimeError("db down"))  # type: ignore[method-assign]

        with pytest.raises(SelfModelStoreError, match="Failed to upsert"):
            await store.upsert_fact(sample_person_fact)


class TestSelfModelStoreGet:
    """Test single fact retrieval."""

    @pytest.mark.asyncio
    async def test_get_existing_fact(
        self, store: SelfModelStore, fake_db: FakeDB, sample_person_fact: SelfFact
    ) -> None:
        fake_db.set_next_results(
            [{"result": [sample_person_fact.model_dump(mode="json")], "status": "OK"}],
        )

        result = await store.get_fact(sample_person_fact.id)

        assert result is not None
        assert result.id == sample_person_fact.id

    @pytest.mark.asyncio
    async def test_get_nonexistent_fact_returns_none(
        self, store: SelfModelStore, fake_db: FakeDB
    ) -> None:
        fake_db.set_next_results([{"result": [], "status": "OK"}])

        result = await store.get_fact("01J000NONEXISTENT")

        assert result is None


class TestSelfModelStoreList:
    """Test fact listing with filters."""

    @pytest.mark.asyncio
    async def test_list_all_active(
        self,
        store: SelfModelStore,
        fake_db: FakeDB,
        sample_person_fact: SelfFact,
        sample_routine_fact: SelfFact,
    ) -> None:
        fake_db.set_next_results(
            [
                {
                    "result": [
                        sample_person_fact.model_dump(mode="json"),
                        sample_routine_fact.model_dump(mode="json"),
                    ],
                    "status": "OK",
                }
            ],
        )

        results = await store.list_facts()

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_list_filtered_by_type(
        self, store: SelfModelStore, fake_db: FakeDB, sample_person_fact: SelfFact
    ) -> None:
        fake_db.set_next_results(
            [{"result": [sample_person_fact.model_dump(mode="json")], "status": "OK"}],
        )

        results = await store.list_facts(entity_type="person")

        assert len(results) == 1
        assert results[0].entity_type == "person"

    @pytest.mark.asyncio
    async def test_list_empty(self, store: SelfModelStore, fake_db: FakeDB) -> None:
        fake_db.set_next_results([{"result": [], "status": "OK"}])

        results = await store.list_facts(entity_type="goal")

        assert len(results) == 0


class TestSelfModelStoreDeactivate:
    """Test fact deactivation (soft-delete)."""

    @pytest.mark.asyncio
    async def test_deactivate_fact(self, store: SelfModelStore, fake_db: FakeDB) -> None:
        fake_db.set_next_results([{"result": [], "status": "OK"}])

        await store.deactivate_fact("01J000TEST", reason="user_requested")
        # No exception = success


class TestExtractRows:
    """Test the SurrealDB result normalization helper."""

    def test_standard_result_format(self) -> None:
        result = [{"result": [{"id": "a"}, {"id": "b"}], "status": "OK"}]

        rows = _extract_rows(result)

        assert len(rows) == 2
        assert rows[0]["id"] == "a"

    def test_nested_list_format(self) -> None:
        result = [[{"id": "x"}]]

        rows = _extract_rows(result)

        assert len(rows) == 1

    def test_empty_result(self) -> None:
        result = [{"result": [], "status": "OK"}]

        rows = _extract_rows(result)

        assert rows == []

    def test_non_list_returns_empty(self) -> None:
        rows = _extract_rows("unexpected")

        assert rows == []

    def test_direct_dict_list(self) -> None:
        result = [{"id": "direct1"}, {"id": "direct2"}]

        rows = _extract_rows(result)

        assert len(rows) == 2
