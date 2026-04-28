"""Tests for coremind.memory.procedural.

All tests are unit tests (no I/O beyond tmp_path).  A deterministic clock is
injected so tests never depend on wall-clock time.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coremind.errors import ProceduralMemoryError
from coremind.memory.procedural import (
    _GENESIS_HASH,
    ProceduralMemory,
    Rule,
    _apply_op,
    _compute_entry_hash,
    _EntryContent,
    _evaluate_trigger,
)
from coremind.world.model import JsonValue

# ---------------------------------------------------------------------------
# Fixed clock
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
_TICK = timedelta(seconds=1)


class _TickingClock:
    """Deterministic clock that advances by _TICK on each call."""

    def __init__(self, start: datetime = _FIXED_NOW) -> None:
        self._current = start
        self.calls: int = 0

    def __call__(self) -> datetime:
        result = self._current
        self._current += _TICK
        self.calls += 1
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule(
    *,
    rule_id: str = "rule_1",
    source: str = "human",
    confidence: float = 0.8,
    trigger: dict[str, JsonValue] | None = None,
    action: dict[str, JsonValue] | None = None,
) -> Rule:
    """Build a minimal Rule for use in tests."""
    return Rule(
        id=rule_id,
        created_at=_FIXED_NOW,
        description=f"Rule {rule_id}",
        trigger=trigger or {"conditions": []},
        action=action or {"type": "notify", "message": "test"},
        confidence=confidence,
        applied_count=0,
        success_rate=0.0,
        source=source,  # type: ignore[arg-type]
    )


def _make_memory(tmp_path: Path) -> tuple[ProceduralMemory, _TickingClock]:
    clock = _TickingClock()
    mem = ProceduralMemory(tmp_path / "procedural.jsonl", clock=clock)
    return mem, clock


# ---------------------------------------------------------------------------
# _compute_entry_hash
# ---------------------------------------------------------------------------


def test_compute_entry_hash_is_deterministic() -> None:
    content = _EntryContent(
        seq=1,
        timestamp=_FIXED_NOW,
        prev_hash=_GENESIS_HASH,
        op="add",
        payload={"id": "r1"},
    )
    h1 = _compute_entry_hash(content)
    h2 = _compute_entry_hash(content)

    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex
    assert h1 != _GENESIS_HASH


def test_compute_entry_hash_changes_with_payload() -> None:
    base = _EntryContent(
        seq=1,
        timestamp=_FIXED_NOW,
        prev_hash=_GENESIS_HASH,
        op="add",
        payload={"id": "r1"},
    )
    modified = _EntryContent(
        seq=1,
        timestamp=_FIXED_NOW,
        prev_hash=_GENESIS_HASH,
        op="add",
        payload={"id": "r2"},
    )

    assert _compute_entry_hash(base) != _compute_entry_hash(modified)


# ---------------------------------------------------------------------------
# _apply_op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "ctx_val", "cmp_val", "expected"),
    [
        ("eq", "foo", "foo", True),
        ("eq", "foo", "bar", False),
        ("neq", "foo", "bar", True),
        ("neq", "foo", "foo", False),
        ("gt", 26.0, 25.0, True),
        ("gt", 24.0, 25.0, False),
        ("gte", 25.0, 25.0, True),
        ("gte", 24.0, 25.0, False),
        ("lt", 24.0, 25.0, True),
        ("lt", 26.0, 25.0, False),
        ("lte", 25.0, 25.0, True),
        ("lte", 26.0, 25.0, False),
    ],
)
def test_apply_op_numeric_and_equality(
    op: str, ctx_val: object, cmp_val: object, expected: bool
) -> None:
    context = {"val": ctx_val}
    assert _apply_op(context, "val", op, cmp_val) is expected  # type: ignore[arg-type]


def test_apply_op_exists_true() -> None:
    assert _apply_op({"x": 1}, "x", "exists", None) is True


def test_apply_op_exists_false() -> None:
    assert _apply_op({"x": 1}, "y", "exists", None) is False


def test_apply_op_contains_list() -> None:
    assert _apply_op({"tags": ["a", "b"]}, "tags", "contains", "a") is True
    assert _apply_op({"tags": ["a", "b"]}, "tags", "contains", "c") is False


def test_apply_op_contains_string() -> None:
    assert _apply_op({"msg": "hello world"}, "msg", "contains", "world") is True
    assert _apply_op({"msg": "hello world"}, "msg", "contains", "xyz") is False


def test_apply_op_non_numeric_returns_false() -> None:
    assert _apply_op({"v": "not_a_number"}, "v", "gt", 10.0) is False


def test_apply_op_unknown_op_returns_false() -> None:
    assert _apply_op({"v": 1}, "v", "between", 0) is False


# ---------------------------------------------------------------------------
# _evaluate_trigger
# ---------------------------------------------------------------------------


def test_evaluate_trigger_empty_conditions_always_matches() -> None:
    assert _evaluate_trigger({"conditions": []}, {"x": 1}) is True


def test_evaluate_trigger_missing_conditions_always_matches() -> None:
    assert _evaluate_trigger({}, {"x": 1}) is True


def test_evaluate_trigger_all_logic_requires_all() -> None:
    trigger = {
        "conditions": [
            {"field": "x", "op": "eq", "value": 1},
            {"field": "y", "op": "eq", "value": 2},
        ],
        "logic": "all",
    }
    assert _evaluate_trigger(trigger, {"x": 1, "y": 2}) is True  # type: ignore[arg-type]
    assert _evaluate_trigger(trigger, {"x": 1, "y": 9}) is False  # type: ignore[arg-type]


def test_evaluate_trigger_any_logic_requires_one() -> None:
    trigger = {
        "conditions": [
            {"field": "x", "op": "eq", "value": 1},
            {"field": "y", "op": "eq", "value": 2},
        ],
        "logic": "any",
    }
    assert _evaluate_trigger(trigger, {"x": 1, "y": 9}) is True  # type: ignore[arg-type]
    assert _evaluate_trigger(trigger, {"x": 9, "y": 9}) is False  # type: ignore[arg-type]


def test_evaluate_trigger_invalid_condition_type_returns_false() -> None:
    trigger = {"conditions": ["not_a_dict"], "logic": "all"}
    assert _evaluate_trigger(trigger, {}) is False  # type: ignore[arg-type]


def test_evaluate_trigger_missing_field_returns_false() -> None:
    trigger = {"conditions": [{"op": "eq", "value": 1}], "logic": "all"}
    assert _evaluate_trigger(trigger, {}) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ProceduralMemory.add
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_creates_journal_file(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    rule = _make_rule()

    await mem.add(rule)

    journal = tmp_path / "procedural.jsonl"
    assert journal.exists()
    assert journal.stat().st_size > 0


@pytest.mark.asyncio
async def test_add_makes_rule_queryable(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    rule = _make_rule(trigger={"conditions": []})

    await mem.add(rule)

    matched = await mem.match({})
    assert len(matched) == 1
    assert matched[0].id == rule.id


@pytest.mark.asyncio
async def test_add_duplicate_id_raises(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="dup"))

    with pytest.raises(ProceduralMemoryError, match="already exists"):
        await mem.add(_make_rule(rule_id="dup"))


@pytest.mark.asyncio
async def test_add_multiple_rules(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))
    await mem.add(_make_rule(rule_id="r2"))

    matched = await mem.match({})
    ids = {r.id for r in matched}
    assert ids == {"r1", "r2"}


@pytest.mark.asyncio
async def test_add_concurrent_duplicate_raises_for_exactly_one(tmp_path: Path) -> None:
    """The asyncio.Lock must ensure exactly one concurrent add of the same id wins."""
    mem, _ = _make_memory(tmp_path)
    r1 = _make_rule(rule_id="concurrent")
    r2 = _make_rule(rule_id="concurrent")

    results = await asyncio.gather(mem.add(r1), mem.add(r2), return_exceptions=True)

    errors = [r for r in results if isinstance(r, ProceduralMemoryError)]
    assert len(errors) == 1
    matched = await mem.match({})
    assert len([r for r in matched if r.id == "concurrent"]) == 1


# ---------------------------------------------------------------------------
# ProceduralMemory.match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_filters_by_trigger(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    matching_rule = _make_rule(
        rule_id="hot",
        trigger={
            "conditions": [{"field": "temp", "op": "gt", "value": 25.0}],
            "logic": "all",
        },
    )
    non_matching_rule = _make_rule(
        rule_id="cold",
        trigger={
            "conditions": [{"field": "temp", "op": "lt", "value": 10.0}],
            "logic": "all",
        },
    )
    await mem.add(matching_rule)
    await mem.add(non_matching_rule)

    result = await mem.match({"temp": 30.0})

    assert len(result) == 1
    assert result[0].id == "hot"


@pytest.mark.asyncio
async def test_match_sorts_by_confidence_descending(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="low", confidence=0.3))
    await mem.add(_make_rule(rule_id="high", confidence=0.9))
    await mem.add(_make_rule(rule_id="mid", confidence=0.6))

    result = await mem.match({})

    assert [r.id for r in result] == ["high", "mid", "low"]


@pytest.mark.asyncio
async def test_match_excludes_deprecated(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="active"))
    await mem.add(_make_rule(rule_id="gone"))
    await mem.deprecate("gone", reason="obsolete")

    result = await mem.match({})

    assert all(r.id != "gone" for r in result)


# ---------------------------------------------------------------------------
# ProceduralMemory.reinforce
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinforce_success_updates_success_rate(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1", confidence=0.5))

    await mem.reinforce("r1", True)

    matched = await mem.match({})
    rule = next(r for r in matched if r.id == "r1")
    assert rule.applied_count == 1
    assert rule.success_rate == pytest.approx(1.0)
    # confidence is the creator's prior and must not be overwritten by reinforce
    assert rule.confidence == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_reinforce_failure_decreases_confidence(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1", confidence=0.9))

    await mem.reinforce("r1", False)

    matched = await mem.match({})
    rule = next(r for r in matched if r.id == "r1")
    assert rule.applied_count == 1
    assert rule.success_rate == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_reinforce_running_mean_across_multiple_calls(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1", confidence=0.5))

    await mem.reinforce("r1", True)  # 1/1 = 1.0
    await mem.reinforce("r1", False)  # 1/2 = 0.5
    await mem.reinforce("r1", True)  # 2/3 ≈ 0.667

    matched = await mem.match({})
    rule = next(r for r in matched if r.id == "r1")
    assert rule.applied_count == 3
    assert rule.success_rate == pytest.approx(2 / 3, rel=1e-4)


@pytest.mark.asyncio
async def test_reinforce_unknown_rule_raises(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)

    with pytest.raises(ProceduralMemoryError, match="No active rule"):
        await mem.reinforce("nonexistent", True)


@pytest.mark.asyncio
async def test_reinforce_deprecated_rule_raises(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))
    await mem.deprecate("r1", reason="gone")

    with pytest.raises(ProceduralMemoryError, match="No active rule"):
        await mem.reinforce("r1", True)


# ---------------------------------------------------------------------------
# ProceduralMemory.deprecate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deprecate_removes_rule_from_match(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))

    await mem.deprecate("r1", reason="superseded")

    result = await mem.match({})
    assert all(r.id != "r1" for r in result)


@pytest.mark.asyncio
async def test_deprecate_unknown_rule_raises(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)

    with pytest.raises(ProceduralMemoryError, match="No rule"):
        await mem.deprecate("nonexistent", reason="test")


@pytest.mark.asyncio
async def test_deprecate_already_deprecated_raises(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))
    await mem.deprecate("r1", reason="first")

    with pytest.raises(ProceduralMemoryError, match="already deprecated"):
        await mem.deprecate("r1", reason="second")


# ---------------------------------------------------------------------------
# ProceduralMemory.load — journal replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_replays_add(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))

    mem2, _ = _make_memory(tmp_path)
    await mem2.load()

    result = await mem2.match({})
    assert len(result) == 1
    assert result[0].id == "r1"


@pytest.mark.asyncio
async def test_load_replays_reinforce(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))
    await mem.reinforce("r1", True)

    mem2, _ = _make_memory(tmp_path)
    await mem2.load()

    result = await mem2.match({})
    assert result[0].applied_count == 1
    assert result[0].success_rate == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_load_replays_deprecate(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))
    await mem.deprecate("r1", reason="test")

    mem2, _ = _make_memory(tmp_path)
    await mem2.load()

    result = await mem2.match({})
    assert result == []


@pytest.mark.asyncio
async def test_load_on_missing_file_is_noop(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.load()

    result = await mem.match({})
    assert result == []


@pytest.mark.asyncio
async def test_load_is_idempotent(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))

    mem2, _ = _make_memory(tmp_path)
    await mem2.load()
    await mem2.load()  # second load resets and replays

    result = await mem2.match({})
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Hash-chain integrity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_detects_tampered_entry(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))

    journal = tmp_path / "procedural.jsonl"
    lines = journal.read_text().splitlines()
    # Replace the entry_hash with garbage to break the chain.
    tampered = lines[0].replace(lines[0].split('"entry_hash":"')[1][:10], "0000000000")
    journal.write_text(tampered + "\n")

    mem2, _ = _make_memory(tmp_path)
    with pytest.raises(ProceduralMemoryError):
        await mem2.load()


@pytest.mark.asyncio
async def test_load_detects_sequence_gap(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))
    await mem.add(_make_rule(rule_id="r2"))

    journal = tmp_path / "procedural.jsonl"
    lines = journal.read_text().splitlines()
    # Drop the first entry — seq 2 will not follow seq 0.
    journal.write_text(lines[1] + "\n")

    mem2, _ = _make_memory(tmp_path)
    with pytest.raises(ProceduralMemoryError, match="Sequence gap"):
        await mem2.load()


@pytest.mark.asyncio
async def test_load_detects_broken_prev_hash(tmp_path: Path) -> None:
    mem, _ = _make_memory(tmp_path)
    await mem.add(_make_rule(rule_id="r1"))
    await mem.add(_make_rule(rule_id="r2"))

    journal = tmp_path / "procedural.jsonl"
    lines = journal.read_text().splitlines()
    # Replace prev_hash in the second line with zeros.
    entry2 = json.loads(lines[1])
    entry2["prev_hash"] = "0" * 64
    lines[1] = json.dumps(entry2)
    journal.write_text("\n".join(lines) + "\n")

    mem2, _ = _make_memory(tmp_path)
    with pytest.raises(ProceduralMemoryError, match="Hash chain broken"):
        await mem2.load()


@pytest.mark.asyncio
async def test_load_detects_corrupt_json(tmp_path: Path) -> None:
    journal = tmp_path / "procedural.jsonl"
    journal.write_text("not valid json\n")

    mem, _ = _make_memory(tmp_path)
    with pytest.raises(ProceduralMemoryError, match="parse error"):
        await mem.load()


# ---------------------------------------------------------------------------
# Trigger / action round-trip with nested dicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_dict_trigger_and_action_round_trip(tmp_path: Path) -> None:
    """Rules with deeply nested dict payloads must survive a journal round-trip."""
    mem, _ = _make_memory(tmp_path)
    rule = _make_rule(
        rule_id="nested",
        trigger={
            "conditions": [
                {"field": "channel", "op": "eq", "value": "slack"},
            ],
            "logic": "all",
            "metadata": {"source": "integration", "priority": 1},
        },
        action={
            "type": "notify",
            "payload": {
                "channel": "slack",
                "message": {"text": "alert", "blocks": [{"type": "section"}]},
            },
        },
    )
    await mem.add(rule)

    mem2, _ = _make_memory(tmp_path)
    await mem2.load()

    result = await mem2.match({"channel": "slack"})
    assert len(result) == 1
    assert result[0].trigger == rule.trigger
    assert result[0].action == rule.action
