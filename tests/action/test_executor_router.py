"""Executor + ActionRouter integration tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from coremind.action.approvals import ApprovalGate
from coremind.action.executor import EffectorPort, Executor
from coremind.action.journal import ActionJournal
from coremind.action.notification_journal import NotificationJournal
from coremind.action.router import ActionRouter
from coremind.action.schemas import Action, ActionResult
from coremind.errors import ActionError
from coremind.intention.persistence import IntentStore
from coremind.intention.schemas import ActionProposal, Intent, InternalQuestion
from coremind.notify.adapters.dashboard import DashboardNotificationPort


class _FakeEffector:
    """Records invocations and returns a stubbed ok result."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.calls: list[Action] = []
        self.raise_exc = raise_exc

    async def invoke(self, action: Action) -> ActionResult:
        self.calls.append(action)
        if self.raise_exc is not None:
            raise self.raise_exc
        return ActionResult(
            action_id=action.id,
            status="ok",
            message="did it",
            completed_at=datetime(2025, 1, 1, tzinfo=UTC),
        )


def _intent(
    *,
    category: str = "safe",
    action_class: str = "light",
    with_action: bool = True,
) -> Intent:
    q = InternalQuestion(id="q-1", text="Should the light be on?")
    proposal = (
        ActionProposal(
            operation="plugin.homeassistant.turn_on",
            parameters={"entity_id": "light.kitchen"},
            expected_outcome="light on",
            action_class=action_class,
        )
        if with_action
        else None
    )
    return Intent(
        id=f"int-{action_class}-{category}",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        question=q,
        proposed_action=proposal,
        salience=0.7,
        confidence=0.95 if category == "safe" else 0.7,
        category=category,  # type: ignore[arg-type]
        status="pending",
    )


@pytest.fixture()
async def wired(tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]):
    """Return a fully wired (intents, journal, executor, router, port) tuple."""
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    port = DashboardNotificationPort()
    effector = _FakeEffector()

    def resolver(operation: str) -> EffectorPort | None:
        return effector

    executor = Executor(
        journal,
        intents,
        resolver,
        notify_port=port,
        notify_journal=NotificationJournal(tmp_path / "notify_journal.jsonl"),
        suggest_grace=timedelta(milliseconds=10),
    )
    approvals = ApprovalGate(port, intents, journal, executor)
    router = ActionRouter(executor, approvals, intents, journal)
    return intents, journal, executor, router, port, effector


async def test_safe_intent_dispatches_silently(wired: tuple) -> None:  # type: ignore[type-arg]
    intents, _journal, _exec, router, port, effector = wired
    intent = _intent(category="safe", action_class="light")

    await router.route(intent)

    assert len(effector.calls) == 1
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.status == "done"
    assert port.history == []  # silent


async def test_suggest_intent_executes_after_grace(wired: tuple) -> None:  # type: ignore[type-arg]
    intents, _j, _exec, router, port, effector = wired
    intent = _intent(category="suggest", action_class="hvac")

    await router.route(intent)

    assert len(effector.calls) == 1
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.status == "done"
    # suggest notifies the user
    assert any(n.category == "suggest" for n in port.history)


async def test_suggest_intent_cancelled_in_grace(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    port = DashboardNotificationPort()
    effector = _FakeEffector()
    executor = Executor(
        journal,
        intents,
        lambda _op: effector,
        notify_port=port,
        notify_journal=NotificationJournal(tmp_path / "notify_journal.jsonl"),
        suggest_grace=timedelta(milliseconds=50),
    )
    approvals = ApprovalGate(port, intents, journal, executor)
    router = ActionRouter(executor, approvals, intents, journal)

    intent = _intent(category="suggest", action_class="hvac")

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.005)
        executor.cancel(intent.id)

    await asyncio.gather(router.route(intent), _cancel_soon())

    assert effector.calls == []
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.status == "rejected"


async def test_ask_intent_requests_approval(wired: tuple) -> None:  # type: ignore[type-arg]
    intents, _j, _exec, router, port, effector = wired
    intent = _intent(category="ask", action_class="email.outbound")

    await router.route(intent)

    assert effector.calls == []
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.status == "pending_approval"
    assert any(n.category == "ask" for n in port.history)


async def test_forced_class_override_is_blocked(wired: tuple) -> None:  # type: ignore[type-arg]
    intents, journal, _exec, router, _port, effector = wired
    # Plugin tries to sneak a "safe" category for an email.outbound class.
    intent = _intent(category="safe", action_class="email.outbound")

    await router.route(intent)

    # Override: category flipped to ask, approval requested, executor NOT called.
    assert effector.calls == []
    saved = await intents.get(intent.id)
    assert saved is not None
    assert saved.category == "ask"
    assert saved.status == "pending_approval"

    # security meta-event must be in the journal
    report = await journal.verify()
    assert report.ok
    text = (_j_path := journal._path).read_text()
    assert "security.category.override_blocked" in text


class _ReversibleEffector:
    """Effector whose ``invoke`` result carries a reversal operation."""

    def __init__(self) -> None:
        self.calls: list[Action] = []

    async def invoke(self, action: Action) -> ActionResult:
        self.calls.append(action)
        # Reversal resolves to the *opposite* homeassistant operation.
        opposite = {
            "plugin.homeassistant.turn_on": "plugin.homeassistant.turn_off",
            "plugin.homeassistant.turn_off": "plugin.homeassistant.turn_on",
        }.get(action.operation)
        return ActionResult(
            action_id=action.id,
            status="ok",
            message="did it",
            completed_at=datetime(2025, 1, 1, tzinfo=UTC),
            reversed_by_operation=opposite,
            reversal_parameters=dict(action.parameters) if opposite else None,
        )


async def test_reverse_dispatches_reversal_operation(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    port = DashboardNotificationPort()
    effector = _ReversibleEffector()
    executor = Executor(
        journal,
        intents,
        lambda _op: effector,
        notify_port=port,
        notify_journal=NotificationJournal(tmp_path / "notify_journal.jsonl"),
        suggest_grace=timedelta(milliseconds=10),
    )
    approvals = ApprovalGate(port, intents, journal, executor)
    router = ActionRouter(executor, approvals, intents, journal)

    intent = _intent(category="safe", action_class="light")
    await router.route(intent)

    assert len(effector.calls) == 1
    original = effector.calls[0]

    reversal_action = await executor.reverse(original.id)

    assert len(effector.calls) == 2
    assert reversal_action.operation == "plugin.homeassistant.turn_off"
    assert reversal_action.parameters == original.parameters
    report = await journal.verify()
    assert report.ok
    # action.reversed meta-event must be in the chain.
    entries = await journal.read_all()
    meta_types = [str(e.payload.get("type")) for e in entries if e.kind == "meta"]
    assert "action.reversed" in meta_types


async def test_reverse_raises_when_no_reversal_declared(
    tmp_path: Path, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
) -> None:
    priv, pub = keypair
    journal = ActionJournal(tmp_path / "audit.log", priv, pub)
    await journal.load()
    intents = IntentStore(tmp_path / "intents.jsonl")
    effector = _FakeEffector()
    executor = Executor(
        journal,
        intents,
        lambda _op: effector,
    )

    intent = _intent(category="safe", action_class="light")
    action = await executor.execute(intent, notify="silent")
    assert action is not None

    with pytest.raises(ActionError, match="declared no reversal"):
        await executor.reverse(action.id)
