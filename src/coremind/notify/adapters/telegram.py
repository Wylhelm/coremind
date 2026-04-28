"""Telegram bot notification adapter.

Uses the Telegram Bot API over HTTPS.  Supports inline-button callbacks
for ``ask``-class approvals.

For Phase 3 the adapter is pragmatic — we rely on long-polling via
``getUpdates`` so no webhook infrastructure is required.  Production
deployments can swap this for webhook mode without changing the port.

The adapter is NOT started automatically by the daemon in Phase 3;
integration is wired via :class:`coremind.notify.adapters.telegram.build_port`
when the operator declares ``[notify.telegram]`` in ``config.toml``.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import aiohttp
import structlog

from coremind.errors import NotificationError
from coremind.notify.port import (
    ApprovalAction,
    ApprovalResponse,
    NotificationCategory,
    NotificationReceipt,
    UserRef,
)

log = structlog.get_logger(__name__)

_API_BASE = "https://api.telegram.org"
_DEFAULT_TIMEOUT = 30
# Telegram's hard cap on inline-button ``callback_data`` is 64 bytes.
_TELEGRAM_CALLBACK_MAX_BYTES = 64
# Number of bytes of randomness used to mint a per-intent callback token.
# 8 bytes -> 16 hex chars; well within ``_TELEGRAM_CALLBACK_MAX_BYTES``.
_TOKEN_BYTES = 8


class TelegramNotificationPort:
    """Telegram-backed :class:`~coremind.notify.port.NotificationPort`.

    Args:
        bot_token: The bot token obtained from ``@BotFather``.  MUST NOT
            be logged; it is stored only as a private attribute.
        chat_id: Target chat/user ID.
        session: Optional aiohttp session (useful for tests).
    """

    id: str = "telegram"
    supports_callbacks: bool = True

    def __init__(
        self,
        bot_token: str,
        chat_id: int | str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._session = session
        self._owned_session = session is None
        self._poll_offset: int | None = None
        # Per-port, in-memory token <-> intent_id maps.  Telegram
        # ``callback_data`` is hard-capped at 64 bytes; we therefore mint a
        # short opaque token for each ``ask`` notification and resolve it
        # back to the originating intent on response.
        self._intent_to_token: dict[str, str] = {}
        self._token_to_intent: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the aiohttp session, creating one on first use."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_DEFAULT_TIMEOUT),
            )
        return self._session

    async def close(self) -> None:
        """Close the owned session (no-op if a session was injected)."""
        if self._owned_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Port implementation
    # ------------------------------------------------------------------

    async def notify(
        self,
        *,
        message: str,
        category: NotificationCategory,
        actions: list[ApprovalAction] | None,
        intent_id: str | None,
        action_class: str | None = None,
    ) -> NotificationReceipt:
        """Send ``message`` via Telegram with optional inline buttons."""
        _ = action_class  # informational; Telegram has no per-class hint surface
        body: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": message,
        }
        if category == "ask" and actions:
            token = self._mint_token(intent_id) if intent_id is not None else ""
            body["reply_markup"] = _inline_keyboard(actions, token)

        data = await self._call("sendMessage", body)
        result = data.get("result", {})
        msg_id = result.get("message_id")
        if msg_id is None:
            raise NotificationError("Telegram sendMessage returned no message_id")
        return NotificationReceipt(
            port_id=self.id,
            channel_message_id=str(msg_id),
            sent_at=datetime.now(UTC),
        )

    async def subscribe_responses(self) -> AsyncIterator[ApprovalResponse]:
        """Yield approval responses collected via ``getUpdates`` long-polling."""
        while True:
            params: dict[str, Any] = {"timeout": 25}
            if self._poll_offset is not None:
                params["offset"] = self._poll_offset

            try:
                data = await self._call("getUpdates", params)
            except NotificationError:
                # Brief back-off so a stuck network does not pin the loop.
                await asyncio.sleep(5.0)
                continue

            for update in data.get("result", []):
                self._poll_offset = int(update["update_id"]) + 1
                response = _update_to_response(update, resolve_token=self._token_to_intent.get)
                if response is not None:
                    yield response

    # ------------------------------------------------------------------
    # Internal HTTP helper
    # ------------------------------------------------------------------

    def _mint_token(self, intent_id: str) -> str:
        """Return a short opaque token mapped to ``intent_id``.

        Reuses an existing token when the same intent id has been notified
        before, so repeat ``notify`` calls do not bloat the mapping.
        """
        existing = self._intent_to_token.get(intent_id)
        if existing is not None:
            return existing
        token = secrets.token_hex(_TOKEN_BYTES)
        self._intent_to_token[intent_id] = token
        self._token_to_intent[token] = intent_id
        return token

    async def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST *body* to ``/bot<token>/<method>`` and return parsed JSON."""
        url = f"{_API_BASE}/bot{self._token}/{method}"
        session = await self._get_session()
        try:
            async with session.post(url, json=body) as resp:
                payload: dict[str, Any] = await resp.json()
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise NotificationError(f"Telegram {method} request failed: {exc}") from exc
        if not payload.get("ok", False):
            raise NotificationError(f"Telegram {method} error: {payload.get('description', '?')}")
        return payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inline_keyboard(
    actions: list[ApprovalAction],
    token: str,
) -> dict[str, Any]:
    """Build a one-row inline keyboard carrying ``token`` in ``callback_data``.

    The payload format is ``{"t": <token>, "v": <value>}``.  A 16-char hex
    token plus the longest expected ``value`` (``"snooze:3600"``) keeps the
    serialised JSON well under Telegram's 64-byte limit.
    """
    buttons = []
    for action in actions:
        payload = json.dumps(
            {"t": token, "v": action.value},
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > _TELEGRAM_CALLBACK_MAX_BYTES:
            # Defensive — should not happen with our token sizing, but if a
            # caller passes an unexpectedly long ``value`` we drop the action
            # rather than emit a corrupt callback.
            log.warning(
                "telegram.callback_data_too_long",
                value=action.value,
                length=len(payload),
            )
            continue
        buttons.append({"text": action.label, "callback_data": payload})
    return {"inline_keyboard": [buttons]}


def _update_to_response(
    update: dict[str, Any],
    *,
    resolve_token: Callable[[str], str | None] | None = None,
) -> ApprovalResponse | None:
    """Convert a Telegram update into an :class:`ApprovalResponse`.

    Accepts both the current short-token form ``{"t": <token>, "v": <value>}``
    and the legacy ``{"intent_id": ..., "value": ...}`` form for
    backward-compatibility with on-the-wire callbacks emitted by older
    daemon versions or by tests calling this helper directly.

    Returns ``None`` for updates that are not button callbacks on an
    ``ask``-class notification, or whose token cannot be resolved.
    """
    callback = update.get("callback_query")
    if callback is None:
        return None
    try:
        data = json.loads(callback.get("data", "{}"))
    except (json.JSONDecodeError, TypeError):
        return None
    value = data.get("v") if "v" in data else data.get("value")

    intent_id: str | None = None
    token = data.get("t")
    if token is not None and resolve_token is not None:
        intent_id = resolve_token(token)
    if intent_id is None:
        intent_id = data.get("intent_id")

    if value is None or intent_id is None:
        return None

    from_user = callback.get("from", {})
    responder = UserRef(
        id=str(from_user.get("id", "unknown")),
        display_name=from_user.get("username", ""),
    )

    decision: str | None = None
    snooze_seconds: int | None = None
    if value in {"approve", "deny"}:
        decision = value
    elif value.startswith("snooze:"):
        try:
            snooze_seconds = int(value.split(":", 1)[1])
        except ValueError:
            return None
        decision = "snooze"

    if decision is None:
        return None
    return ApprovalResponse(
        intent_id=intent_id,
        decision=decision,  # type: ignore[arg-type]
        snooze_seconds=snooze_seconds,
        responder=responder,
    )
