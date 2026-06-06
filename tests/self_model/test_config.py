"""Tests for SelfModelConfig — TOML loading, defaults, validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coremind.self_model.config import SelfModelConfig, SelfModelSourcesConfig


class TestSelfModelConfig:
    """Validate configuration model behavior."""

    def test_default_config_is_disabled(self) -> None:
        config = SelfModelConfig()

        assert config.enabled is False
        assert config.extraction_interval_seconds == 3600
        assert config.max_facts_per_cycle == 10

    def test_confidence_thresholds_have_sane_defaults(self) -> None:
        config = SelfModelConfig()

        assert config.min_confidence_declared == 0.95
        assert config.min_confidence_observed == 0.70
        assert config.min_confidence_synthesized == 0.50

    def test_confidence_decay_default(self) -> None:
        config = SelfModelConfig()

        assert config.confidence_decay_per_week == 0.01

    def test_from_toml_dict(self) -> None:
        raw = {
            "enabled": True,
            "extraction_interval_seconds": 1800,
            "max_facts_per_cycle": 20,
            "sources": {
                "github_activity": True,
                "whatsapp_metadata": True,
                "immich_photos": False,
            },
        }

        config = SelfModelConfig.model_validate(raw)

        assert config.enabled is True
        assert config.extraction_interval_seconds == 1800
        assert config.max_facts_per_cycle == 20
        assert config.sources.whatsapp_metadata is True
        assert config.sources.immich_photos is False

    def test_extraction_interval_minimum_enforced(self) -> None:
        with pytest.raises(ValidationError, match="extraction_interval_seconds"):
            SelfModelConfig(extraction_interval_seconds=10)

    def test_confidence_thresholds_bounded(self) -> None:
        with pytest.raises(ValidationError):
            SelfModelConfig(min_confidence_declared=1.5)

    def test_max_context_tokens_minimum(self) -> None:
        with pytest.raises(ValidationError, match="max_context_tokens"):
            SelfModelConfig(max_context_tokens=50)

    def test_frozen_rejects_mutation(self) -> None:
        config = SelfModelConfig()

        with pytest.raises(ValidationError):
            config.enabled = True  # type: ignore[assignment]


class TestSelfModelSourcesConfig:
    """Validate source toggle configuration."""

    def test_default_sources(self) -> None:
        sources = SelfModelSourcesConfig()

        assert sources.github_activity is True
        assert sources.vscode_activity is True
        assert sources.telegram_metadata is True
        assert sources.whatsapp_metadata is False  # Requires QR setup
        assert sources.email_metadata is True
        assert sources.calendar_context is True
        assert sources.health_patterns is True
        assert sources.presence_patterns is True
        assert sources.firefly_spending is True
        assert sources.immich_photos is True

    def test_all_disabled(self) -> None:
        sources = SelfModelSourcesConfig(
            github_activity=False,
            vscode_activity=False,
            telegram_metadata=False,
            whatsapp_metadata=False,
            email_metadata=False,
            calendar_context=False,
            health_patterns=False,
            presence_patterns=False,
            firefly_spending=False,
            immich_photos=False,
        )

        # All false — no collectors will run
        assert not any(
            [
                sources.github_activity,
                sources.vscode_activity,
                sources.telegram_metadata,
                sources.whatsapp_metadata,
                sources.email_metadata,
                sources.calendar_context,
                sources.health_patterns,
                sources.presence_patterns,
                sources.firefly_spending,
                sources.immich_photos,
            ]
        )
