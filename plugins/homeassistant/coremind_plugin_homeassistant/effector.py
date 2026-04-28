"""Home Assistant effector — implements :class:`EffectorPort` via HA REST API.

The effector calls Home Assistant's ``/api/services/{domain}/{service}`` endpoints
to carry out actions proposed by L6.  It mirrors the operation vocabulary
declared in ``manifest.toml`` and validates parameters against small
per-operation schemas before dispatching.

Operations
----------

- ``homeassistant.turn_on`` / ``homeassistant.turn_off`` — light / switch.
- ``homeassistant.set_brightness`` — light (0-255).
- ``homeassistant.set_temperature`` — climate entity.
- ``homeassistant.trigger_scene`` — scene entity.

Every method returns a signed :class:`ActionResult` whose ``reversed_by_operation``
is populated whenever a natural reversal exists (e.g. ``turn_on`` → ``turn_off``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import aiohttp
import structlog

from coremind.action.schemas import Action, ActionResult

log = structlog.get_logger(__name__)

_BRIGHTNESS_MIN: int = 0
_BRIGHTNESS_MAX: int = 255
_TEMP_MIN: float = -50.0
_TEMP_MAX: float = 80.0


class HomeAssistantEffector:
    """Bidirectional-plugin effector targeting a single HA instance.

    Args:
        base_url: Home Assistant HTTP base URL (e.g. ``http://localhost:8123``).
        access_token: Long-lived access token.
        session: Optional aiohttp session (useful for tests).
    """

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = access_token
        self._session = session
        self._owned = session is None

    async def close(self) -> None:
        """Close the owned aiohttp session if one was created."""
        if self._owned and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return an aiohttp session, creating one on first use."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    # ------------------------------------------------------------------
    # EffectorPort
    # ------------------------------------------------------------------

    async def invoke(self, action: Action) -> ActionResult:
        """Dispatch ``action`` to Home Assistant and return the outcome."""
        try:
            domain, service, body, reversal = _resolve_operation(action)
        except ValueError as exc:
            return _fail(action, f"invalid_parameters: {exc}")

        url = f"{self._base_url}/api/services/{domain}/{service}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        session = await self._get_session()
        try:
            async with session.post(url, json=body, headers=headers) as resp:
                if resp.status >= 400:  # noqa: PLR2004 — HTTP convention
                    detail = await resp.text()
                    return _fail(action, f"http_{resp.status}: {detail[:200]}")
                payload = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as exc:
            return ActionResult(
                action_id=action.id,
                status="transient_failure",
                message=f"network_error: {exc}",
                completed_at=datetime.now(UTC),
            )

        output: dict[str, Any] | None = None
        if isinstance(payload, list):
            output = {"states": payload}
        elif isinstance(payload, dict):
            output = payload

        return ActionResult(
            action_id=action.id,
            status="ok",
            message=f"{domain}.{service} dispatched",
            output=output,
            completed_at=datetime.now(UTC),
            reversed_by_operation=reversal.get("operation") if reversal else None,
            reversal_parameters=reversal.get("parameters") if reversal else None,
        )


# ---------------------------------------------------------------------------
# Operation routing
# ---------------------------------------------------------------------------


def _resolve_operation(
    action: Action,
) -> tuple[str, str, dict[str, Any], dict[str, Any] | None]:
    """Return ``(domain, service, body, reversal)`` for ``action.operation``."""
    op = action.operation
    params = dict(action.parameters)
    entity_id = params.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        raise ValueError("entity_id is required and must be a non-empty string")

    if op == "homeassistant.turn_on":
        domain = _domain_of(entity_id)
        return (
            domain,
            "turn_on",
            {"entity_id": entity_id},
            {
                "operation": "homeassistant.turn_off",
                "parameters": {"entity_id": entity_id},
            },
        )
    if op == "homeassistant.turn_off":
        domain = _domain_of(entity_id)
        return (
            domain,
            "turn_off",
            {"entity_id": entity_id},
            {
                "operation": "homeassistant.turn_on",
                "parameters": {"entity_id": entity_id},
            },
        )
    if op == "homeassistant.set_brightness":
        brightness = params.get("brightness")
        if not isinstance(brightness, (int, float)):
            raise ValueError("brightness must be numeric")
        b = int(brightness)
        if not _BRIGHTNESS_MIN <= b <= _BRIGHTNESS_MAX:
            raise ValueError(f"brightness {b} out of range [{_BRIGHTNESS_MIN},{_BRIGHTNESS_MAX}]")
        return "light", "turn_on", {"entity_id": entity_id, "brightness": b}, None
    if op == "homeassistant.set_temperature":
        temperature = params.get("temperature")
        if not isinstance(temperature, (int, float)):
            raise ValueError("temperature must be numeric")
        t = float(temperature)
        if not _TEMP_MIN <= t <= _TEMP_MAX:
            raise ValueError(f"temperature {t} out of range [{_TEMP_MIN},{_TEMP_MAX}]")
        return "climate", "set_temperature", {"entity_id": entity_id, "temperature": t}, None
    if op == "homeassistant.trigger_scene":
        return "scene", "turn_on", {"entity_id": entity_id}, None

    raise ValueError(f"unsupported operation {op!r}")


def _domain_of(entity_id: str) -> str:
    """Return the HA domain portion of ``entity_id`` (e.g. ``light.kitchen`` → ``light``)."""
    domain, _, rest = entity_id.partition(".")
    if not domain or not rest:
        raise ValueError(f"entity_id {entity_id!r} has no domain")
    return domain


def _fail(action: Action, message: str) -> ActionResult:
    """Return a permanent-failure :class:`ActionResult` for ``action``."""
    return ActionResult(
        action_id=action.id,
        status="permanent_failure",
        message=message,
        completed_at=datetime.now(UTC),
    )


# Accepted operation names — mirrored in manifest.toml.
ACCEPTED_OPERATIONS: tuple[str, ...] = (
    "homeassistant.turn_on",
    "homeassistant.turn_off",
    "homeassistant.set_brightness",
    "homeassistant.set_temperature",
    "homeassistant.trigger_scene",
)
