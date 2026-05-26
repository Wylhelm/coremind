"""Tests for personalization config model."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from coremind.personalization.config import PersonalizationConfig, get_timezone


class TestPersonalizationConfig:
    """Unit tests for PersonalizationConfig."""

    def test_defaults(self) -> None:
        cfg = PersonalizationConfig()

        assert cfg.language == "en"
        assert cfg.user_name == "User"
        assert cfg.timezone == "UTC"
        assert cfg.greeting_name == ""
        assert cfg.notification_style == "je"

    def test_language_name_english(self) -> None:
        cfg = PersonalizationConfig(language="en")

        assert cfg.language_name == "English"

    def test_language_name_french(self) -> None:
        cfg = PersonalizationConfig(language="fr")

        assert cfg.language_name == "French"

    def test_language_name_auto(self) -> None:
        cfg = PersonalizationConfig(language="auto")

        assert cfg.language_name == "the user's detected language"

    def test_effective_greeting_uses_greeting_name(self) -> None:
        cfg = PersonalizationConfig(user_name="Alice", greeting_name="Ali")

        assert cfg.effective_greeting == "Ali"

    def test_effective_greeting_falls_back_to_user_name(self) -> None:
        cfg = PersonalizationConfig(user_name="Alice", greeting_name="")

        assert cfg.effective_greeting == "Alice"

    def test_frozen_model(self) -> None:
        cfg = PersonalizationConfig()

        with pytest.raises(Exception):  # noqa: B017 — ValidationError in frozen model
            cfg.language = "fr"

    @pytest.mark.parametrize("lang", ["xx", "de", ""])
    def test_invalid_language_rejected(self, lang: str) -> None:
        with pytest.raises(Exception):  # noqa: B017 — ValidationError from pattern
            PersonalizationConfig(language=lang)

    @pytest.mark.parametrize("style", ["tu", "nous", ""])
    def test_invalid_notification_style_rejected(self, style: str) -> None:
        with pytest.raises(Exception):  # noqa: B017 — ValidationError from pattern
            PersonalizationConfig(notification_style=style)


class TestGetTimezone:
    """Unit tests for get_timezone helper."""

    def test_valid_timezone(self) -> None:
        cfg = PersonalizationConfig(timezone="America/Toronto")

        result = get_timezone(cfg)

        assert result == ZoneInfo("America/Toronto")

    def test_utc_default(self) -> None:
        cfg = PersonalizationConfig()

        result = get_timezone(cfg)

        assert result == ZoneInfo("UTC")

    def test_invalid_timezone_falls_back_to_utc(self) -> None:
        cfg = PersonalizationConfig.model_construct(timezone="Not/A/Timezone")

        result = get_timezone(cfg)

        assert result == ZoneInfo("UTC")
