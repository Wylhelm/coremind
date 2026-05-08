"""Action class category overrides.

Hardcoded routing policies that override LLM-assigned categories
to ensure safe operations never require approval and destructive
operations are always gated.

The :func:`get_forced_category` function is called by the
:class:`~coremind.action.router.ActionRouter` *after* the LLM has
assigned a category, so the LLM judgment is the default but
destructive/safe classes are always enforced.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

# ---------------------------------------------------------------------------
# SAFE — silent auto-execute, no notification
# ---------------------------------------------------------------------------

_SAFE_PATTERNS: tuple[str, ...] = (
    "homeassistant.get_state",
    "homeassistant.get_history",
    "homeassistant.get_printer_estimated_pages",
    "calendar.fetch_upcoming_events",
    "calendar.get_next_payday",
    "vikunja.list_tasks",
    "vikunja.get_tasks",
    "weather.",
    "gmail.fetch_unread",
    "gmail.search_emails",
)

# ---------------------------------------------------------------------------
# SUGGEST — notify user but auto-execute after grace, no approval needed
# ---------------------------------------------------------------------------

_SUGGEST_PATTERNS: tuple[str, ...] = (
    "homeassistant.light.turn_off",
    "homeassistant.light.turn_on",
    "homeassistant.turn_on",
    "homeassistant.turn_off",
    "homeassistant.set_temperature",
    "homeassistant.send_notification",
    # All CoreMind→user notifications are suggest (auto-execute, notify result).
    "notification.",
    "notification.send",
)

# ---------------------------------------------------------------------------
# ASK — require explicit user approval (destructive / security-sensitive)
# ---------------------------------------------------------------------------

_ASK_PATTERNS: tuple[str, ...] = (
    # Financial / payment / billing.
    "finance.",
    "payment.",
    "banking.",
    "billing.",
    # Outbound messaging.
    "email.outbound",
    "sms.outbound",
    "chat.outbound",
    "social.outbound",
    "message.outbound",
    # Credentials & infrastructure.
    "credentials.",
    "secrets.",
    "infrastructure.",
    "apikey.",
    # Plugin lifecycle / permissions.
    "plugin.install",
    "plugin.grant",
    "plugin.permission",
    # Safety-mechanism modifications.
    "coremind.safety",
    "coremind.forced_class",
    "coremind.signing_key",
    # Physical-world destructive.
    "homeassistant.vacuum.",
    "homeassistant.lock.",
    "homeassistant.create_automation",
    # Garage doors / covers — glob pattern.
    "homeassistant.*_cover",
)

ForcedCategory = Literal["safe", "suggest", "ask"]

# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


def _match_pattern(action_class: str, pattern: str) -> bool:
    """Check if *action_class* matches a single pattern.

    Four pattern types are supported:

    * **Exact + prefix**: ``"email.outbound"`` — literal equality OR
      ``action_class`` starts with ``pattern + "."``.
    * **Prefix-only**: ``"weather."`` — trailing dot means "starts with"
      (the dot itself is part of the prefix).
    * **Glob**: ``"homeassistant.*_cover"`` — ``*`` is a wildcard.
    """
    if not action_class:
        return False
    if pattern.endswith("."):
        return action_class.startswith(pattern)
    if "*" in pattern:
        return bool(re.fullmatch(pattern.replace("*", ".*"), action_class))
    # Exact match OR prefix with trailing dot (legacy behaviour).
    return action_class == pattern or action_class.startswith(pattern + ".")


def _matches_any(action_class: str, patterns: tuple[str, ...]) -> bool:
    """Return ``True`` if *action_class* matches any pattern."""
    return any(_match_pattern(action_class, p) for p in patterns)


@lru_cache(maxsize=256)
def _matches_ask_cached(action_class: str) -> bool:
    """Cached wrapper — _ASK_PATTERNS is a tuple so it is hashable."""
    return _matches_any(action_class, _ASK_PATTERNS)


@lru_cache(maxsize=256)
def _matches_suggest_cached(action_class: str) -> bool:
    return _matches_any(action_class, _SUGGEST_PATTERNS)


@lru_cache(maxsize=256)
def _matches_safe_cached(action_class: str) -> bool:
    return _matches_any(action_class, _SAFE_PATTERNS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_forced_category(
    action_class: str,
    *,
    user_ask_classes: tuple[str, ...] = (),
) -> ForcedCategory | None:
    """Return the forced category for *action_class*, or ``None``.

    ASK checks run first (security trumps).  Then SUGGEST.  Then SAFE.
    User-configured ask classes are exact-match only.

    Args:
        action_class: The class string declared on the proposed action.
        user_ask_classes: Additional classes the user has listed in
            ``config.ask_classes``.  Exact-match only.

    Returns:
        ``"ask"``, ``"suggest"``, ``"safe"``, or ``None`` if no override
        applies.
    """
    if not action_class:
        return None
    if action_class in user_ask_classes:
        return "ask"
    if _matches_ask_cached(action_class):
        return "ask"
    if _matches_suggest_cached(action_class):
        return "suggest"
    if _matches_safe_cached(action_class):
        return "safe"
    return None
