"""Approval gate lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from coremind.action.approvals import ApprovalGate
from coremind.action.journal import ActionJournal
from coremind.errors import ApprovalError
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import ActionProposal, Intent, InternalQuestion
from coremind.notify.adapters.dashboard import DashboardNotificationPort
from coremind.notify.port import ApprovalResponse, UserRef
from coremind.notify.router import DeferredNotificationError


class _SpyExecutor:
    """Captures execute() calls without doing anything."""

    def __init__(self) -> None:
        self.calls: list[Intent] = []

    async def execute(self, intent: Intent, *, notify: str = "immediate") -> None:  # type: ignore[override]
        self.calls.append(intent)


def _intent() -> Intent:
    return Intent(
        id="int-1",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        question=InternalQuestion(id="q", text="?"),
        proposed_action=ActionProposal(
            operation="op.x",
            parameters={},
            action_class="finance.transfer",
        ),
        salience=0.5,
        confidence=0.6,
        category="ask",
    )


async def _setup(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> tuple[ApprovalGate, IntentStore, DashboardNotificationPort, _SpyExecutor, ActionJournal]:
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    port = DashboardNotificationPort()
    spy = _SpyExecutor()
    gate = ApprovalGate(port, intents, journal, spy)  # type: ignore[arg-type]
    return gate, intents, port, spy, journal


async def test_request_sets_pending_and_notifies(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    gate, intents, port, _spy, _journal = await _setup(tmp_path, keypair)
    intent = _intent()
    await gate.request(intent)
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.status == "pending_approval"
    assert saved.expires_at is not None
    assert len(port.history) == 1
    assert port.history[0].category == "ask"


async def test_approve_triggers_executor(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    gate, intents, _port, spy, _journal = await _setup(tmp_path, keypair)
    intent = _intent()
    await gate.request(intent)

    resp = ApprovalResponse(
        intent_id=intent.id,
        decision="approve",
        responder=UserRef(id="u1"),
    )
    await gate.handle_response(resp)

    # Approving merely flips the intent to ``approved`` — execution is
    # carried out by the daemon's approved-intent dispatcher.
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.status == "approved"
    assert spy.calls == []

    dispatched = await gate.dispatch_approved()
    assert dispatched == 1
    assert len(spy.calls) == 1
    assert spy.calls[0].id == intent.id


async def test_deny_rejects(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    gate, intents, _port, spy, _journal = await _setup(tmp_path, keypair)
    intent = _intent()
    await gate.request(intent)
    await gate.handle_response(
        ApprovalResponse(intent_id=intent.id, decision="deny", responder=UserRef(id="u1"))
    )
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.status == "rejected"
    assert spy.calls == []


async def test_second_snooze_refused(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    gate, intents, _port, _spy, _journal = await _setup(tmp_path, keypair)
    intent = _intent()
    await gate.request(intent)

    await gate.handle_response(
        ApprovalResponse(
            intent_id=intent.id,
            decision="snooze",
            snooze_seconds=3600,
            responder=UserRef(id="u1"),
        )
    )
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.snooze_count == 1

    with pytest.raises(ApprovalError, match="snoozed once"):
        await gate.handle_response(
            ApprovalResponse(
                intent_id=intent.id,
                decision="snooze",
                snooze_seconds=3600,
                responder=UserRef(id="u1"),
            )
        )


async def test_expire_stale_never_auto_executes(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    port = DashboardNotificationPort()
    spy = _SpyExecutor()

    # Clock that jumps forward on the second call.
    calls = {"n": 0}

    def clock() -> datetime:
        calls["n"] += 1
        base = datetime(2025, 1, 1, tzinfo=UTC)
        if calls["n"] == 1:
            return base
        return base + timedelta(days=2)

    gate = ApprovalGate(port, intents, journal, spy, clock=clock)  # type: ignore[arg-type]
    intent = _intent()
    await gate.request(intent)  # expires_at = base + 24h
    expired = await gate.expire_stale()
    assert expired == 1
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.status == "expired"
    assert spy.calls == []


async def test_dispatch_approved_executes_only_approved(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    """``dispatch_approved`` must execute every ``approved`` intent and leave others alone."""
    gate, intents, _port, spy, _journal = await _setup(tmp_path, keypair)

    pending = _intent()
    pending.id = "int-pending"
    await intents.save(pending)

    approved = _intent()
    approved.id = "int-approved"
    approved.status = "approved"
    await intents.save(approved)

    rejected = _intent()
    rejected.id = "int-rejected"
    rejected.status = "rejected"
    await intents.save(rejected)

    dispatched = await gate.dispatch_approved()
    assert dispatched == 1
    assert {c.id for c in spy.calls} == {"int-approved"}


async def test_request_deferred_rolls_back_pending_state(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    """A quiet-hours deferral must not strand the intent in ``pending_approval``."""
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    spy = _SpyExecutor()

    class _DeferringPort:
        id = "deferring"
        supports_callbacks = False

        async def notify(self, **_: object) -> None:  # type: ignore[override]
            raise DeferredNotificationError("quiet hours")

        def subscribe_responses(self):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    gate = ApprovalGate(_DeferringPort(), intents, journal, spy)  # type: ignore[arg-type]
    intent = _intent()
    with pytest.raises(DeferredNotificationError):
        await gate.request(intent)
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.status == "pending"
    assert saved.expires_at is None
    assert spy.calls == []
