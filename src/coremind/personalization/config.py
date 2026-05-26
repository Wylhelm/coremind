"""Personalization configuration model.

Holds only what sensors cannot observe: how to address the user,
what language to speak, and what timezone they're in.  Everything
else — pets, family, rooms, habits — is discovered through
L1→L2 perception and L3 memory.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

_LANGUAGE_NAMES: dict[str, str] = {
    "fr": "French",
    "en": "English",
    "auto": "the user's detected language",
}


class PersonalizationConfig(BaseModel):
    """User personalization settings.

    Attributes:
        language: Language code for all LLM-facing output.
        user_name: How the system refers to the user internally.
        timezone: IANA timezone identifier.
        greeting_name: How to greet the user (falls back to user_name).
        notification_style: Notification voice — "je" (first person) or "il" (neutral).
    """

    model_config = ConfigDict(frozen=True)

    language: str = Field(default="en", pattern=r"^(fr|en|auto)$")
    user_name: str = Field(default="User")
    timezone: str = Field(default="UTC")
    greeting_name: str = Field(default="")
    notification_style: str = Field(default="je", pattern=r"^(je|il)$")

    @property
    def language_name(self) -> str:
        """Human-readable language name for prompt injection."""
        return _LANGUAGE_NAMES.get(self.language, "English")

    @property
    def effective_greeting(self) -> str:
        """Greeting name with fallback to user_name."""
        return self.greeting_name or self.user_name


def get_timezone(config: PersonalizationConfig) -> ZoneInfo:
    """Resolve the configured timezone to a ZoneInfo instance.

    Falls back to UTC if the configured timezone is invalid.
    """
    try:
        return ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")
