"""Tests for :mod:`coremind_plugin_homeassistant.effector`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from coremind_plugin_homeassistant.effector import (
    ACCEPTED_OPERATIONS,
    HomeAssistantEffector,
)

from coremind.action.schemas import Action


def _action(
    op: str,
    params: dict[str, Any],
    *,
    action_class: str = "light",
) -> Action:
    return Action(
        id="act-1",
        intent_id="int-1",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        category="safe",
        operation=op,
        parameters=params,
        action_class=action_class,
        expected_outcome="done",
        confidence=0.95,
    )


class _RecordingApp:
    """Spin up a fake HA HTTP service that records every call."""

    def __init__(self, *, status: int = 200, body_text: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status
        self._body_text = body_text
        self._server: TestServer | None = None

    async def __aenter__(self) -> _RecordingApp:
        async def handle(request: web.Request) -> web.Response:
            body = await request.json()
            self.calls.append(
                {
                    "domain": request.match_info["domain"],
                    "service": request.match_info["service"],
                    "body": body,
                    "auth": request.headers.get("Authorization", ""),
                }
            )
            if self._body_text is not None:
                return web.Response(status=self._status, text=self._body_text)
            return web.json_response(
                [{"entity_id": body["entity_id"], "state": "on"}],
                status=self._status,
            )

        app = web.Application()
        app.router.add_post("/api/services/{domain}/{service}", handle)
        self._server = TestServer(app)
        await self._server.start_server()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            await self._server.close()

    def url(self) -> str:
        assert self._server is not None
        return str(self._server.make_url(""))


@pytest.fixture()
async def ha() -> AsyncIterator[_RecordingApp]:
    async with _RecordingApp() as app:
        yield app


async def test_turn_on_dispatches_to_light_domain(ha: _RecordingApp) -> None:
    effector = HomeAssistantEffector(ha.url(), "tok")
    try:
        result = await effector.invoke(
            _action("homeassistant.turn_on", {"entity_id": "light.kitchen"})
        )
    finally:
        await effector.close()

    assert result.status == "ok"
    assert result.reversed_by_operation == "homeassistant.turn_off"
    assert ha.calls[0]["domain"] == "light"
    assert ha.calls[0]["service"] == "turn_on"
    assert ha.calls[0]["body"] == {"entity_id": "light.kitchen"}
    assert ha.calls[0]["auth"] == "Bearer tok"


async def test_turn_off_reversal_pairs_with_turn_on(ha: _RecordingApp) -> None:
    effector = HomeAssistantEffector(ha.url(), "tok")
    try:
        result = await effector.invoke(
            _action("homeassistant.turn_off", {"entity_id": "switch.fan"})
        )
    finally:
        await effector.close()

    assert result.status == "ok"
    assert result.reversed_by_operation == "homeassistant.turn_on"
    assert ha.calls[0]["domain"] == "switch"


async def test_set_brightness_validates_range(ha: _RecordingApp) -> None:
    effector = HomeAssistantEffector(ha.url(), "tok")
    try:
        bad = await effector.invoke(
            _action(
                "homeassistant.set_brightness",
                {"entity_id": "light.x", "brightness": 999},
            )
        )
        ok = await effector.invoke(
            _action(
                "homeassistant.set_brightness",
                {"entity_id": "light.x", "brightness": 128},
            )
        )
    finally:
        await effector.close()

    assert bad.status == "permanent_failure"
    assert "out of range" in bad.message
    assert ok.status == "ok"
    assert ha.calls[0]["body"] == {"entity_id": "light.x", "brightness": 128}
    assert ha.calls[0]["domain"] == "light"


async def test_set_temperature_hits_climate_domain(ha: _RecordingApp) -> None:
    effector = HomeAssistantEffector(ha.url(), "tok")
    try:
        result = await effector.invoke(
            _action(
                "homeassistant.set_temperature",
                {"entity_id": "climate.bedroom", "temperature": 21.5},
                action_class="hvac",
            )
        )
    finally:
        await effector.close()

    assert result.status == "ok"
    assert ha.calls[0]["domain"] == "climate"
    assert ha.calls[0]["service"] == "set_temperature"
    assert ha.calls[0]["body"]["temperature"] == 21.5


async def test_unknown_operation_returns_permanent_failure(ha: _RecordingApp) -> None:
    effector = HomeAssistantEffector(ha.url(), "tok")
    try:
        result = await effector.invoke(_action("homeassistant.explode", {"entity_id": "light.x"}))
    finally:
        await effector.close()

    assert result.status == "permanent_failure"
    assert "unsupported operation" in result.message
    assert ha.calls == []


async def test_http_error_mapped_to_permanent_failure() -> None:
    async with _RecordingApp(status=500, body_text="HA down") as ha:
        effector = HomeAssistantEffector(ha.url(), "tok")
        try:
            result = await effector.invoke(
                _action("homeassistant.turn_on", {"entity_id": "light.kitchen"})
            )
        finally:
            await effector.close()

    assert result.status == "permanent_failure"
    assert "http_500" in result.message


def test_accepted_operations_matches_manifest() -> None:
    assert set(ACCEPTED_OPERATIONS) == {
        "homeassistant.turn_on",
        "homeassistant.turn_off",
        "homeassistant.set_brightness",
        "homeassistant.set_temperature",
        "homeassistant.trigger_scene",
    }
