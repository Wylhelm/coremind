"""Constants for the meta-cognition layer (L8).

Defines hard safety boundaries and default policies. These values are
authoritative — the meta-loop cannot override them at runtime.
"""

from __future__ import annotations

from coremind.meta.schemas import AdjustmentPolicy

FORBIDDEN_PARAMETER_PATHS: list[str] = [
    "autonomy.hard_ask",
    "autonomy.hard_safe",
    "intention.quiet_hours",
    "notifications.quiet_hours",
    "secrets.*",
    "plugins.*.permissions",
    "plugins.*.action_classes",
    "audit.*",
    "logging.*",
    "meta.forbidden_parameter_paths",
    "meta.safety_bounds",
    "meta.enabled",
]
"""Parameter paths that L8 is absolutely forbidden from modifying."""

HARD_BOUNDS: dict[str, tuple[float, float]] = {
    "intention.min_salience": (0.20, 0.70),
    "intention.min_confidence": (0.20, 0.80),
    "reasoning.interval_seconds": (60.0, 7200.0),
    "intention.interval_seconds": (60.0, 3600.0),
    "reflection.interval_seconds": (1800.0, 86400.0),
    "plugins.*.poll_interval_seconds": (30.0, 86400.0),
    "notifications.cooldown_seconds.*": (60.0, 86400.0),
    "autonomy.domains.*": (0.0, 1.0),
}
"""Min/max bounds per adjustable parameter. L8 cannot push values outside."""

DEFAULT_POLICIES: list[AdjustmentPolicy] = [
    AdjustmentPolicy(
        name="lower_salience_when_quiet",
        description="Lower min_salience when system generates few intents",
        observation_kind="intents_per_hour",
        trigger_condition="below",
        threshold=1.0,
        parameter_path="intention.min_salience",
        direction="decrease",
        delta=0.05,
        min_value=0.20,
        max_value=0.70,
        cooldown_seconds=21600.0,
    ),
    AdjustmentPolicy(
        name="raise_salience_when_noisy",
        description="Raise min_salience when many low-quality intents",
        observation_kind="low_quality_intent_rate",
        trigger_condition="above",
        threshold=0.5,
        parameter_path="intention.min_salience",
        direction="increase",
        delta=0.05,
        min_value=0.20,
        max_value=0.70,
        cooldown_seconds=21600.0,
    ),
    AdjustmentPolicy(
        name="increase_cooldown_on_ignored",
        description="Increase notification cooldown for topics consistently ignored",
        observation_kind="notification_ignore_rate",
        trigger_condition="above",
        threshold=0.7,
        parameter_path="notifications.cooldown_seconds.<topic>",
        direction="increase",
        delta=3600.0,
        min_value=60.0,
        max_value=86400.0,
        cooldown_seconds=86400.0,
    ),
    AdjustmentPolicy(
        name="decrease_cooldown_on_engaged",
        description="Decrease cooldown for topics user engages with",
        observation_kind="notification_engagement_rate",
        trigger_condition="above",
        threshold=0.8,
        parameter_path="notifications.cooldown_seconds.<topic>",
        direction="decrease",
        delta=1800.0,
        min_value=60.0,
        max_value=86400.0,
        cooldown_seconds=86400.0,
    ),
    AdjustmentPolicy(
        name="throttle_failing_plugin",
        description="Double poll interval for plugins with high error rates",
        observation_kind="plugin_error_rate",
        trigger_condition="above",
        threshold=0.5,
        parameter_path="plugins.<plugin_id>.poll_interval_seconds",
        direction="increase",
        delta=0.0,
        min_value=30.0,
        max_value=86400.0,
        cooldown_seconds=3600.0,
    ),
    AdjustmentPolicy(
        name="restore_plugin_cadence",
        description="Restore plugin cadence when errors clear",
        observation_kind="plugin_error_rate",
        trigger_condition="below",
        threshold=0.05,
        parameter_path="plugins.<plugin_id>.poll_interval_seconds",
        direction="decrease",
        delta=0.0,
        min_value=30.0,
        max_value=86400.0,
        cooldown_seconds=3600.0,
    ),
    AdjustmentPolicy(
        name="propose_slider_promotion",
        description="Propose autonomy slider increase for high-approval domains",
        observation_kind="domain_approval_rate",
        trigger_condition="above",
        threshold=0.8,
        parameter_path="autonomy.domains.<domain>",
        direction="increase",
        delta=0.1,
        min_value=0.0,
        max_value=1.0,
        cooldown_seconds=604800.0,
        requires_user_approval=True,
    ),
]
"""Built-in adjustment policies. Loaded as defaults when no user overrides exist."""
