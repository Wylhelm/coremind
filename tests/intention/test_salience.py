"""Salience & confidence scoring tests."""

from __future__ import annotations

from datetime import UTC, datetime

from coremind.intention.salience import (
    categorize,
    score_confidence,
    score_salience,
)
from coremind.intention.schemas import (
    Intent,
    InternalQuestion,
    RawIntent,
)
from coremind.world.model import EntityRef, WorldEventRecord, WorldSnapshot


def _empty_snapshot() -> WorldSnapshot:
    return WorldSnapshot(
        taken_at=datetime(2025, 1, 1, tzinfo=UTC),
        entities=[],
        recent_events=[],
    )


def _raw(text: str = "Is the light on?", conf: float = 0.7) -> RawIntent:
    return RawIntent(
        question=InternalQuestion(id="q", text=text, grounding=[]),
        proposed_action=None,
        model_confidence=conf,
        model_salience=0.5,
    )


def test_categorize_thresholds() -> None:
    assert categorize(0.95, None) == "safe"
    assert categorize(0.70, None) == "suggest"
    assert categorize(0.30, None) == "ask"


def test_score_confidence_blends_signals() -> None:
    low = score_confidence(_raw(conf=0.3), matching_rules=0)
    high = score_confidence(_raw(conf=0.9), matching_rules=5)
    assert low < high
    assert 0.0 <= low <= 1.0
    assert 0.0 <= high <= 1.0


def test_score_salience_penalises_duplicates() -> None:
    snap = _empty_snapshot()
    raw = _raw(text="Should I dim the kitchen light tonight")
    prior = Intent(
        id="x",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        question=InternalQuestion(id="q0", text="Should I dim the kitchen light tonight"),
        salience=0.5,
        confidence=0.5,
        category="ask",
    )
    novel = score_salience(raw, snap, [])
    dup = score_salience(raw, snap, [prior])
    assert dup < novel


def test_score_salience_urgency_from_recent_events() -> None:
    ref = EntityRef(type="light", id="kitchen")
    event = WorldEventRecord(
        id="e1",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        source="test",
        source_version="1",
        signature=None,
        entity=ref,
        attribute="state",
        value="on",
        confidence=1.0,
    )
    snap = WorldSnapshot(
        taken_at=datetime(2025, 1, 1, tzinfo=UTC),
        entities=[],
        recent_events=[event],
    )
    raw = RawIntent(
        question=InternalQuestion(id="q", text="Kitchen light changed recently?", grounding=[ref]),
        proposed_action=None,
        model_confidence=0.5,
        model_salience=0.5,
    )
    score = score_salience(raw, snap, [])
    assert score > 0.0
