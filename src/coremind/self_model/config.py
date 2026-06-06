"""Self-Model configuration.

Loaded as part of :class:`~coremind.config.DaemonConfig` from the
``[self_model]`` section of ``~/.coremind/config.toml``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SelfModelSourcesConfig(BaseModel):
    """Toggle individual data collectors on/off."""

    model_config = ConfigDict(frozen=True)

    github_activity: bool = True
    vscode_activity: bool = True
    telegram_metadata: bool = True
    whatsapp_metadata: bool = False
    email_metadata: bool = True
    calendar_context: bool = True
    health_patterns: bool = True
    presence_patterns: bool = True
    firefly_spending: bool = True
    immich_photos: bool = True


class SelfModelConfig(BaseModel):
    """Configuration for the Self-Model plugin.

    Controls extraction frequency, confidence thresholds, source toggles,
    and rate limiting for fact generation.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    extraction_interval_seconds: int = Field(
        default=3600,
        ge=60,
        description="How often to run the extraction pipeline (seconds).",
    )
    max_facts_per_cycle: int = Field(
        default=10,
        ge=1,
        description="Maximum new facts per extraction cycle (prevents flood).",
    )
    min_confidence_declared: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Minimum confidence for declared facts.",
    )
    min_confidence_observed: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        description="Minimum confidence for observed patterns.",
    )
    min_confidence_synthesized: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description="Minimum confidence for synthesized inferences.",
    )
    confidence_decay_per_week: float = Field(
        default=0.01,
        ge=0.0,
        le=0.1,
        description="Weekly confidence decay for unrefreshed observed facts.",
    )
    allow_questions: bool = Field(
        default=True,
        description="Allow generation of Level 4 (questioned) hypotheses.",
    )
    max_context_tokens: int = Field(
        default=2000,
        ge=100,
        description="Maximum tokens for self-model context injected into prompts.",
    )
    seed_file: str = Field(
        default="~/.coremind/self_model_seed.toml",
        description="Path to initial seed file for bootstrap declarations.",
    )
    sources: SelfModelSourcesConfig = Field(default_factory=SelfModelSourcesConfig)
