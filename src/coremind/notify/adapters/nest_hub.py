"""Nest Hub notification adapter.

Speaks text through the Google Nest Hub using the gbot-say.sh TTS script,
giving CoreMind a physical voice in the room.

This is Pillar #3 (Physical Presence) of CoreMind v0.3.0.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import structlog

from coremind.errors import NotificationError
from coremind.notify.port import (
    ApprovalAction,
    NotificationCategory,
    NotificationReceipt,
)

log = structlog.get_logger(__name__)

DEFAULT_TTS_SCRIPT = os.path.expanduser("~/workspace/home-assistant/scripts/gbot-say.sh")
DEFAULT_CAST_SCRIPT = os.path.expanduser("~/workspace/home-assistant/scripts/cast-dashboard.sh")


class NestHubAdapter:
    """NotificationPort that speaks through a Google Nest Hub.

    Uses gbot-say.sh for TTS delivery.  The adapter is callback-incapable
    (no way to get approval responses from a voice-only device), so
    ``ask``-class notifications are downgraded to ``info`` with a note
    that the user should check Telegram for action buttons.

    Attributes:
        id: ``"nest_hub"``
        supports_callbacks: ``False``
    """

    id: str = "nest_hub"
    supports_callbacks: bool = False

    def __init__(
        self,
        *,
        tts_script: str | None = None,
        cast_script: str | None = None,
        min_urgency: float = 0.7,
    ) -> None:
        self._tts_script = Path(tts_script or DEFAULT_TTS_SCRIPT)
        self._cast_script = Path(cast_script or DEFAULT_CAST_SCRIPT)
        self._min_urgency = min_urgency

    async def notify(
        self,
        *,
        message: str,
        category: NotificationCategory,
        actions: list[ApprovalAction] | None = None,
        intent_id: str | None = None,
        action_class: str | None = None,
    ) -> NotificationReceipt:
        """Speak ``message`` through the Nest Hub.

        ``ask``-class notifications are spoken with a prefix noting that
        action buttons are available on Telegram.
        """
        if not self._tts_script.exists():
            raise NotificationError(f"TTS script not found: {self._tts_script}")

        # Build the spoken text
        spoken = message
        if category == "ask":
            spoken = (
                f"{message} — pour approuver ou refuser, regarde Telegram."
            )

        try:
            await self._speak(spoken)
        except Exception as exc:
            raise NotificationError(f"Nest Hub TTS failed: {exc}") from exc

        return NotificationReceipt(
            port_id=self.id,
            channel_message_id=f"nest_hub:{datetime.now(UTC).isoformat()}",
            sent_at=datetime.now(UTC),
        )

    async def display_url(self, url: str, duration_seconds: int = 30) -> bool:
        """Cast a URL to the Nest Hub display.

        Args:
            url: The URL to display.
            duration_seconds: How long to show it (honored by kiosk mode).

        Returns:
            True if casting succeeded.
        """
        if not self._cast_script.exists():
            log.warning("nest_hub.cast_script_not_found", path=str(self._cast_script))
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(self._cast_script),
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=float(duration_seconds + 10)
            )
            if proc.returncode != 0:
                log.warning(
                    "nest_hub.cast_failed",
                    returncode=proc.returncode,
                    stderr=stderr.decode()[:200] if stderr else "",
                )
                return False
            log.info("nest_hub.cast_success", url=url)
            return True
        except TimeoutError:
            log.warning("nest_hub.cast_timeout", url=url)
            return False
        except Exception as exc:
            log.error("nest_hub.cast_error", error=str(exc))
            return False

    async def _speak(self, text: str) -> None:
        """Execute gbot-say.sh with the given text."""
        proc = await asyncio.create_subprocess_exec(
            "bash",
            str(self._tts_script),
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=30.0
        )
        if proc.returncode != 0:
            err = stderr.decode()[:200] if stderr else "unknown error"
            raise NotificationError(f"gbot-say.sh failed: {err}")
        log.info("nest_hub.spoke", text=text[:80])
