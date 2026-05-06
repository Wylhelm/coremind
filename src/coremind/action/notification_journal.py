"""Notification journal — prevents duplicate/repetitive notifications.

Tracks every notification sent: when, what topic, and what message.
Before sending, CoreMind checks if a similar topic was already raised
recently and suppresses repeats.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Topics we track for deduplication
TOPIC_KEYWORDS = {
    "sommeil": ["sleep", "sommeil", "dormi", "dormir", "nuit", "réveil"],
    "pas": ["step", "pas", "marche", "promenade", "bouger"],
    "météo": ["météo", "pluie", "température", "degrés", "°C", "parapluie", "vent"],
    "chats": ["chat", "minuit", "poukie", "timimi", "canapé"],
    "finances": ["compte", "solde", "visa", "mastercard", "scotia", "paiement"],
    "pause": ["pause", "bureau", "travail", "café", "☕"],
    "batterie": ["batterie", "robot", "aspirateur", "nettoyage", "séchage"],
    "calendrier": ["calendrier", "événement", "rendez-vous", "paie"],
}


def _extract_topic(text: str) -> str:
    """Identify the topic of a notification from its text."""
    text_lower = text.lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return topic
    return "autre"


class NotificationJournal:
    """Tracks sent notifications to prevent spam."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or Path.home() / ".coremind" / "notification_journal.jsonl"
        self._entries: list[dict[str, str]] = []
        self._cooldowns: dict[str, int] = {
            "sommeil": 21600,  # 6h
            "pas": 14400,  # 4h
            "météo": 21600,  # 6h
            "chats": 7200,  # 2h
            "finances": 43200,  # 12h
            "pause": 7200,  # 2h
            "batterie": 21600,  # 6h
            "calendrier": 14400,  # 4h
            "autre": 3600,  # 1h
        }
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open() as f:
                self._entries = [json.loads(line) for line in f if line.strip()]
        except Exception:
            log.warning("notification_journal.load_failed")

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w") as f:
            for entry in self._entries[-500:]:  # Keep last 500
                f.write(json.dumps(entry) + "\n")

    def should_send(self, message: str, intent_id: str = "") -> bool:
        """Check if this notification should be sent or suppressed as duplicate."""
        topic = _extract_topic(message)
        cooldown = self._cooldowns.get(topic, 3600)
        now = datetime.now(UTC)

        # Check recent entries for same topic
        for entry in reversed(self._entries):
            if entry.get("topic") != topic:
                continue
            last_time = datetime.fromisoformat(entry["timestamp"])
            if (now - last_time).total_seconds() < cooldown:
                log.info(
                    "notification_journal.suppressed_duplicate",
                    topic=topic,
                    cooldown_hours=cooldown / 3600,
                    last_sent=entry["timestamp"][:19],
                )
                return False

        # Record this notification
        entry = {
            "timestamp": now.isoformat(),
            "topic": topic,
            "message": message[:200],
            "intent_id": intent_id,
        }
        self._entries.append(entry)
        self._save()
        return True

    def last_topic_time(self, topic: str) -> datetime | None:
        """Return when a topic was last notified, or None."""
        for entry in reversed(self._entries):
            if entry.get("topic") == topic:
                return datetime.fromisoformat(entry["timestamp"])
        return None
