"""Action class category override tests."""

from __future__ import annotations

import pytest

from coremind.action.action_classes import get_forced_category


class TestSafePatterns:
    """SAFE classes should return ``"safe"`` regardless of what the LLM said."""

    @pytest.mark.parametrize(
        "action_class",
        [
            "notification.send",
            "homeassistant.get_state",
            "homeassistant.get_history",
            "homeassistant.get_printer_estimated_pages",
            "calendar.fetch_upcoming_events",
            "calendar.get_next_payday",
            "vikunja.list_tasks",
            "vikunja.get_tasks",
            "gmail.fetch_unread",
            "gmail.search_emails",
        ],
    )
    def test_safe_exact_matches(self, action_class: str) -> None:
        assert get_forced_category(action_class) == "safe"

    def test_safe_prefix_matches(self) -> None:
        assert get_forced_category("weather.get_forecast") == "safe"
        assert get_forced_category("weather.alerts") == "safe"
        assert get_forced_category("weather.") == "safe"


class TestSuggestPatterns:
    """SUGGEST classes should return ``"suggest"``."""

    @pytest.mark.parametrize(
        "action_class",
        [
            "homeassistant.light.turn_off",
            "homeassistant.light.turn_on",
            "homeassistant.turn_on",
            "homeassistant.turn_off",
            "homeassistant.set_temperature",
            "homeassistant.send_notification",
        ],
    )
    def test_suggest_exact_matches(self, action_class: str) -> None:
        assert get_forced_category(action_class) == "suggest"


class TestAskPatterns:
    """ASK classes should return ``"ask"``."""

    @pytest.mark.parametrize(
        "action_class",
        [
            # Legacy forced classes (still work).
            "email.outbound",
            "email.outbound.gmail",
            "coremind.safety",
            "coremind.safety.pause",
            "plugin.install",
            "finance.transfer",
            "payment.refund",
            "credentials.github",
            "secrets.vault",
            "infrastructure.restart",
            "apikey.rotate",
            "plugin.grant",
            "plugin.permission.write",
            "coremind.forced_class",
            "coremind.signing_key",
            "chat.outbound.slack",
            "social.outbound.twitter",
            "message.outbound",
            "billing.invoice",
            "banking.withdrawal",
            # New ASK classes.
            "homeassistant.vacuum.start",
            "homeassistant.vacuum.return_to_base",
            "homeassistant.lock.lock",
            "homeassistant.lock.unlock",
            "homeassistant.create_automation",
            "finance.pay",
        ],
    )
    def test_ask_matches(self, action_class: str) -> None:
        assert get_forced_category(action_class) == "ask"

    def test_glob_match_cover(self) -> None:
        """homeassistant.*_cover should match any class ending in _cover under homeassistant."""
        assert get_forced_category("homeassistant.garage_cover") == "ask"
        assert get_forced_category("homeassistant.front_door_cover") == "ask"

    def test_user_declared_classes(self) -> None:
        assert get_forced_category("wearable", user_ask_classes=("wearable",)) == "ask"
        # user_ask_classes is exact-match only
        assert get_forced_category("wearable.heartrate", user_ask_classes=("wearable",)) is None


class TestNoOverride:
    """Classes not in any list should return None."""

    @pytest.mark.parametrize(
        "action_class",
        [
            "light",
            "hvac",
            "home.scene",
            "finance",  # bare finance, not prefix-matched
            "unknown.plugin.thing",
        ],
    )
    def test_no_match_returns_none(self, action_class: str) -> None:
        assert get_forced_category(action_class) is None

    def test_empty_class(self) -> None:
        assert get_forced_category("") is None
