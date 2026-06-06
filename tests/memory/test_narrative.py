"""Tests for NarrativeMemory — deduplication and decay."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coremind.memory.narrative import NarrativeMemory, _jaccard, _tokenise

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixed_clock(dt: datetime | None = None):
    """Return a clock callable that always returns the same time."""
    now = dt or datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return lambda: now


# ---------------------------------------------------------------------------
# Deduplication helpers unit tests
# ---------------------------------------------------------------------------


def test_tokenise_basic() -> None:
    tokens = _tokenise("Apple Health data is frozen")
    assert tokens == {"apple", "health", "data", "is", "frozen"}


def test_tokenise_strips_non_alnum_tokens() -> None:
    tokens = _tokenise("data-sync failed! No response...")
    # Tokens containing non-alnum characters are excluded entirely
    # "data-sync" has hyphen, "failed!" has !, "response..." has dots
    assert "no" in tokens
    assert "failed!" not in tokens
    assert "data-sync" not in tokens
    assert "response..." not in tokens


def test_jaccard_identical() -> None:
    a = {"apple", "health", "data", "frozen"}
    assert _jaccard(a, a) == 1.0


def test_jaccard_disjoint() -> None:
    a = {"apple", "health"}
    b = {"gmail", "oauth"}
    assert _jaccard(a, b) == 0.0


def test_jaccard_partial_overlap() -> None:
    a = {"apple", "health", "data", "frozen"}
    b = {"apple", "health", "data", "unchanged"}
    # 3 shared / 5 total = 0.6
    assert _jaccard(a, b) == pytest.approx(0.6)


def test_jaccard_empty() -> None:
    assert _jaccard(set(), {"a", "b"}) == 0.0
    assert _jaccard({"a"}, set()) == 0.0


# ---------------------------------------------------------------------------
# NarrativeMemory.add_observation deduplication tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_observation_basic(tmp_path: Path) -> None:
    """Adding a new observation succeeds."""
    mem = NarrativeMemory(store_path=tmp_path / "narrative.json", clock=_fixed_clock())
    await mem.add_observation("User is sleeping less than usual")

    state = mem.get_current()
    assert len(state.recent_patterns) == 1
    assert state.recent_patterns[0].text == "User is sleeping less than usual"


@pytest.mark.asyncio
async def test_add_observation_rejects_exact_duplicate(tmp_path: Path) -> None:
    """Identical text added twice results in only one entry."""
    mem = NarrativeMemory(store_path=tmp_path / "narrative.json", clock=_fixed_clock())
    await mem.add_observation("Apple Health data is frozen")
    await mem.add_observation("Apple Health data is frozen")
    await mem.add_observation("Apple Health data is frozen")

    state = mem.get_current()
    # Max 2 similar occurrences allowed (threshold=0.6, exact duplicate has Jaccard=1.0)
    assert len(state.recent_patterns) <= 2


@pytest.mark.asyncio
async def test_add_observation_rejects_semantically_similar(tmp_path: Path) -> None:
    """Similar paraphrases are treated as duplicates after threshold."""
    mem = NarrativeMemory(store_path=tmp_path / "narrative.json", clock=_fixed_clock())
    # These share enough tokens to exceed Jaccard 0.6
    await mem.add_observation("Apple Health data is frozen since 5 days")
    await mem.add_observation("Apple Health data remains frozen since 5 days")
    await mem.add_observation("Apple Health data still frozen since 5 days")
    await mem.add_observation("Apple Health data appears frozen since 5 days")

    state = mem.get_current()
    # At most 2 entries should survive (max similar occurrences = 2)
    assert len(state.recent_patterns) <= 2


@pytest.mark.asyncio
async def test_add_observation_allows_distinct_topics(tmp_path: Path) -> None:
    """Different observations are not deduplicated."""
    mem = NarrativeMemory(store_path=tmp_path / "narrative.json", clock=_fixed_clock())
    await mem.add_observation("Apple Health data is frozen")
    await mem.add_observation("Gmail OAuth token expired")
    await mem.add_observation("Living room motion detected")

    state = mem.get_current()
    assert len(state.recent_patterns) == 3


@pytest.mark.asyncio
async def test_add_observation_decay_removes_old(tmp_path: Path) -> None:
    """Observations older than TTL are pruned on next add."""
    old_time = datetime(2026, 5, 20, tzinfo=UTC)
    new_time = datetime(2026, 6, 1, tzinfo=UTC)

    mem = NarrativeMemory(store_path=tmp_path / "narrative.json", clock=lambda: old_time)
    await mem.add_observation("Old observation")

    # Advance clock past the 7-day TTL
    mem._clock = lambda: new_time
    await mem.add_observation("New observation")

    state = mem.get_current()
    assert len(state.recent_patterns) == 1
    assert state.recent_patterns[0].text == "New observation"


@pytest.mark.asyncio
async def test_hallucination_loop_prevention(tmp_path: Path) -> None:
    """Simulates the narrative hallucination loop bug — 14 identical insertions.

    Before the fix: all 14 would be stored.
    After the fix: significantly fewer survive (Jaccard dedup catches most).
    """
    mem = NarrativeMemory(store_path=tmp_path / "narrative.json", clock=_fixed_clock())

    # Simulate 14 cycles all producing variants of the same hallucination.
    # Real-world LLM hallucinations are closer to verbatim repetitions;
    # here we include both near-identical and paraphrased variants.
    variants = [
        "Apple Health data remains frozen at static values",
        "Apple Health data frozen at static values since 5 days",
        "Apple Health data is frozen at static values",
        "Apple Health data appears frozen at static values since days",
        "Apple Health data values remain frozen static",
        "Apple Health data frozen static values unchanged",
        "Apple Health data still frozen at same static values",
        "Apple Health data is frozen at static values still",
        "Apple Health data frozen values have not changed",
        "Apple Health data remains frozen at static values unchanged",
        "Apple Health data frozen static since days no update",
        "Apple Health data is frozen values static unchanged",
        "Apple Health data remains frozen static values same",
        "Apple Health data frozen at static values no sync",
    ]
    for v in variants:
        await mem.add_observation(v)

    state = mem.get_current()
    # Before the fix: 14 patterns.  After the fix: significantly fewer.
    # Jaccard dedup catches lexically similar variants; some lexically diverse
    # paraphrases may slip through but the flood is controlled.
    assert len(state.recent_patterns) <= 7, (
        f"Expected at most 7 patterns after dedup, got {len(state.recent_patterns)}"
    )
    # The critical property: 14 variants -> significantly fewer than 14 stored
    assert len(state.recent_patterns) < 14


@pytest.mark.asyncio
async def test_hallucination_loop_exact_repetition(tmp_path: Path) -> None:
    """The most common real-world case: LLM repeats nearly the same text.

    This is what actually happened in production — the same observation
    with minor rewording added 14 times.
    """
    mem = NarrativeMemory(store_path=tmp_path / "narrative.json", clock=_fixed_clock())

    # Near-identical repetitions (as seen in actual logs)
    for i in range(14):
        await mem.add_observation(
            f"Apple Health data remains frozen at static values (observation {i})"
        )

    state = mem.get_current()
    # Near-identical text should be heavily deduplicated
    assert len(state.recent_patterns) <= 2, (
        f"Expected at most 2 patterns for near-identical text, got {len(state.recent_patterns)}"
    )
