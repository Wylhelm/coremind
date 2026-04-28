"""Quiet-hours and focus-window policy for notifications.

The policy sits BETWEEN the approval gate / executor and the notification
port(s).  It never bypasses or executes anything; it only defers delivery of
lower-priority notifications that arrive during declared quiet windows.

Rules (per `ARCHITECTURE.md §15.6`):

- Quiet hours (default 23:00 → 07:00 local time):
    * ``info`` and ``suggest`` are deferred to the next active window.
    * ``ask`` for non-urgent domains is delivered with lowered urgency.
    * ``ask`` for safety/security domains is delivered immediately.
- Focus windows (explicit ranges):
    * Non-``ask`` notifications are suppressed.
    * ``ask`` is delivered without sound/vibration hints (implementation-defined).

The policy is deliberately simple: all decisions are time-of-day based; calendar
presence hints inform urgency scoring upstream but never override the schedule.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from coremind.notify.port import NotificationCategory

type Clock = Callable[[], datetime]
type Decision = Literal["deliver", "defer", "deliver_low_urgency"]

# Action-class prefixes considered "safety/security" — always delivered,
# even inside quiet hours or focus windows.
_SAFETY_CLASSES: tuple[str, ...] = (
    "safety.",
    "security.",
    "alarm.",
    "health.critical",
    "coremind.safety",
    "coremind.forced_class",
)


class FocusWindow(BaseModel):
    """A user-declared focus window."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime


class QuietHoursPolicy(BaseModel):
    """Static configuration of the quiet-hours filter.

    Attributes:
        timezone: IANA timezone for the local clock (e.g. ``"America/Montreal"``).
        quiet_start: Local time of day when quiet hours begin.
        quiet_end: Local time of day when quiet hours end.  A window that
            crosses midnight is supported (e.g. 23:00 → 07:00).
        focus_windows: Explicit focus-window ranges.  During a focus window,
            non-``ask`` notifications are suppressed.
    """

    model_config = ConfigDict(frozen=True)

    timezone: str = "UTC"
    quiet_start: time = Field(default=time(23, 0))
    quiet_end: time = Field(default=time(7, 0))
    focus_windows: list[FocusWindow] = Field(default_factory=list)


def _in_quiet_hours(now_local: datetime, start: time, end: time) -> bool:
    """Return ``True`` if *now_local* falls inside the quiet window.

    Handles windows that cross midnight (``start > end``).
    """
    current = now_local.time()
    if start == end:
        return False
    if start < end:
        return start <= current < end
    # wrap-around: e.g. 23:00 → 07:00
    return current >= start or current < end


def _in_focus_window(now: datetime, windows: list[FocusWindow]) -> bool:
    """Return ``True`` if *now* lies in any focus window."""
    return any(w.start <= now < w.end for w in windows)


def _is_safety_class(action_class: str | None) -> bool:
    """Return ``True`` when ``action_class`` is a safety/security class."""
    if not action_class:
        return False
    return any(
        action_class == c or action_class.startswith(c if c.endswith(".") else c + ".")
        for c in _SAFETY_CLASSES
    )


class QuietHoursFilter:
    """Stateful quiet-hours filter.

    Args:
        policy: The quiet-hours policy.
        clock: Injectable UTC clock.
    """

    def __init__(
        self,
        policy: QuietHoursPolicy,
        *,
        clock: Clock = lambda: datetime.now(tz=ZoneInfo("UTC")),
    ) -> None:
        self._policy = policy
        self._clock = clock

    @property
    def policy(self) -> QuietHoursPolicy:
        """Return the configured policy."""
        return self._policy

    def _tzinfo(self) -> tzinfo:
        """Return the configured timezone."""
        return ZoneInfo(self._policy.timezone)

    def decide(
        self,
        *,
        category: NotificationCategory,
        action_class: str | None = None,
    ) -> Decision:
        """Return a policy decision for a notification about to be sent.

        Args:
            category: The notification category (``info``/``suggest``/``ask``).
            action_class: Class of the originating action, if any, used to
                identify safety/security exemptions.

        Returns:
            - ``"deliver"``: send now.
            - ``"deliver_low_urgency"``: send now but strip urgency hints
              (port-dependent).
            - ``"defer"``: do not send now; caller should enqueue for the
              next active window.
        """
        now_utc = self._clock()
        now_local = now_utc.astimezone(self._tzinfo())

        in_quiet = _in_quiet_hours(now_local, self._policy.quiet_start, self._policy.quiet_end)
        in_focus = _in_focus_window(now_utc, list(self._policy.focus_windows))
        safety = _is_safety_class(action_class)

        if safety:
            return "deliver"

        if in_focus:
            # Focus: non-ask is suppressed; ask is delivered low-urgency.
            if category == "ask":
                return "deliver_low_urgency"
            return "defer"

        if in_quiet:
            if category == "ask":
                return "deliver_low_urgency"
            return "defer"

        return "deliver"

    def next_active(self, now: datetime | None = None) -> datetime:
        """Return the next wall-clock time at which deferred items may be sent.

        If *now* is not provided, uses the configured clock.
        """
        current = now or self._clock()
        local = current.astimezone(self._tzinfo())
        start = self._policy.quiet_start
        end = self._policy.quiet_end
        if not _in_quiet_hours(local, start, end):
            return current
        today_end = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if today_end <= local:
            today_end += timedelta(days=1)
        return today_end.astimezone(current.tzinfo or ZoneInfo("UTC"))
