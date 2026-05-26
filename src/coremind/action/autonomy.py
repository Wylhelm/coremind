"""Per-domain graduated autonomy system.

Replaces the binary forced-category model with a graduated slider (0.0-1.0)
per domain, combined with the LLM's confidence to produce agency decisions.

The decision algorithm:
    1. Hard ASK check → always "ask" regardless of slider/confidence.
    2. Hard SAFE check → always "safe" regardless of slider/confidence.
    3. Resolve domain from action_class via longest-prefix match.
    4. Look up slider for that domain (or default_slider).
    5. Compare confidence to slider thresholds:
       - confidence >= slider       → "safe"  (auto-execute)
       - confidence >= slider * 0.6 → "suggest" (notify + grace)
       - otherwise                  → "ask"   (block)
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from coremind.action.schemas import ActionCategory

# ---------------------------------------------------------------------------
# Domain classification — maps action_class prefixes to domains
# ---------------------------------------------------------------------------

_DOMAIN_CLASSIFICATION: dict[str, str] = {
    "light": "lights",
    "switch.light": "lights",
    "homeassistant.light": "lights",
    "climate": "hvac",
    "thermostat": "hvac",
    "hvac": "hvac",
    "homeassistant.set_temperature": "hvac",
    "calendar": "calendar",
    "weather": "weather",
    "vacuum": "vacuum",
    "robot": "vacuum",
    "homeassistant.vacuum": "vacuum",
    "lock": "locks",
    "garage_door": "locks",
    "homeassistant.lock": "locks",
    "finance": "finance",
    "transaction": "finance",
    "payment": "finance",
    "banking": "finance",
    "billing": "finance",
    "messaging": "messaging",
    "email.send": "messaging",
    "email.outbound": "messaging",
    "sms.outbound": "messaging",
    "chat.outbound": "messaging",
    "social.outbound": "messaging",
    "message.outbound": "messaging",
    "notification.send": "notifications",
    "homeassistant.send_notification": "notifications",
    "notification": "notifications",
    "health": "health",
    "media": "media",
    "speaker": "media",
    "tv": "media",
    "presence": "presence",
    "notify_user": "presence",
    "homeassistant.turn_on": "lights",
    "homeassistant.turn_off": "lights",
    "homeassistant.send_notification": "notifications",
    "notification": "notifications",
}

# Pre-sorted for longest-prefix matching (computed once at import).
_SORTED_CLASSIFICATION: list[tuple[str, str]] = sorted(
    _DOMAIN_CLASSIFICATION.items(), key=lambda x: -len(x[0])
)

# Suggest threshold multiplier — below slider but above this fraction → "suggest".
_SUGGEST_FACTOR: float = 0.6


def classify_domain(action_class: str) -> str:
    """Map an action_class to its domain via longest-prefix matching."""
    if not action_class:
        return "default"
    for prefix, domain in _SORTED_CLASSIFICATION:
        if action_class == prefix or action_class.startswith(prefix + "."):
            return domain
    return "default"


# ---------------------------------------------------------------------------
# Defaults — match v1 behavior when no config is provided
# ---------------------------------------------------------------------------

_DEFAULT_DOMAIN_SLIDERS: dict[str, float] = {
    "lights": 0.8,
    "hvac": 0.7,
    "calendar": 0.8,
    "weather": 1.0,
    "vacuum": 0.3,
    "locks": 0.1,
    "finance": 0.1,
    "messaging": 0.2,
    "health": 0.5,
    "media": 0.7,
    "presence": 0.6,
    "notifications": 0.6,
}

_DEFAULT_HARD_ASK_CLASSES: tuple[str, ...] = (
    "finance.transfer",
    "finance.payment",
    "payment.",
    "banking.",
    "billing.",
    "lock.unlock",
    "garage_door.open",
    "homeassistant.lock.",
    "homeassistant.*_cover",
    "messaging.send_external",
    "email.send",
    "email.outbound",
    "sms.outbound",
    "chat.outbound",
    "social.outbound",
    "message.outbound",
    "security.disable",
    "plugin.install",
    "plugin.grant",
    "plugin.permission",
    "config.modify",
    "credentials.",
    "secrets.",
    "infrastructure.",
    "apikey.",
    "coremind.safety",
    "coremind.forced_class",
    "coremind.signing_key",
)

_DEFAULT_HARD_SAFE_CLASSES: tuple[str, ...] = (
    "homeassistant.get_state",
    "homeassistant.get_history",
    "homeassistant.get_printer_estimated_pages",
    "calendar.fetch_upcoming_events",
    "calendar.get_next_payday",
    "calendar.fetch_events",
    "vikunja.list_tasks",
    "vikunja.get_tasks",
    "weather.",
    "gmail.fetch_unread",
    "gmail.search_emails",
    "health.read",
)


# ---------------------------------------------------------------------------
# Configuration models — as specified in Phase 1 design doc
# ---------------------------------------------------------------------------


class HardAskRule(BaseModel):
    """A rule that forces ASK regardless of slider."""

    model_config = ConfigDict(frozen=True)

    action_class: str = Field(min_length=1)
    reason: str = ""


class HardSafeRule(BaseModel):
    """A rule that forces SAFE regardless of slider."""

    model_config = ConfigDict(frozen=True)

    action_class: str = Field(min_length=1)


class GraduationConfig(BaseModel):
    """Configuration for the slider graduation mechanism."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    min_approvals_before_promotion: int = Field(default=10, ge=1)
    min_approval_rate_for_promotion: float = Field(default=0.8, ge=0.0, le=1.0)
    max_promotion_per_proposal: float = Field(default=0.1, ge=0.01, le=0.5)
    min_observation_days: int = Field(default=30, ge=1)
    promotion_cooldown_days: int = Field(default=7, ge=1)
    require_user_approval: bool = True

    # Override for dev/test: if > 0, replaces min_observation_days.
    # Set via env COREMIND_DEV_QUICK_GRADUATION=true or config.toml
    min_observation_days_dev: int = Field(default=0, ge=0)

    # Demotion — mirror of promotion for when the user rejects consistently.
    demotion_enabled: bool = True
    min_rejections_before_demotion: int = Field(default=5, ge=1)
    min_rejection_rate_for_demotion: float = Field(default=0.5, ge=0.0, le=1.0)
    max_demotion_per_proposal: float = Field(default=0.1, ge=0.01, le=0.5)
    demotion_cooldown_days: int = Field(default=14, ge=1)

    @property
    def effective_observation_days(self) -> int:
        """Return the dev override if set, otherwise the production value."""
        if self.min_observation_days_dev > 0:
            return self.min_observation_days_dev
        return self.min_observation_days


class AutonomyConfig(BaseModel):
    """Complete autonomy configuration.

    Attributes:
        default_slider: Fallback slider for domains not explicitly configured.
        domains: Per-domain slider values (0.0 = always ask, 1.0 = always auto).
        hard_ask: Rules that always force ASK regardless of slider/confidence.
        hard_safe: Rules that always force SAFE regardless of slider/confidence.
        graduation: Configuration for the automatic promotion mechanism.
    """

    model_config = ConfigDict(frozen=True)

    default_slider: float = Field(default=0.4, ge=0.0, le=1.0)
    domains: dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_DOMAIN_SLIDERS))
    hard_ask: list[HardAskRule] = Field(
        default_factory=lambda: [HardAskRule(action_class=c) for c in _DEFAULT_HARD_ASK_CLASSES]
    )
    hard_safe: list[HardSafeRule] = Field(
        default_factory=lambda: [HardSafeRule(action_class=c) for c in _DEFAULT_HARD_SAFE_CLASSES]
    )
    graduation: GraduationConfig = Field(default_factory=GraduationConfig)

    @field_validator("hard_ask", mode="before")
    @classmethod
    def _coerce_hard_ask(
        cls, v: object
    ) -> list[dict[str, str]]:
        """Accept both the design-doc TOML shape and a flat list of strings.

        TOML ``[autonomy.hard_ask]\\nclasses = [...]`` arrives as a dict
        with a ``classes`` key.  A raw list of class-name strings is also
        accepted for backward compatibility during migration.
        """
        if isinstance(v, dict):
            raw = v.get("classes", [])
        elif isinstance(v, (list, tuple)):
            raw = list(v)
        else:
            return v  # let Pydantic raise the validation error

        return [HardAskRule(action_class=r) if isinstance(r, str) else r for r in raw]

    @field_validator("hard_safe", mode="before")
    @classmethod
    def _coerce_hard_safe(
        cls, v: object
    ) -> list[dict[str, str]]:
        """Same coercion logic as _coerce_hard_ask but for hard_safe rules."""
        if isinstance(v, dict):
            raw = v.get("classes", [])
        elif isinstance(v, (list, tuple)):
            raw = list(v)
        else:
            return v

        return [HardSafeRule(action_class=r) if isinstance(r, str) else r for r in raw]

    def _hard_ask_classes(self) -> tuple[str, ...]:
        """Extract action_class strings from hard_ask rules (for matching)."""
        return tuple(r.action_class for r in self.hard_ask)

    def _hard_safe_classes(self) -> tuple[str, ...]:
        """Extract action_class strings from hard_safe rules (for matching)."""
        return tuple(r.action_class for r in self.hard_safe)

    def get_slider(self, domain: str) -> float:
        """Return the slider value for a domain, falling back to default."""
        return self.domains.get(domain, self.default_slider)

    def is_hard_ask(self, action_class: str) -> bool:
        """Return True if this action class is hard-locked to ASK."""
        return _matches_any(action_class, self._hard_ask_classes())

    def is_hard_safe(self, action_class: str) -> bool:
        """Return True if this action class is hard-locked to SAFE."""
        return _matches_any(action_class, self._hard_safe_classes())


# ---------------------------------------------------------------------------
# Decision algorithm
# ---------------------------------------------------------------------------


def resolve_agency(
    action_class: str,
    confidence: float,
    config: AutonomyConfig,
) -> ActionCategory:
    """Determine whether to auto-execute, suggest, or ask for approval.

    Combines the user's per-domain trust (slider) with the LLM's confidence
    in the proposed action to produce the agency decision.

    Returns:
        "safe" (auto-execute), "suggest" (notify + grace), or "ask" (block).
    """
    if config.is_hard_ask(action_class):
        return "ask"
    if config.is_hard_safe(action_class):
        return "safe"

    domain = classify_domain(action_class)
    slider = config.get_slider(domain)

    if confidence >= slider:
        return "safe"
    if confidence >= slider * _SUGGEST_FACTOR:
        return "suggest"
    return "ask"


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


def _match_pattern(action_class: str, pattern: str) -> bool:
    """Check if action_class matches a single pattern.

    Supports:
      - Trailing dot: "weather." → prefix match.
      - Glob: "homeassistant.*_cover" → fnmatch-style.
      - Exact + prefix: "finance.transfer" → exact or starts with "finance.transfer.".
    """
    if not action_class:
        return False
    if pattern.endswith("."):
        return action_class.startswith(pattern)
    if "*" in pattern:
        return bool(re.fullmatch(pattern.replace("*", ".*"), action_class))
    return action_class == pattern or action_class.startswith(pattern + ".")


def _matches_any(action_class: str, patterns: Sequence[str]) -> bool:
    """Return True if action_class matches any pattern in the sequence."""
    return any(_match_pattern(action_class, p) for p in patterns)
