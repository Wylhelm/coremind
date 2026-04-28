"""Tests for src/coremind/crypto/signatures.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ec import (
    SECP256R1,
)
from cryptography.hazmat.primitives.asymmetric.ec import (
    generate_private_key as generate_ec_private_key,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from coremind.crypto.signatures import (
    canonical_json,
    ensure_daemon_keypair,
    ensure_plugin_keypair,
    load_public_key,
    sign,
    verify,
)
from coremind.errors import KeyManagementError

# ---------------------------------------------------------------------------
# sign / verify
# ---------------------------------------------------------------------------


def test_sign_returns_64_byte_signature(
    keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey],
) -> None:
    private_key, _ = keypair
    payload = b"hello world"
    sig = sign(payload, private_key)
    assert len(sig) == 64


def test_verify_accepts_valid_signature(
    keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey],
) -> None:
    private_key, public_key = keypair
    payload = b"some event payload"
    sig = sign(payload, private_key)

    result = verify(payload, sig, public_key)

    assert result is True


def test_verify_rejects_tampered_payload(
    keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey],
) -> None:
    private_key, public_key = keypair
    original = b"original payload"
    sig = sign(original, private_key)
    tampered = b"tampered payload"

    result = verify(tampered, sig, public_key)

    assert result is False


def test_verify_rejects_tampered_signature(
    keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey],
) -> None:
    private_key, public_key = keypair
    payload = b"payload"
    sig = sign(payload, private_key)
    corrupted_sig = bytes([sig[0] ^ 0xFF]) + sig[1:]

    result = verify(payload, corrupted_sig, public_key)

    assert result is False


def test_verify_rejects_wrong_key(keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]) -> None:
    private_key, _ = keypair
    payload = b"payload"
    sig = sign(payload, private_key)

    other_private = Ed25519PrivateKey.generate()
    other_public = other_private.public_key()

    result = verify(payload, sig, other_public)

    assert result is False


def test_sign_empty_bytes(keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]) -> None:
    private_key, public_key = keypair
    sig = sign(b"", private_key)

    assert verify(b"", sig, public_key) is True


# ---------------------------------------------------------------------------
# canonical_json
# ---------------------------------------------------------------------------


def test_canonical_json_is_deterministic() -> None:
    obj: dict[str, object] = {"z": 1, "a": 2, "m": 3}
    first = canonical_json(obj)
    second = canonical_json(obj)

    assert first == second


def test_canonical_json_orders_keys_lexicographically() -> None:
    obj: dict[str, object] = {"z": 1, "a": 2}
    result = canonical_json(obj)

    assert result == b'{"a":2,"z":1}'


def test_canonical_json_handles_nested_objects() -> None:
    obj: dict[str, object] = {"outer": {"z": 9, "a": 1}, "b": True}
    first = canonical_json(obj)
    reordered: dict[str, object] = {"b": True, "outer": {"a": 1, "z": 9}}
    second = canonical_json(reordered)

    assert first == second


def test_canonical_json_returns_bytes() -> None:
    result = canonical_json({"key": "value"})

    assert isinstance(result, bytes)


def test_canonical_json_is_valid_utf8() -> None:
    obj: dict[str, object] = {"msg": "héllo wörld"}
    result = canonical_json(obj)

    decoded = result.decode("utf-8")
    assert "héllo wörld" in decoded


# ---------------------------------------------------------------------------
# sign + canonical_json round-trip
# ---------------------------------------------------------------------------


def test_canonical_json_signature_roundtrip(
    keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey],
) -> None:
    private_key, public_key = keypair
    event: dict[str, object] = {
        "entity": {"id": "host:local", "type": "host"},
        "attribute": "cpu_percent",
        "value": 42.5,
    }

    payload = canonical_json(event)
    sig = sign(payload, private_key)

    # Re-canonicalise with different key order — must produce same bytes
    event_reordered: dict[str, object] = {
        "value": 42.5,
        "attribute": "cpu_percent",
        "entity": {"type": "host", "id": "host:local"},
    }
    payload_reordered = canonical_json(event_reordered)

    assert payload == payload_reordered
    assert verify(payload_reordered, sig, public_key) is True


def test_canonical_json_signature_fails_after_value_change(
    keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey],
) -> None:
    private_key, public_key = keypair
    event: dict[str, object] = {"attribute": "cpu_percent", "value": 42.5}
    payload = canonical_json(event)
    sig = sign(payload, private_key)

    tampered_event: dict[str, object] = {"attribute": "cpu_percent", "value": 99.9}
    tampered_payload = canonical_json(tampered_event)

    assert verify(tampered_payload, sig, public_key) is False


# ---------------------------------------------------------------------------
# Key management helpers
# ---------------------------------------------------------------------------


def test_ensure_daemon_keypair_creates_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    key = ensure_daemon_keypair()

    assert isinstance(key, Ed25519PrivateKey)
    key_file = tmp_path / ".coremind" / "keys" / "daemon.ed25519"
    pub_key_file = tmp_path / ".coremind" / "keys" / "daemon.ed25519.pub"
    assert key_file.exists()
    assert pub_key_file.exists()


def test_ensure_daemon_keypair_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    key1 = ensure_daemon_keypair()
    key2 = ensure_daemon_keypair()

    pub1 = key1.public_key().public_bytes_raw()
    pub2 = key2.public_key().public_bytes_raw()
    assert pub1 == pub2


def test_ensure_plugin_keypair_creates_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    key = ensure_plugin_keypair("test-plugin")

    assert isinstance(key, Ed25519PrivateKey)
    key_file = tmp_path / ".coremind" / "keys" / "plugins" / "test-plugin.ed25519"
    pub_key_file = tmp_path / ".coremind" / "keys" / "plugins" / "test-plugin.ed25519.pub"
    assert key_file.exists()
    assert pub_key_file.exists()


def test_ensure_plugin_keypair_rejects_empty_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    with pytest.raises(ValueError, match="plugin_id must not be empty"):
        ensure_plugin_keypair("")


def test_ensure_plugin_keypairs_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    key_a = ensure_plugin_keypair("plugin-a")
    key_b = ensure_plugin_keypair("plugin-b")

    pub_a = key_a.public_key().public_bytes_raw()
    pub_b = key_b.public_key().public_bytes_raw()
    assert pub_a != pub_b


# ---------------------------------------------------------------------------
# ensure_plugin_keypair — path traversal and idempotency
# ---------------------------------------------------------------------------


def test_ensure_plugin_keypair_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    key1 = ensure_plugin_keypair("my-plugin")
    key2 = ensure_plugin_keypair("my-plugin")

    pub1 = key1.public_key().public_bytes_raw()
    pub2 = key2.public_key().public_bytes_raw()
    assert pub1 == pub2


def test_ensure_plugin_keypair_rejects_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    with pytest.raises(ValueError, match="plugin_id must contain only"):
        ensure_plugin_keypair("../../daemon")


def test_ensure_plugin_keypair_rejects_slash_in_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    with pytest.raises(ValueError, match="plugin_id must contain only"):
        ensure_plugin_keypair("sub/plugin")


# ---------------------------------------------------------------------------
# load_public_key
# ---------------------------------------------------------------------------


def test_load_public_key_happy_path(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    pub_key = private_key.public_key()
    pub_pem = pub_key.public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    )
    key_file = tmp_path / "test.pub"
    key_file.write_bytes(pub_pem)

    loaded = load_public_key(key_file)

    assert isinstance(loaded, Ed25519PublicKey)
    loaded_pem = loaded.public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    )
    assert loaded_pem == pub_pem


def test_load_public_key_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(KeyManagementError, match="Failed to load public key"):
        load_public_key(tmp_path / "nonexistent.pub")


def test_load_public_key_wrong_key_type(tmp_path: Path) -> None:
    ec_private = generate_ec_private_key(SECP256R1())
    ec_pub_pem = ec_private.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    )
    key_file = tmp_path / "ec.pub"
    key_file.write_bytes(ec_pub_pem)

    with pytest.raises(KeyManagementError, match="not an ed25519 public key"):
        load_public_key(key_file)


# ---------------------------------------------------------------------------
# Key file permission enforcement
# ---------------------------------------------------------------------------


def test_ensure_daemon_keypair_raises_when_key_has_open_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    ensure_daemon_keypair()  # create key file with correct 0o600 permissions
    key_file = tmp_path / ".coremind" / "keys" / "daemon.ed25519"
    key_file.chmod(0o644)  # weaken permissions to simulate misconfiguration

    with pytest.raises(KeyManagementError, match="permissions too open"):
        ensure_daemon_keypair()


def test_ensure_plugin_keypair_raises_when_key_has_open_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    ensure_plugin_keypair("my-plugin")  # create key file with correct 0o600 permissions
    key_file = tmp_path / ".coremind" / "keys" / "plugins" / "my-plugin.ed25519"
    key_file.chmod(0o644)  # weaken permissions to simulate misconfiguration

    with pytest.raises(KeyManagementError, match="permissions too open"):
        ensure_plugin_keypair("my-plugin")
