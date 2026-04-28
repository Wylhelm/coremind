"""Tests for the reasoning loop and cycle persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coremind.errors import ReasoningError
from coremind.reasoning.llm import LLM, CompletionResult, LayerConfig, LLMConfig
from coremind.reasoning.loop import ReasoningLoop, ReasoningLoopConfig, entities_touched
from coremind.reasoning.persistence import JsonlCyclePersister
from coremind.reasoning.schemas import ReasoningOutput, TokenUsage
from coremind.world.model import (
    Entity,
    EntityRef,
    WorldEventRecord,
    WorldSnapshot,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSnapshotProvider:
    def __init__(self, snapshot: WorldSnapshot) -> None:
        self._snapshot = snapshot

    async def snapshot(self, at: datetime | None = None) -> WorldSnapshot:
        return self._snapshot


class _FakeMemory:
    """Minimal recall port that yields deterministic memory-like objects."""

    class _Mem:
        def __init__(self, mid: str, text: str) -> None:
            self.id = mid
            self.text = text

    async def recall(
        self,
        query: str,
        k: int = 10,
        tags: list[str] | None = None,
    ) -> list[object]:
        return [self._Mem(f"{query}-{i}", f"note about {query} #{i}") for i in range(k)]


class _ScriptedBackend:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        response_format: dict[str, str] | None,
        api_key: str | None,
    ) -> CompletionResult:
        return CompletionResult(
            content=json.dumps(self._payload),
            prompt_tokens=5,
            completion_tokens=10,
            total_tokens=15,
        )


def _sample_snapshot() -> WorldSnapshot:
    now = datetime.now(UTC)
    ref = EntityRef(type="host", id="laptop")
    return WorldSnapshot(
        taken_at=now,
        entities=[
            Entity(
                type="host",
                display_name="laptop",
                created_at=now,
                updated_at=now,
                properties={"cpu_percent": 42.0},
                source_plugins=["coremind.plugin.systemstats"],
            )
        ],
        recent_events=[
            WorldEventRecord(
                id="ev-1",
                timestamp=now,
                source="coremind.plugin.systemstats",
                source_version="0.1.0",
                signature=None,
                entity=ref,
                attribute="cpu_percent",
                value=42.0,
                confidence=0.95,
                unit=None,
            )
        ],
    )


def _valid_cycle_payload() -> dict[str, object]:
    """A cycle payload that will validate against ReasoningOutput."""
    return {
        "cycle_id": "placeholder",
        "timestamp": datetime.now(UTC).isoformat(),
        "model_used": "placeholder",
        "patterns": [],
        "anomalies": [],
        "predictions": [],
        "token_usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


# ---------------------------------------------------------------------------
# JsonlCyclePersister
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persister_round_trip(tmp_path: Path) -> None:
    persister = JsonlCyclePersister(tmp_path / "cycles.jsonl")
    cycle = ReasoningOutput(
        cycle_id="c1",
        timestamp=datetime.now(UTC),
        model_used="test/fake",
        token_usage=TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
    )
    await persister.persist_cycle(cycle)
    fetched = await persister.get_cycle("c1")
    assert fetched is not None
    assert fetched.cycle_id == "c1"


@pytest.mark.asyncio
async def test_persister_list_orders_descending(tmp_path: Path) -> None:
    persister = JsonlCyclePersister(tmp_path / "cycles.jsonl")
    for i in range(3):
        await persister.persist_cycle(
            ReasoningOutput(
                cycle_id=f"c{i}",
                timestamp=datetime(2026, 1, 1 + i, tzinfo=UTC),
                model_used="test/fake",
                token_usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            )
        )
    cycles = await persister.list_cycles(limit=10)
    ids = [c.cycle_id for c in cycles]
    assert ids == ["c2", "c1", "c0"]


# ---------------------------------------------------------------------------
# ReasoningLoop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cycle_happy_path(tmp_path: Path) -> None:
    backend = _ScriptedBackend(_valid_cycle_payload())
    llm = LLM(
        LLMConfig(reasoning_heavy=LayerConfig(model="test/fake")),
        backend=backend,
    )
    persister = JsonlCyclePersister(tmp_path / "cycles.jsonl")
    loop = ReasoningLoop(
        snapshot_provider=_FakeSnapshotProvider(_sample_snapshot()),
        memory=_FakeMemory(),
        llm=llm,
        persister=persister,
    )
    output = await loop.run_cycle()
    assert output.cycle_id  # replaced by the loop
    assert output.model_used == "test/fake"
    # Persisted
    listed = await persister.list_cycles(limit=5)
    assert len(listed) == 1
    assert listed[0].cycle_id == output.cycle_id


@pytest.mark.asyncio
async def test_run_cycle_wraps_llm_error(tmp_path: Path) -> None:
    class _FailBackend:
        async def complete(self, **_: object) -> CompletionResult:
            raise RuntimeError("network down")

    llm = LLM(
        LLMConfig(reasoning_heavy=LayerConfig(model="test/fake")),
        backend=_FailBackend(),  # type: ignore[arg-type]
    )
    loop = ReasoningLoop(
        snapshot_provider=_FakeSnapshotProvider(_sample_snapshot()),
        memory=None,
        llm=llm,
        persister=JsonlCyclePersister(tmp_path / "cycles.jsonl"),
    )
    with pytest.raises(ReasoningError):
        await loop.run_cycle()


def test_entities_touched() -> None:
    snap = _sample_snapshot()
    touched = entities_touched(snap, window=timedelta(hours=1))
    assert EntityRef(type="host", id="laptop") in touched


@pytest.mark.asyncio
async def test_run_cycle_without_memory(tmp_path: Path) -> None:
    backend = _ScriptedBackend(_valid_cycle_payload())
    llm = LLM(
        LLMConfig(reasoning_heavy=LayerConfig(model="test/fake")),
        backend=backend,
    )
    persister = JsonlCyclePersister(tmp_path / "cycles.jsonl")
    loop = ReasoningLoop(
        snapshot_provider=_FakeSnapshotProvider(_sample_snapshot()),
        memory=None,
        llm=llm,
        persister=persister,
        config=ReasoningLoopConfig(interval_seconds=10),
    )
    output = await loop.run_cycle()
    assert output is not None
