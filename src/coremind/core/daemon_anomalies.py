"""Anomaly alert checker — extracted from daemon.py as a standalone factory.

Watches the reasoning journal for high-severity anomalies and alerts via the
notification router, with multi-layered deduplication.

Deduplication layers:
1. Notification journal text-hash (existing)
2. Category-hash: same anomaly category = 24h cooldown
3. Staleness threshold: skip anomalies about events > 6h old
4. Rate limiting: max 3 high-severity alerts per hour

Usage::

    task = create_anomaly_checker_task(
        reasoning_journal_path=some_path,
        notify_journal=NotificationJournal(),
        notify_router=router,
    )
    # ... later ...
    task.cancel()
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

import structlog

from coremind.action.notification_journal import NotificationJournal
from coremind.notify.router import NotificationRouter

log = structlog.get_logger(__name__)

# Maximum number of high-severity alerts per hour
_MAX_ALERTS_PER_HOUR = 3
# Cooldown per anomaly category (seconds) — 24 hours
_CATEGORY_COOLDOWN = 86400
# Ignore anomalies about events older than this (seconds) — 6 hours
_STALENESS_THRESHOLD = 21600
# Skip cycles older than this (seconds) on startup — prevents full replay
_MAX_CYCLE_AGE_SECONDS = 3600


def _extract_category(description: str) -> str:
    """Extract a stable category key from an anomaly description.

    Strips timestamps, numeric values, and IDs so that semantically
    identical anomalies (e.g. "sensor X unavailable at 15:23" vs
    "sensor X unavailable at 16:45") produce the same hash.
    """
    # Strip timestamps (ISO 8601)
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s,]*", "", description)
    # Strip HH:MM time references
    text = re.sub(r"\d{2}:\d{2}(:\d{2})?", "", text)
    # Strip percentages and decimal numbers
    text = re.sub(r"\d+\.?\d*\s*%", "", text)
    text = re.sub(r"\d+\.\d+°?[CF]\b", "", text)
    # Strip dollar amounts
    text = re.sub(r"\$\s*\d+[.,]?\d*", "", text)
    # Strip cycle IDs and UUIDs
    text = re.sub(
        r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}",
        "",
        text,
    )
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip().lower()
    # Hash for stable key
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _iso_to_unix(timestamp: str) -> float:
    """Convert ISO 8601 timestamp string to Unix timestamp."""
    # Handle timezone offset (e.g. +00:00, -04:00, or Z)
    ts = timestamp.replace("Z", "+00:00")
    return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))


def _is_stale(description: str, baseline: str) -> bool:
    """Check if anomaly refers to events older than the staleness threshold.

    Looks for timestamps in the description and baseline, and skips
    anomalies about events that happened more than _STALENESS_THRESHOLD
    seconds ago.
    """
    now = time.time()
    # Find the most recent timestamp mentioned
    timestamps = re.findall(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
        description + " " + baseline,
    )
    if not timestamps:
        return False  # No timestamp to check → not stale
    # All timestamps should be in UTC (from SurrealDB)
    try:
        latest_ts = max(
            time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) for ts in timestamps
        )
        return (now - latest_ts) > _STALENESS_THRESHOLD
    except (ValueError, OverflowError):
        return False


def create_anomaly_checker_task(
    reasoning_journal_path: Path,
    notify_journal: NotificationJournal,
    notify_router: NotificationRouter,
) -> asyncio.Task[None]:
    """Create and return a background :class:`asyncio.Task` that watches the
    reasoning journal for high-severity anomalies and alerts through the
    notification router.

    Parameters
    ----------
    reasoning_journal_path:
        Path to the ``reasoning.log`` JSON-lines file produced by the
        reasoning loop's :class:`~coremind.reasoning.persistence.JsonlCyclePersister`.
    notify_journal:
        Deduplication journal — only alerts when ``should_send`` returns ``True``.
    notify_router:
        Notification router used to deliver the anomaly alert to all
        registered notification ports.
    """

    async def _check_anomalies() -> None:  # noqa: PLR0912 — sequential filter layers require branches
        """Watch reasoning.log for high-severity anomalies and alert."""
        _last_pos = 0
        _category_cache: dict[str, float] = {}  # category hash → last sent time
        _alert_count_window: list[float] = []  # timestamps of recent alerts

        while True:
            try:
                if reasoning_journal_path.exists():  # noqa: ASYNC240
                    with reasoning_journal_path.open() as _f:
                        _f.seek(_last_pos)
                        for _line in _f:
                            try:
                                _cycle = json.loads(_line)
                            except json.JSONDecodeError:
                                continue

                            # --- Layer 0: Cycle age gate ---
                            # Skip cycles older than _MAX_CYCLE_AGE_SECONDS to prevent
                            # full journal replay on daemon restart.
                            _cycle_ts = _cycle.get("timestamp")
                            if _cycle_ts:
                                try:
                                    _cycle_age = time.time() - _iso_to_unix(_cycle_ts)
                                    if _cycle_age > _MAX_CYCLE_AGE_SECONDS:
                                        continue  # stale cycle, skip entirely
                                except (ValueError, OverflowError):
                                    pass

                            for _a in _cycle.get("anomalies", []):
                                if _a.get("severity") != "high":
                                    continue

                                desc = _a.get("description", "")
                                baseline = _a.get("baseline_description", "")

                                # --- Layer 1: Staleness check ---
                                if _is_stale(desc, baseline):
                                    log.debug(
                                        "anomaly_checker.stale_skipped",
                                        description=desc[:80],
                                    )
                                    continue

                                # --- Layer 2: Category cooldown (24h) ---
                                cat = _extract_category(desc)
                                last_sent = _category_cache.get(cat)
                                now_ts = time.time()
                                if last_sent and (now_ts - last_sent) < _CATEGORY_COOLDOWN:
                                    log.debug(
                                        "anomaly_checker.cooldown_skipped",
                                        category=cat,
                                        seconds_since=now_ts - last_sent,
                                    )
                                    continue

                                # --- Layer 3: Rate limiting ---
                                _alert_count_window[:] = [
                                    t
                                    for t in _alert_count_window
                                    if now_ts - t < 3600  # noqa: PLR2004
                                ]
                                if len(_alert_count_window) >= _MAX_ALERTS_PER_HOUR:
                                    log.warning(
                                        "anomaly_checker.rate_limited",
                                        alerts_this_hour=len(_alert_count_window),
                                    )
                                    continue

                                # --- Layer 4: Notification journal dedup ---
                                msg = (
                                    f"🚨 **High-Severity Anomaly**\n\n"
                                    f"{desc}\n\n"
                                    f"Baseline: {baseline}\n"
                                    f"Cycle: `{_cycle.get('cycle_id', '?')[:16]}`"
                                )
                                if notify_journal.should_send(msg):
                                    try:
                                        await notify_router.notify(
                                            actions=None,
                                            intent_id=None,
                                            message=msg,
                                            category="suggest",
                                            action_class="anomaly_alert",
                                        )
                                    except Exception:
                                        log.exception(
                                            "anomaly_checker.notify_failed",
                                            category=cat,
                                        )
                                        continue
                                    _category_cache[cat] = now_ts
                                    _alert_count_window.append(now_ts)
                                    log.info(
                                        "anomaly_checker.sent",
                                        category=cat,
                                        description=desc[:100],
                                    )
                        _last_pos = _f.tell()
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("anomaly_checker.error")
                await asyncio.sleep(30)

    return asyncio.create_task(
        _check_anomalies(),
        name="coremind.anomaly_checker",
    )
