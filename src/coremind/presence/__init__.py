"""CoreMind Presence Layer — Pillar #3 (Physical Presence).

Gives CoreMind the ability to speak in the room through the Google Nest Hub,
enabling ambient interactions like morning greetings, evening summaries,
and significant event notifications.
"""

from coremind.presence.detector import PresenceDetector
from coremind.presence.evaluator import ActivityEvaluation, ActivityEvaluator
from coremind.presence.scheduler import PresenceScheduler
from coremind.presence.schemas import PresenceConfig, PresenceEvent, PresenceEventType

__all__ = [
    "ActivityEvaluation",
    "ActivityEvaluator",
    "PresenceConfig",
    "PresenceDetector",
    "PresenceEvent",
    "PresenceEventType",
    "PresenceScheduler",
]
