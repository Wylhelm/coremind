"""CoreMind Presence Layer — Pillar #3 (Physical Presence).

Gives CoreMind the ability to speak in the room through the Google Nest Hub,
enabling ambient interactions like morning greetings, evening summaries,
and significant event notifications.
"""

from coremind.presence.scheduler import PresenceScheduler
from coremind.presence.schemas import PresenceConfig, PresenceEvent, PresenceEventType

__all__ = [
    "PresenceConfig",
    "PresenceEvent",
    "PresenceEventType",
    "PresenceScheduler",
]
