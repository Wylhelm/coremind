"""Tests for :mod:`coremind.notify.adapters.telegram`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from coremind.errors import NotificationError
from coremind.notify.adapters import telegram as tg_mod
from coremind.notify.adapters.telegram import (
    TelegramNotificationPort,
    _inline_keyboard,
    _update_to_response,
)
from coremind.notify.port import ApprovalAction


class _FakeTelegram:
    """Minimal stub of the Telegram Bot API used by the adapter tests."""

    def __init__(self) -> None:
        self.send_calls: list[dict[str, Any]] = []
        self.get_updates_calls: list[dict[str, Any]] = []
        self.updates_batches: list[list[dict[str, Any]]] = []
        self.send_status: int = 200
        self.send_error: str | None = None

    def app(self) -> web.Application:
        async def send(request: web.Request) -> web.Response:
            body = await request.json()
            self.send_calls.append(body)
            if self.send_error is not None:
                return web.json_response(
                    {"ok": False, "description": self.send_error},
                    status=self.send_status,
                )
            return web.json_response(
                {"ok": True, "result": {"message_id": len(self.send_calls)}},
                status=self.send_status,
            )

        async def get_updates(request: web.Request) -> web.Response:
            body = await request.json()
            self.get_updates_calls.append(body)
            batch = self.updates_batches.pop(0) if self.updates_batches else []
            return web.json_response({"ok": True, "result": batch})

        app = web.Application()
        app.router.add_post("/bottok/sendMessage", send)
        app.router.add_post("/bottok/getUpdates", get_updates)
        return app


@pytest.fixture()
async def telegram_server() -> AsyncIterator[tuple[_FakeTelegram, TestServer]]:
    fake = _FakeTelegram()
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, server
    finally:
        await server.close()


async def _make_port(
    fake: _FakeTelegram, server: TestServer, *, monkeypatch: pytest.MonkeyPatch
) -> TelegramNotificationPort:
    """Build a port pointing at the fake HTTP server."""
    _ = fake
    monkeypatch.setattr(tg_mod, "_API_BASE", str(server.make_url("")))
    return TelegramNotificationPort("tok", 42)


async def test_notify_info_sends_plain_message(
    telegram_server: tuple[_FakeTelegram, TestServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, server = telegram_server
    port = await _make_port(fake, server, monkeypatch=monkeypatch)
    try:
        receipt = await port.notify(message="hello", category="info", actions=None, intent_id="i1")
    finally:
        await port.close()

    assert receipt.port_id == "telegram"
    assert receipt.channel_message_id == "1"
    assert fake.send_calls[0]["chat_id"] == 42
    assert fake.send_calls[0]["text"] == "hello"
    assert "reply_markup" not in fake.send_calls[0]


async def test_notify_ask_includes_inline_keyboard(
    telegram_server: tuple[_FakeTelegram, TestServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, server = telegram_server
    port = await _make_port(fake, server, monkeypatch=monkeypatch)
    actions = [
        ApprovalAction(label="Approve", value="approve"),
        ApprovalAction(label="Deny", value="deny"),
    ]
    try:
        await port.notify(message="q?", category="ask", actions=actions, intent_id="i1")
    finally:
        await port.close()

    body = fake.send_calls[0]
    assert "reply_markup" in body
    buttons = body["reply_markup"]["inline_keyboard"][0]
    assert len(buttons) == 2
    decoded = [json.loads(b["callback_data"]) for b in buttons]
    # New short-token format: {"t": <token>, "v": <value>}.
    assert {d["v"] for d in decoded} == {"approve", "deny"}
    # Both buttons share the same per-intent token.
    assert len({d["t"] for d in decoded}) == 1
    # And every callback_data fits Telegram's 64-byte limit.
    for b in buttons:
        assert len(b["callback_data"].encode("utf-8")) <= 64


async def test_notify_raises_on_telegram_error(
    telegram_server: tuple[_FakeTelegram, TestServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, server = telegram_server
    fake.send_status = 400
    fake.send_error = "bad chat"
    port = await _make_port(fake, server, monkeypatch=monkeypatch)
    try:
        with pytest.raises(NotificationError):
            await port.notify(message="hi", category="info", actions=None, intent_id="i1")
    finally:
        await port.close()


async def test_subscribe_responses_yields_parsed_callbacks(
    telegram_server: tuple[_FakeTelegram, TestServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, server = telegram_server
    fake.updates_batches.append(
        [
            {
                "update_id": 100,
                "callback_query": {
                    "from": {"id": 99, "username": "alice"},
                    "data": json.dumps({"intent_id": "i1", "value": "approve"}),
                },
            }
        ]
    )
    port = await _make_port(fake, server, monkeypatch=monkeypatch)
    try:
        stream = port.subscribe_responses()
        response = await anext(stream)
    finally:
        await stream.aclose()  # type: ignore[attr-defined]
        await port.close()

    assert response.intent_id == "i1"
    assert response.decision == "approve"
    assert response.responder.id == "99"
    assert fake.get_updates_calls[0]["timeout"] == 25


def test_update_to_response_handles_snooze() -> None:
    update = {
        "update_id": 1,
        "callback_query": {
            "from": {"id": 1, "username": "a"},
            "data": json.dumps({"intent_id": "i1", "value": "snooze:1800"}),
        },
    }
    r = _update_to_response(update)
    assert r is not None
    assert r.decision == "snooze"
    assert r.snooze_seconds == 1800


def test_update_to_response_rejects_malformed() -> None:
    assert _update_to_response({"update_id": 1}) is None
    bad = {
        "update_id": 1,
        "callback_query": {"from": {"id": 1}, "data": "not-json"},
    }
    assert _update_to_response(bad) is None


def test_inline_keyboard_keeps_callback_data_short() -> None:
    actions = [
        ApprovalAction(label="X", value="v"),
        ApprovalAction(label="Snooze", value="snooze:3600"),
    ]
    keyboard = _inline_keyboard(actions, token="deadbeefdeadbeef")  # noqa: S106 — test fixture token
    for button in keyboard["inline_keyboard"][0]:
        data = button["callback_data"]
        # Telegram's hard cap.
        assert len(data.encode("utf-8")) <= 64
        parsed = json.loads(data)
        assert parsed["t"] == "deadbeefdeadbeef"
        assert parsed["v"] in {"v", "snooze:3600"}


async def test_token_round_trip_resolves_intent_id(
    telegram_server: tuple[_FakeTelegram, TestServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notifying with a long intent id and replying with the issued token must resolve."""
    fake, server = telegram_server
    port = await _make_port(fake, server, monkeypatch=monkeypatch)
    long_intent_id = "i" * 80
    actions = [ApprovalAction(label="Approve", value="approve")]
    try:
        await port.notify(
            message="q?",
            category="ask",
            actions=actions,
            intent_id=long_intent_id,
        )
    finally:
        await port.close()

    sent_data = fake.send_calls[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    payload = json.loads(sent_data)
    response = _update_to_response(
        {
            "update_id": 1,
            "callback_query": {
                "from": {"id": 1, "username": "x"},
                "data": json.dumps(payload),
            },
        },
        resolve_token=port._token_to_intent.get,
    )
    assert response is not None
    assert response.intent_id == long_intent_id
    assert response.decision == "approve"
