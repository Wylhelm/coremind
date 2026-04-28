"""IntentStore JSONL persistence tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import Intent, InternalQuestion


def _make(id_: str = "i1", status: str = "pending", at: datetime | None = None) -> Intent:
    return Intent(
        id=id_,
        created_at=at or datetime(2025, 1, 1, tzinfo=UTC),
        question=InternalQuestion(id="q", text="?"),
        salience=0.5,
        confidence=0.5,
        category="ask",
        status=status,  # type: ignore[arg-type]
    )


async def test_save_and_get(tmp_path: Path) -> None:
    s = IntentStore(tmp_path / "intents.jsonl")
    intent = _make()
    await s.save(intent)
    loaded = await s.get("i1")
    assert loaded is not None
    assert loaded.id == "i1"


async def test_save_is_idempotent_by_id(tmp_path: Path) -> None:
    s = IntentStore(tmp_path / "intents.jsonl")
    await s.save(_make(status="pending"))
    await s.save(_make(status="done"))
    loaded = await s.get("i1")
    assert loaded is not None
    assert loaded.status == "done"


async def test_list_filters_by_status(tmp_path: Path) -> None:
    s = IntentStore(tmp_path / "intents.jsonl")
    await s.save(_make(id_="a", status="pending"))
    await s.save(_make(id_="b", status="done"))
    items = await s.list(status="pending")
    assert {i.id for i in items} == {"a"}
