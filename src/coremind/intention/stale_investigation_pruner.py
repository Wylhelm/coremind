"""Stale investigation pruner — removes investigations invalidated by new data.

Runs before each intention cycle.  Compares active investigations against
the current world snapshot to detect premises that are no longer true.

Design principles:
- Conservative: only remove when the data *conclusively* disproves the premise.
- Auditable: every removal is logged and journaled.
- Lightweight: no LLM calls — pure data-driven checks on WorldSnapshot entities.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Protocol

import structlog

from coremind.world.model import WorldSnapshot

log = structlog.get_logger(__name__)

# Known entity attributes that indicate a resolved issue for common premises.
# Format: (entity_type_fragment, attribute_fragment) → premise is invalidated
# if the entity's updated_at is more recent than the investigation's last check.
STALE_PREMISE_PATTERNS: dict[str, dict[str, str]] = {
    "robot": {
        "nettoyage": "The robot has cleaned recently — the 'not cleaning' premise is stale.",
        "vacuum": "The vacuum state shows recent activity — the 'idle' premise is stale.",
        "cleaning": "Cleaning data is fresh — the investigation premise is no longer valid.",
    },
    "tapo": {
        "activity": "Camera activity data is updating — the 'stale data' premise is invalidated.",
    },
    "sleep": {
        "hours": "Sleep data has been refreshed — any 'missing sleep' premise is stale.",
    },
    "step": {
        "count": "Step count data is updating — 'missing health data' premise is stale.",
    },
}

# How recent must the entity's updated_at be to consider it "fresh" (in hours).
FRESHNESS_HOURS = 48


class InvestigationsStore(Protocol):
    """Protocol for loading/saving the investigations file."""

    def get_active(self) -> list[str]:
        """Return the list of active investigation text strings."""
        ...

    def save(self, investigations: list[str]) -> None:
        """Persist the filtered investigation list."""
        ...


class FileInvestigationsStore:
    """JSON-file-backed investigations store."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def get_active(self) -> list[str]:
        if not self._path.exists():
            return []
        try:
            with self._path.open() as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def save(self, investigations: list[str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w") as f:
            json.dump(investigations, f, indent=2)


class StaleInvestigationPruner:
    """Detect and remove investigations whose premises are invalidated by new data.

    Pure data-driven — no LLM calls.  Only removes when the world snapshot
    *conclusively* shows the premise is no longer true.

    Args:
        investigations: Source of active investigations.
        log_path: Optional JSONL path for audit journaling of prunes.
    """

    def __init__(
        self,
        investigations: InvestigationsStore,
        log_path: Path | None = None,
    ) -> None:
        self._investigations = investigations
        self._log_path = log_path

    def prune(self, snapshot: WorldSnapshot) -> list[str]:
        """Return investigations with stale ones removed.

        Does NOT mutate the store — caller is responsible for saving.
        """
        active = self._investigations.get_active()
        if not active:
            return active

        now = datetime.now(datetime.UTC)
        kept = []
        pruned = []

        for inv in active:
            if self._is_premise_stale(inv, snapshot, now):
                pruned.append(inv)
            else:
                kept.append(inv)

        if pruned:
            log.info(
                "stale_pruner.pruned",
                removed=len(pruned),
                kept=len(kept),
                pruned_topics=[_topic_summary(p) for p in pruned],
            )
            self._journal_prunes(pruned, now)

        return kept

    def _is_premise_stale(
        self, investigation: str, snapshot: WorldSnapshot, now: datetime
    ) -> bool:
        """Check if the investigation's premise is stale based on snapshot data."""
        inv_lower = investigation.lower()

        for entity in snapshot.entities:
            entity_name = str(getattr(entity, "display_name", "") or "").lower()
            entity_type = str(getattr(entity, "type", "") or "").lower()

            # Check if this entity type matches the investigation
            for type_key, attributes in STALE_PREMISE_PATTERNS.items():
                if type_key in entity_name or type_key in entity_type:
                    # The investigation mentions this entity category
                    if type_key not in inv_lower:
                        continue

                    # Check freshness
                    updated_at = getattr(entity, "updated_at", None)
                    if updated_at is not None:
                        age_hours = (now - updated_at).total_seconds() / 3600
                        if age_hours > FRESHNESS_HOURS:
                            continue  # data too old to trust

                    # Check if entity properties indicate resolution
                    props = getattr(entity, "properties", {}) or {}
                    for prop_key in attributes:
                        if (
                            any(prop_key in k.lower() for k in props)
                            and _has_stale_date_reference(inv_lower)
                        ):
                                return True

                    # If investigation says "not X since DATE" and entity data
                    # is more recent than that date, it's stale
                    if _has_stale_date_reference(inv_lower) and updated_at is not None:
                        return True

        return False

    def _journal_prunes(self, pruned: list[str], now: datetime) -> None:
        """Write pruned investigations to an audit log."""
        if self._log_path is None:
            return
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a") as f:
                for inv in pruned:
                    entry = json.dumps({
                        "timestamp": now.isoformat(),
                        "event": "investigation.pruned",
                        "reason": "stale_premise",
                        "investigation": inv[:200],
                    })
                    f.write(entry + "\n")
        except OSError:
            pass


def _topic_summary(investigation: str) -> str:
    """Extract a short topic summary from an investigation text."""
    return investigation[:80].strip()


def _has_stale_date_reference(text: str) -> bool:
    """Check if the text contains a reference to a stale date.

    Matches patterns like:
    - "depuis le 17 mai"
    - "since May 17"
    - "not X since DATE"
    - "no X for N days"
    - "pas nettoyé depuis le X"
    """
    patterns = [
        r"depuis\s+le\s+\d{1,2}\s+("
        r"janvier|février|mars|avril|mai|juin|"
        r"juillet|août|septembre|octobre|novembre|décembre"
        r")",
        r"since\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}",
        r"(pas|not?|no)\s+\w+\s+(depuis|since)\s+",
        r"n'a\s+pas\s+\w+\s+depuis",
        r"no\s+\w+\s+for\s+\d+\s+days",
        r"hasn'?t\s+\w+\s+since",
    ]
    return any(re.search(p, text) for p in patterns)
