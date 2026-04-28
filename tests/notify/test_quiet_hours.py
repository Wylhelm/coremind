"""Quiet-hours filter tests."""

from __future__ import annotations

from datetime import UTC, datetime, time

from coremind.notify.quiet_hours import FocusWindow, QuietHoursFilter, QuietHoursPolicy


def _clock_at(h: int, m: int = 0) -> datetime:
    return datetime(2025, 1, 1, h, m, tzinfo=UTC)


def test_outside_quiet_hours_delivers() -> None:
    policy = QuietHoursPolicy(timezone="UTC", quiet_start=time(23, 0), quiet_end=time(7, 0))
    f = QuietHoursFilter(policy, clock=lambda: _clock_at(10))
    assert f.decide(category="info") == "deliver"
    assert f.decide(category="suggest") == "deliver"
    assert f.decide(category="ask") == "deliver"


def test_quiet_hours_defers_info_and_suggest() -> None:
    policy = QuietHoursPolicy(timezone="UTC", quiet_start=time(23, 0), quiet_end=time(7, 0))
    f = QuietHoursFilter(policy, clock=lambda: _clock_at(2))
    assert f.decide(category="info") == "defer"
    assert f.decide(category="suggest") == "defer"
    assert f.decide(category="ask") == "deliver_low_urgency"


def test_safety_overrides_quiet_hours() -> None:
    policy = QuietHoursPolicy(timezone="UTC", quiet_start=time(23, 0), quiet_end=time(7, 0))
    f = QuietHoursFilter(policy, clock=lambda: _clock_at(2))
    assert f.decide(category="info", action_class="safety.fire") == "deliver"
    assert f.decide(category="suggest", action_class="alarm.sos") == "deliver"


def test_focus_window_defers_non_ask() -> None:
    focus = FocusWindow(
        start=datetime(2025, 1, 1, 9, 0, tzinfo=UTC),
        end=datetime(2025, 1, 1, 11, 0, tzinfo=UTC),
    )
    policy = QuietHoursPolicy(
        timezone="UTC",
        quiet_start=time(23, 0),
        quiet_end=time(7, 0),
        focus_windows=[focus],
    )
    f = QuietHoursFilter(policy, clock=lambda: _clock_at(10))
    assert f.decide(category="info") == "defer"
    assert f.decide(category="ask") == "deliver_low_urgency"


def test_next_active_returns_end_of_window() -> None:
    policy = QuietHoursPolicy(timezone="UTC", quiet_start=time(23, 0), quiet_end=time(7, 0))
    f = QuietHoursFilter(policy, clock=lambda: _clock_at(2))
    nxt = f.next_active()
    assert nxt.hour == 7
    assert nxt.minute == 0


def test_next_active_outside_quiet_is_now() -> None:
    now = _clock_at(10)
    policy = QuietHoursPolicy(timezone="UTC", quiet_start=time(23, 0), quiet_end=time(7, 0))
    f = QuietHoursFilter(policy, clock=lambda: now)
    assert f.next_active() == now
