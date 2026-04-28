"""Hash-chain audit journal tests."""

# ruff: noqa: ASYNC240 — small sync I/O inside async tests is fine

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from coremind.action.journal import ActionJournal, _compute_entry_hash, _JournalEntry
from coremind.action.schemas import Action, ActionResult
from coremind.crypto.signatures import sign
from coremind.errors import JournalError


def _make_action(intent_id: str = "int-1", action_id: str = "act-1") -> Action:
    return Action(
        id=action_id,
        intent_id=intent_id,
        timestamp=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        category="safe",
        operation="plugin.x.op",
        parameters={"k": "v"},
        action_class="light",
        expected_outcome="light on",
        confidence=0.95,
    )


@pytest.fixture()
def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.log"


async def test_genesis_load(
    journal_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(journal_path, priv, pub)
    await journal.load()
    report = await journal.verify()
    assert report.ok
    assert report.entries == 0


async def test_append_and_verify_chain(
    journal_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(journal_path, priv, pub)
    await journal.load()
    a1 = _make_action(action_id="a1")
    a2 = _make_action(action_id="a2")
    await journal.append(a1)
    await journal.append(a2)
    await journal.append_meta("approval.requested", {"intent_id": "int-1"})

    report = await journal.verify()
    assert report.ok, report.reason
    assert report.entries == 3


async def test_file_chmod_600(
    journal_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(journal_path, priv, pub)
    await journal.load()
    await journal.append(_make_action())
    mode = journal_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


async def test_update_result_preserves_chain(
    journal_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(journal_path, priv, pub)
    await journal.load()
    a1 = _make_action(action_id="a1")
    a2 = _make_action(action_id="a2")
    await journal.append(a1)
    await journal.append(a2)

    a1.result = ActionResult(
        action_id="a1",
        status="ok",
        message="done",
        completed_at=datetime(2025, 1, 1, 12, 0, 1, tzinfo=UTC),
    )
    await journal.update_result(a1)

    report = await journal.verify()
    assert report.ok, report.reason
    assert report.entries == 2


async def test_tampering_is_detected(
    journal_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(journal_path, priv, pub)
    await journal.load()
    await journal.append(_make_action(action_id="a1"))
    await journal.append(_make_action(action_id="a2"))

    # Tamper: flip one character in the middle line.
    lines = journal_path.read_text().splitlines()
    lines[0] = lines[0].replace("plugin.x.op", "plugin.x.EVIL")
    journal_path.write_text("\n".join(lines) + "\n")

    report = await journal.verify()
    assert not report.ok
    assert report.broken_at == 1


async def test_append_without_load_raises(
    journal_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(journal_path, priv, pub)
    with pytest.raises(JournalError):
        await journal.append(_make_action())


async def test_action_signature_is_verified(
    journal_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    """Tampering with an embedded ``signature`` (without changing other fields)
    must be detected by ``verify`` even though the daemon-level entry hash is
    valid in this scenario.
    """
    priv, pub = keypair
    journal = ActionJournal(journal_path, priv, pub)
    await journal.load()
    await journal.append(_make_action(action_id="a1"))

    raw_lines = journal_path.read_text().splitlines()
    entry = _JournalEntry.model_validate_json(raw_lines[0])
    payload = dict(entry.payload)
    # Replace the action signature with a structurally-valid but invalid one.
    payload["signature"] = "AAAA" * 16
    new_hash = _compute_entry_hash(
        seq=entry.seq,
        timestamp=entry.timestamp,
        prev_hash=entry.prev_hash,
        kind=entry.kind,
        payload=payload,
    )
    new_sig = base64.b64encode(sign(new_hash.encode("ascii"), priv)).decode("ascii")
    rebuilt = entry.model_copy(
        update={"payload": payload, "entry_hash": new_hash, "signature": new_sig}
    )
    journal_path.write_text(rebuilt.model_dump_json() + "\n")

    report = await journal.verify()
    assert not report.ok
    assert report.broken_at == 1
    assert report.reason is not None
    assert "action signature" in report.reason
