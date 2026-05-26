"""Tests for the per-domain autonomy slider system."""

from __future__ import annotations

import pytest

from coremind.action.autonomy import (
    AutonomyConfig,
    GraduationConfig,
    classify_domain,
    resolve_agency,
)

# ---------------------------------------------------------------------------
# Domain classification
# ---------------------------------------------------------------------------


class TestClassifyDomain:
    """Tests for classify_domain() prefix matching."""

    def test_light_action_maps_to_lights(self) -> None:
        assert classify_domain("light.turn_on") == "lights"
        assert classify_domain("light.set_brightness") == "lights"

    def test_homeassistant_light_maps_to_lights(self) -> None:
        assert classify_domain("homeassistant.light.turn_off") == "lights"

    def test_finance_maps_to_finance(self) -> None:
        assert classify_domain("finance.transfer") == "finance"
        assert classify_domain("finance.check_balance") == "finance"

    def test_payment_maps_to_finance(self) -> None:
        assert classify_domain("payment.send") == "finance"

    def test_lock_maps_to_locks(self) -> None:
        assert classify_domain("lock.unlock") == "locks"
        assert classify_domain("garage_door.open") == "locks"

    def test_vacuum_maps_to_vacuum(self) -> None:
        assert classify_domain("vacuum.start") == "vacuum"
        assert classify_domain("homeassistant.vacuum.send_command") == "vacuum"

    def test_email_outbound_maps_to_messaging(self) -> None:
        assert classify_domain("email.outbound") == "messaging"
        assert classify_domain("email.outbound.gmail") == "messaging"

    def test_email_send_maps_to_messaging(self) -> None:
        assert classify_domain("email.send") == "messaging"
        assert classify_domain("email.send.draft") == "messaging"

    def test_weather_maps_to_weather(self) -> None:
        assert classify_domain("weather.fetch") == "weather"
        assert classify_domain("weather.get_forecast") == "weather"

    def test_media_maps_to_media(self) -> None:
        assert classify_domain("media.play") == "media"
        assert classify_domain("speaker.set_volume") == "media"
        assert classify_domain("tv.turn_off") == "media"

    def test_health_maps_to_health(self) -> None:
        assert classify_domain("health.read") == "health"
        assert classify_domain("health.summary") == "health"

    def test_unknown_falls_to_default(self) -> None:
        assert classify_domain("some.unknown.operation") == "default"
        assert classify_domain("custom_plugin.do_thing") == "default"

    def test_empty_string_returns_default(self) -> None:
        assert classify_domain("") == "default"

    def test_longest_prefix_wins(self) -> None:
        # "homeassistant.vacuum" is more specific than "homeassistant.turn_on"
        assert classify_domain("homeassistant.vacuum.start") == "vacuum"

    def test_notification_send_maps_to_messaging(self) -> None:
        assert classify_domain("notification.send.telegram") == "messaging"

    def test_notification_bare_maps_to_notifications(self) -> None:
        assert classify_domain("notification") == "notifications"


# ---------------------------------------------------------------------------
# AutonomyConfig
# ---------------------------------------------------------------------------


class TestAutonomyConfig:
    """Tests for AutonomyConfig model behavior."""

    def test_get_slider_returns_configured_value(self) -> None:
        config = AutonomyConfig(domains={"lights": 0.8, "finance": 0.1})

        assert config.get_slider("lights") == 0.8
        assert config.get_slider("finance") == 0.1

    def test_get_slider_falls_back_to_default(self) -> None:
        config = AutonomyConfig(default_slider=0.4, domains={})

        assert config.get_slider("unknown") == 0.4

    def test_hard_ask_exact_match(self) -> None:
        config = AutonomyConfig(hard_ask=["finance.transfer"])

        assert config.is_hard_ask("finance.transfer") is True
        assert config.is_hard_ask("finance.transfer.send") is True
        assert config.is_hard_ask("light.toggle") is False

    def test_hard_ask_prefix_pattern(self) -> None:
        config = AutonomyConfig(hard_ask=["credentials."])

        assert config.is_hard_ask("credentials.rotate") is True
        assert config.is_hard_ask("credentials.") is True
        assert config.is_hard_ask("credential") is False

    def test_hard_ask_glob_pattern(self) -> None:
        config = AutonomyConfig(hard_ask=["homeassistant.*_cover"])

        assert config.is_hard_ask("homeassistant.garage_cover") is True
        assert config.is_hard_ask("homeassistant.window_cover") is True
        assert config.is_hard_ask("homeassistant.light") is False

    def test_hard_safe_exact_match(self) -> None:
        config = AutonomyConfig(hard_safe=["weather."])

        assert config.is_hard_safe("weather.fetch") is True
        assert config.is_hard_safe("weather.get_forecast") is True
        assert config.is_hard_safe("finance.transfer") is False

    def test_default_config_has_sensible_domains(self) -> None:
        config = AutonomyConfig()

        assert config.get_slider("lights") == 0.8
        assert config.get_slider("finance") == 0.1
        assert config.get_slider("weather") == 1.0
        assert config.get_slider("vacuum") == 0.3

    def test_default_config_has_hard_ask_rules(self) -> None:
        config = AutonomyConfig()

        assert config.is_hard_ask("finance.transfer") is True
        assert config.is_hard_ask("lock.unlock") is True
        assert config.is_hard_ask("email.outbound") is True
        assert config.is_hard_ask("plugin.install") is True

    def test_default_config_has_hard_safe_rules(self) -> None:
        config = AutonomyConfig()

        assert config.is_hard_safe("weather.fetch") is True
        assert config.is_hard_safe("gmail.fetch_unread") is True
        assert config.is_hard_safe("vikunja.list_tasks") is True

    def test_graduation_config_defaults(self) -> None:
        config = AutonomyConfig()

        assert config.graduation.enabled is True
        assert config.graduation.min_approvals_before_promotion == 10
        assert config.graduation.min_approval_rate_for_promotion == 0.8
        assert config.graduation.max_promotion_per_proposal == 0.1


# ---------------------------------------------------------------------------
# resolve_agency — the core decision algorithm
# ---------------------------------------------------------------------------


class TestResolveAgency:
    """Tests for the resolve_agency() decision function."""

    def test_hard_ask_overrides_everything(self) -> None:
        config = AutonomyConfig(
            domains={"finance": 1.0},
            hard_ask=["finance.transfer"],
        )

        result = resolve_agency("finance.transfer", confidence=0.99, config=config)

        assert result == "ask"

    def test_hard_safe_overrides_slider(self) -> None:
        config = AutonomyConfig(
            domains={"weather": 0.0},
            hard_safe=["weather."],
        )

        result = resolve_agency("weather.fetch", confidence=0.01, config=config)

        assert result == "safe"

    def test_hard_ask_takes_precedence_over_hard_safe(self) -> None:
        config = AutonomyConfig(
            hard_ask=["special.op"],
            hard_safe=["special.op"],
        )

        result = resolve_agency("special.op", confidence=1.0, config=config)

        assert result == "ask"

    def test_high_trust_high_confidence_returns_safe(self) -> None:
        config = AutonomyConfig(domains={"lights": 0.8})

        result = resolve_agency("light.turn_on", confidence=0.95, config=config)

        assert result == "safe"

    def test_high_trust_medium_confidence_returns_suggest(self) -> None:
        config = AutonomyConfig(domains={"lights": 0.8})

        # slider=0.8, suggest threshold = 0.8 * 0.6 = 0.48
        result = resolve_agency("light.turn_on", confidence=0.65, config=config)

        assert result == "suggest"

    def test_high_trust_low_confidence_returns_ask(self) -> None:
        config = AutonomyConfig(domains={"lights": 0.8})

        # slider=0.8, suggest threshold = 0.8 * 0.6 = 0.48
        result = resolve_agency("light.turn_on", confidence=0.40, config=config)

        assert result == "ask"

    def test_low_trust_high_confidence_returns_safe(self) -> None:
        config = AutonomyConfig(domains={"vacuum": 0.3})

        result = resolve_agency("vacuum.start", confidence=0.85, config=config)

        assert result == "safe"

    def test_low_trust_medium_confidence_returns_suggest(self) -> None:
        config = AutonomyConfig(domains={"vacuum": 0.3})

        # slider=0.3, suggest threshold = 0.3 * 0.6 = 0.18
        result = resolve_agency("vacuum.start", confidence=0.25, config=config)

        assert result == "suggest"

    def test_low_trust_very_low_confidence_returns_ask(self) -> None:
        config = AutonomyConfig(domains={"vacuum": 0.3})

        # slider=0.3, suggest threshold = 0.3 * 0.6 = 0.18
        result = resolve_agency("vacuum.start", confidence=0.10, config=config)

        assert result == "ask"

    def test_max_slider_without_hard_safe(self) -> None:
        """Slider at 1.0 means only confidence=1.0 gets safe (without hard_safe)."""
        config = AutonomyConfig(
            domains={"media": 1.0},
            hard_ask=[],
            hard_safe=[],
        )

        # slider=1.0, safe threshold=1.0, suggest threshold=0.6
        result_high = resolve_agency("media.play", confidence=1.0, config=config)
        result_mid = resolve_agency("media.play", confidence=0.8, config=config)
        result_suggest = resolve_agency("media.play", confidence=0.6, config=config)
        result_low = resolve_agency("media.play", confidence=0.3, config=config)

        assert result_high == "safe"
        assert result_mid == "suggest"
        assert result_suggest == "suggest"
        assert result_low == "ask"

    def test_weather_with_hard_safe_always_safe(self) -> None:
        """Weather is hard_safe by default, so slider never applies."""
        config = AutonomyConfig()

        result = resolve_agency("weather.fetch", confidence=0.01, config=config)

        assert result == "safe"

    def test_unknown_domain_uses_default_slider(self) -> None:
        config = AutonomyConfig(default_slider=0.4, domains={})

        # slider=0.4, suggest threshold=0.24
        assert resolve_agency("custom.op", confidence=0.5, config=config) == "safe"
        assert resolve_agency("custom.op", confidence=0.3, config=config) == "suggest"
        assert resolve_agency("custom.op", confidence=0.1, config=config) == "ask"

    def test_exact_threshold_boundary_is_safe(self) -> None:
        config = AutonomyConfig(domains={"media": 0.5})

        result = resolve_agency("media.play", confidence=0.5, config=config)

        assert result == "safe"

    def test_exact_suggest_boundary(self) -> None:
        config = AutonomyConfig(domains={"media": 0.5})

        # suggest threshold = 0.5 * 0.6 = 0.3
        result = resolve_agency("media.play", confidence=0.3, config=config)

        assert result == "suggest"

    def test_just_below_suggest_threshold_is_ask(self) -> None:
        config = AutonomyConfig(domains={"media": 0.5})

        # suggest threshold = 0.5 * 0.6 = 0.3
        result = resolve_agency("media.play", confidence=0.29, config=config)

        assert result == "ask"

    @pytest.mark.parametrize(
        ("action_class", "confidence", "expected"),
        [
            ("light.turn_on", 0.95, "safe"),
            ("light.turn_on", 0.65, "suggest"),
            ("light.turn_on", 0.40, "ask"),
            ("finance.transfer", 0.99, "ask"),  # hard ASK
            ("weather.get_forecast", 0.01, "safe"),  # hard SAFE
            ("gmail.fetch_unread", 0.01, "safe"),  # hard SAFE
            ("homeassistant.lock.unlock", 0.99, "ask"),  # hard ASK
        ],
    )
    def test_default_config_parametrized(
        self,
        action_class: str,
        confidence: float,
        expected: str,
    ) -> None:
        config = AutonomyConfig()

        result = resolve_agency(action_class, confidence, config)

        assert result == expected


# ---------------------------------------------------------------------------
# GraduationConfig validation
# ---------------------------------------------------------------------------


class TestGraduationConfig:
    """Tests for GraduationConfig model constraints."""

    def test_defaults_are_reasonable(self) -> None:
        config = GraduationConfig()

        assert config.min_approvals_before_promotion == 10
        assert config.min_approval_rate_for_promotion == 0.8
        assert config.max_promotion_per_proposal == 0.1
        assert config.min_observation_days == 30
        assert config.promotion_cooldown_days == 7
        assert config.require_user_approval is True

    def test_invalid_rate_rejected(self) -> None:
        with pytest.raises(ValueError):
            GraduationConfig(min_approval_rate_for_promotion=1.5)

    def test_invalid_approvals_rejected(self) -> None:
        with pytest.raises(ValueError):
            GraduationConfig(min_approvals_before_promotion=0)
