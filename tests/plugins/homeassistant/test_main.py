"""Unit tests for the Home Assistant plugin helpers."""

from __future__ import annotations

from coremind_plugin_homeassistant.main import (
    _entity_type,
    _ws_url,
    build_signed_event,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def test_entity_type_from_domain() -> None:
    assert _entity_type("sensor.living_room_temp") == "ha_sensor"
    assert _entity_type("binary_sensor.door") == "ha_binary_sensor"
    assert _entity_type("light.kitchen") == "ha_light"


def test_ws_url_http() -> None:
    assert _ws_url("http://localhost:8123") == "ws://localhost:8123/api/websocket"
    assert _ws_url("http://localhost:8123/") == "ws://localhost:8123/api/websocket"


def test_ws_url_https() -> None:
    assert _ws_url("https://ha.example.com") == "wss://ha.example.com/api/websocket"


def test_build_signed_event_shape() -> None:
    key = Ed25519PrivateKey.generate()
    event = build_signed_event(
        key,
        entity_id="sensor.temp",
        attribute="temperature",
        value=21.5,
        unit="°C",
    )
    assert event.source == "coremind.plugin.homeassistant"
    assert event.entity.type == "ha_sensor"
    assert event.entity.entity_id == "sensor.temp"
    assert event.attribute == "temperature"
    assert event.unit == "°C"
    assert event.signature  # non-empty bytes
