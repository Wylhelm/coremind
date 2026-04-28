"""Unit tests for the Gmail IMAP plugin helpers."""

from __future__ import annotations

from coremind_plugin_gmail_imap.main import (
    _bool_value,
    _str_value,
    build_signed_event,
    parse_headers,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_SAMPLE = (
    b"From: alice@example.com\r\n"
    b"To: bob@example.com\r\n"
    b"Subject: Hello World\r\n"
    b"Message-ID: <abc123@example.com>\r\n"
    b'Content-Type: multipart/mixed; boundary="---"\r\n'
    b"\r\n"
)


def test_parse_headers_extracts_core_fields() -> None:
    mid, subject, sender, has_att = parse_headers(_SAMPLE)
    assert mid == "abc123@example.com"
    assert subject == "Hello World"
    assert sender == "alice@example.com"
    assert has_att is True


def test_parse_headers_no_attachment() -> None:
    raw = _SAMPLE.replace(b"multipart/mixed", b"text/plain")
    _, _, _, has_att = parse_headers(raw)
    assert has_att is False


def test_build_signed_event_shape() -> None:
    key = Ed25519PrivateKey.generate()
    event = build_signed_event(
        key,
        email_id="abc123@example.com",
        attribute="subject",
        value=_str_value("Hello"),
    )
    assert event.source == "coremind.plugin.gmail-imap"
    assert event.entity.type == "email"
    assert event.entity.entity_id == "abc123@example.com"
    assert event.signature


def test_value_wrappers() -> None:
    assert _str_value("x").string_value == "x"
    assert _bool_value(True).bool_value is True
