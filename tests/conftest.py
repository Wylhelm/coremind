"""Shared pytest fixtures for the CoreMind test suite."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


@pytest.fixture()
def private_key() -> Ed25519PrivateKey:
    """Generate a fresh in-memory ed25519 private key."""
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def public_key(private_key: Ed25519PrivateKey) -> Ed25519PublicKey:
    """Derive the public key from the fixture private key."""
    return private_key.public_key()


@pytest.fixture()
def keypair(
    private_key: Ed25519PrivateKey,
    public_key: Ed25519PublicKey,
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Return a consistent (private_key, public_key) ed25519 pair."""
    return private_key, public_key
