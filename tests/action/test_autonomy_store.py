"""Tests for the autonomy audit store."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coremind.action.autonomy_store import (
    AutonomyChangeRecord,
    SurrealAutonomyAuditStore,
)


class FakeSurrealDB:
    """Minimal fake SurrealDB client for testing."""

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.queries: list[tuple[str, dict[str, object]]] = []

    async def query(
        self, sql: str, params: dict[str, object] | None = None
    ) -> list[list[dict[str, object]]]:
        self.queries.append((sql, params or {}))
        if sql.startswith("CREATE"):
            self.rows.append(dict(params or {}))
            return [[]]
        # SELECT queries: filter if domain is specified
        domain = (params or {}).get("domain")
        limit = (params or {}).get("limit", 50)
        assert isinstance(limit, int)
        rows = self.rows
        if domain is not None:
            rows = [r for r in rows if r["domain"] == domain]
        return [rows[:limit]]


@pytest.fixture
def fake_db() -> FakeSurrealDB:
    return FakeSurrealDB()


@pytest.fixture
def store(fake_db: FakeSurrealDB) -> SurrealAutonomyAuditStore:
    return SurrealAutonomyAuditStore(fake_db)


def _record(
    domain: str = "lighting",
    old_slider: float = 0.5,
    new_slider: float = 0.7,
    reason: str = "graduation",
    changed_by: str = "system",
) -> AutonomyChangeRecord:
    return AutonomyChangeRecord(
        domain=domain,
        old_slider=old_slider,
        new_slider=new_slider,
        reason=reason,
        changed_by=changed_by,
        changed_at=datetime.now(UTC),
    )


class TestAutonomyChangeRecord:
    """Pydantic model validation tests."""

    def test_valid_record(self) -> None:
        record = _record()
        assert record.domain == "lighting"
        assert record.old_slider == 0.5
        assert record.new_slider == 0.7

    def test_slider_bounds(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal"):
            _record(old_slider=-0.1)

        with pytest.raises(ValueError, match="less than or equal"):
            _record(new_slider=1.1)

    def test_empty_domain_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1 character"):
            _record(domain="")


@pytest.mark.asyncio
class TestSurrealAutonomyAuditStore:
    """Tests for the SurrealDB-backed audit store."""

    async def test_record_change(
        self, store: SurrealAutonomyAuditStore, fake_db: FakeSurrealDB
    ) -> None:
        record = _record()
        await store.record_change(record)
        assert len(fake_db.queries) == 1
        sql, params = fake_db.queries[0]
        assert "CREATE autonomy_change" in sql
        assert params["domain"] == "lighting"
        assert params["old_slider"] == 0.5
        assert params["new_slider"] == 0.7

    async def test_get_history_all(
        self, store: SurrealAutonomyAuditStore, fake_db: FakeSurrealDB
    ) -> None:
        await store.record_change(_record(domain="lighting"))
        await store.record_change(_record(domain="finance"))
        results = await store.get_history()
        assert len(results) == 2

    async def test_get_history_by_domain(
        self, store: SurrealAutonomyAuditStore, fake_db: FakeSurrealDB
    ) -> None:
        await store.record_change(_record(domain="lighting"))
        await store.record_change(_record(domain="finance"))
        results = await store.get_history(domain="lighting")
        assert len(results) == 1
        assert results[0].domain == "lighting"

    async def test_get_history_empty(self, store: SurrealAutonomyAuditStore) -> None:
        results = await store.get_history()
        assert results == []
