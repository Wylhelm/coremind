"""Tests for coremind_plugin_systemstats.collector."""

from __future__ import annotations

import pytest
from coremind_plugin_systemstats.collector import (
    collect_cpu_percent,
    collect_memory_percent,
    collect_uptime_seconds,
)

# ---------------------------------------------------------------------------
# collect_cpu_percent
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_collect_cpu_percent_returns_float_in_range() -> None:
    """cpu_percent() returns a float within [0.0, 100.0]."""
    result = collect_cpu_percent(interval=0.1)

    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0


@pytest.mark.slow
def test_collect_cpu_percent_with_short_interval_does_not_raise() -> None:
    """cpu_percent() does not raise for any positive interval."""
    result = collect_cpu_percent(interval=0.05)

    assert isinstance(result, float)


# ---------------------------------------------------------------------------
# collect_memory_percent
# ---------------------------------------------------------------------------


def test_collect_memory_percent_returns_float_in_range() -> None:
    """memory_percent() returns a float within [0.0, 100.0]."""
    result = collect_memory_percent()

    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0


def test_collect_memory_percent_reflects_nonzero_usage() -> None:
    """memory_percent() returns a value above zero (any real host has some usage)."""
    result = collect_memory_percent()

    assert result > 0.0


# ---------------------------------------------------------------------------
# collect_uptime_seconds
# ---------------------------------------------------------------------------


def test_collect_uptime_seconds_returns_positive_int() -> None:
    """uptime_seconds() returns a non-negative integer."""
    result = collect_uptime_seconds()

    assert isinstance(result, int)
    assert result >= 0


def test_collect_uptime_seconds_is_consistent() -> None:
    """Two calls to uptime_seconds() return values within 2 seconds of each other."""
    first = collect_uptime_seconds()
    second = collect_uptime_seconds()

    assert abs(second - first) <= 2
