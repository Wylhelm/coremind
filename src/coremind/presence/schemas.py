"""Presence module schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PresenceEventType(StrEnum):
    MORNING_GREETING = "morning_greeting"
    EVENING_SUMMARY = "evening_summary"
    SIGNIFICANT_EVENT = "significant_event"
    USER_ARRIVED = "user_arrived"
    WEATHER_ALERT = "weather_alert"
    HEALTH_NUDGE = "health_nudge"


class PresenceEvent(BaseModel):
    """An ambient interaction to be delivered via Nest Hub or other channel."""

    model_config = ConfigDict(frozen=True)

    event_type: PresenceEventType
    message: str = Field(min_length=1)
    urgency: float = Field(default=0.5, ge=0.0, le=1.0)
    display_url: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PresenceConfig(BaseModel):
    """Configuration for the presence scheduler."""

    enabled: bool = True
    tts_script: str = "~/workspace/home-assistant/scripts/gbot-say.sh"
    cast_script: str = "~/workspace/home-assistant/scripts/cast-dashboard.sh"
    morning_greeting: bool = True
    morning_time: str = "08:00"
    evening_summary: bool = False
    evening_time: str = "21:00"
    min_urgency: float = 0.5
