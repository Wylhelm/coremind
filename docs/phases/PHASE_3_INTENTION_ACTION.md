# Phase 3 — Intention + Action + Interaction (L5 + L6 + user loop)

**Duration:** ~2 weeks
**Prerequisite:** Phase 2 complete
**Deliverable:** CoreMind becomes **proactive**. It generates its own questions, decides what to act on, and executes with graduated agency — fully signed, reversible, and respecting the user's rhythm.

This is the phase where CoreMind stops being a smart observer and starts being an autonomous agent. It is also the phase where CoreMind becomes **livable**: the graduated consent protocol (see `ARCHITECTURE.md §15`) is implemented end-to-end so the user is asked when they must be, and left alone otherwise.

Every design decision here must preserve reversibility and user control.

---

## Goals

- Intention loop generates internal prompts from world + memory + reasoning.
- Intents are ranked by salience and gated by confidence.
- Action layer executes `safe`-category autonomously, notifies for `suggest`, and asks approval for `ask`.
- **Forced approval classes** are enforced in the daemon and cannot be bypassed by plugins or by high-confidence reasoning.
- Every action is signed, journaled, and reversible when applicable.
- The **Notification Port** abstraction is implemented with a Telegram adapter that supports inline buttons for approval responses.
- **Quiet hours and focus windows** are enforced at the notification layer.
- User has a control surface to approve/dismiss/reverse from the CLI and any configured channel.
- Second round of plugins adds effectors (at least one).

---

## Deliverables Checklist

- [ ] `src/coremind/intention/loop.py` — intention generation loop
- [ ] `src/coremind/intention/prompts.py` — prompt templates
- [ ] `src/coremind/intention/schemas.py` — `Intent`, `InternalQuestion`, `ActionProposal`
- [ ] `src/coremind/intention/salience.py` — salience scoring heuristics
- [ ] `src/coremind/action/router.py` — routes intents by confidence tier
- [ ] `src/coremind/action/executor.py` — runs actions through effector plugins
- [ ] `src/coremind/action/journal.py` — hash-chained audit log writer + verifier
- [ ] `src/coremind/action/approvals.py` — approval gate abstraction
- [ ] `src/coremind/notify/port.py` — `NotificationPort` Protocol + shared schemas (see §15.5)
- [ ] `src/coremind/notify/quiet_hours.py` — quiet-hours + focus-window policy enforcement
- [ ] `src/coremind/notify/adapters/telegram.py` — first Notification Port adapter (Telegram inline buttons)
- [ ] `src/coremind/notify/adapters/dashboard.py` — in-dashboard notification adapter (used in Phase 4 dashboard, but port implemented here)
- [ ] `plugins/homeassistant/` — upgraded to bidirectional (accepts actions)
- [ ] CLI: `coremind intent *`, `coremind action *`, `coremind approvals *`, `coremind audit verify`
- [ ] Tests for every new module, plus end-to-end scenarios

---

## Tasks for the Coding Agent

### 3.1 Intent schema

**File:** `src/coremind/intention/schemas.py`

```python
class InternalQuestion(BaseModel):
    id: str
    text: str                         # the question in natural language
    grounding: list[EntityRef]        # what in the world triggered it
    reasoning_refs: list[str]         # ids of reasoning cycles that fed it

class ActionProposal(BaseModel):
    operation: str                    # plugin-qualified, e.g. "homeassistant.turn_on"
    parameters: dict
    expected_outcome: str             # what the system predicts will happen
    reversal: str | None              # how to undo, if applicable
    action_class: str                 # e.g. "hvac", "light", "email.outbound", "finance.transfer"

class Intent(BaseModel):
    id: str
    created_at: datetime
    question: InternalQuestion
    proposed_action: ActionProposal | None
    salience: float                   # 0.0 – 1.0
    confidence: float                 # 0.0 – 1.0
    category: Literal["safe", "suggest", "ask"]   # aligned with ARCHITECTURE.md §15.2
    status: Literal[
        "pending",
        "pending_approval",
        "approved",
        "rejected",
        "snoozed",
        "executing",
        "done",
        "failed",
        "expired",
    ]
    expires_at: datetime | None = None
    human_feedback: str | None = None
```

Note: the literal set for `category` uses `safe` / `suggest` / `ask` — the same terms used in the architecture doc. The earlier draft used `safe` / `optimization` / `uncertain`; those are superseded.

### 3.2 Intention Loop

**File:** `src/coremind/intention/loop.py`

Runs every N minutes (configurable, default 10), and reactively on significant reasoning outputs.

Pseudocode:

```python
async def intention_cycle():
    world = await world_store.snapshot()
    recent_reasoning = await reasoning_store.recent(window="1h")
    recent_intents = await intent_store.recent(window="24h")  # for loop detection

    prompt_ctx = {
        "world": world.compact(),
        "reasoning": recent_reasoning,
        "recent_intents_summary": summarize(recent_intents),
        "user_patterns": await procedural_memory.active_patterns(),
    }

    questions_output = await llm.complete_structured(
        layer="intention",
        system=prompts.INTENTION_SYSTEM,
        user=prompts.INTENTION_USER.render(**prompt_ctx),
        response_model=QuestionBatch,
    )

    for q in questions_output.questions:
        if await is_duplicate_or_loop(q, recent_intents):
            continue
        proposed = await plan_action(q)
        salience = score_salience(q, world)
        confidence = score_confidence(q, proposed)
        category = categorize(confidence, proposed)

        intent = Intent(
            id=ulid(),
            created_at=utcnow(),
            question=q,
            proposed_action=proposed,
            salience=salience,
            confidence=confidence,
            category=category,
            status="pending",
        )
        await intent_store.save(intent)
        await action_router.route(intent)
```

### 3.3 Salience & Confidence Scoring

**File:** `src/coremind/intention/salience.py`

**Salience** — how much a question matters, blend of:
- **Novelty:** is this a new concern? (penalize repeats)
- **Urgency:** are we close to a threshold? (e.g. temperature climbing toward a limit)
- **Coherence:** does it align with user's known priorities from procedural memory?
- **Impact:** how many entities are affected?

**Confidence** — how sure the system is about the proposed action, blend of:
- Model-reported confidence (if available)
- Number of matching procedural rules
- Agreement across a reasoning ensemble (if enabled)
- Historical calibration (from reflection feedback in Phase 4)

Both return `0.0 – 1.0`. Exact formulas are implementation details but must be documented and testable.

### 3.4 Action Router

**File:** `src/coremind/action/router.py`

```python
class ActionRouter:
    async def route(self, intent: Intent) -> None:
        if intent.proposed_action is None:
            return  # pure question, no action to take

        # Forced approval classes are authoritative — they override any
        # category the intention layer chose.
        if self._is_forced_ask(intent.proposed_action):
            if intent.category != "ask":
                await self._emit_meta("security.category.override_blocked", intent)
            intent.category = "ask"
            await self.approvals.request(intent)
            return

        if intent.category == "safe":
            await self.executor.execute(intent, notify="silent")
        elif intent.category == "suggest":
            await self.executor.execute_with_grace(
                intent,
                grace=timedelta(seconds=self.config.suggest_grace_seconds),
            )
        else:  # ask
            await self.approvals.request(intent)
```

`_is_forced_ask` returns True — regardless of confidence — when the proposed action's `action_class` falls into any of the following **hardcoded** categories (see `ARCHITECTURE.md §15.4`):

- Finance (any transfer, purchase, investment change)
- Outbound messaging to third parties (email, SMS, chat, social)
- Modifications to external credentials, API keys, or shared infrastructure
- Plugin installation or permission grant
- Modifications to CoreMind's own safety mechanisms (disabling a forced-approval class, changing signing keys, etc.)
- Any action class the user has explicitly listed in `config.ask_classes`

The forced-ask list is defined in `src/coremind/action/forced_classes.py` and is **not** overridable by plugins. A plugin attempting to override it via its manifest triggers a `security.category.override_blocked` meta-event and the plugin is sandboxed pending user review.

### 3.5 Executor

**File:** `src/coremind/action/executor.py`

```python
class Executor:
    async def execute(self, intent: Intent, notify: Literal["silent", "summary", "immediate"]) -> None:
        plugin = self.registry.effector_for(intent.proposed_action.operation)
        if plugin is None:
            await self._fail(intent, "no_effector")
            return

        action = Action(
            id=ulid(),
            intent_id=intent.id,
            timestamp=utcnow(),
            category=intent.category,
            operation=intent.proposed_action.operation,
            parameters=intent.proposed_action.parameters,
            signature=None,         # filled below
            reversible_by=intent.proposed_action.reversal,
        )
        action.signature = sign_action(action, self.daemon_key)

        await self.journal.append(action)   # BEFORE execution, for pre-image integrity

        try:
            result = await plugin.invoke_action(action)
            action.result = result
            await self.journal.update_result(action)
            intent.status = "done"
        except Exception as e:
            intent.status = "failed"
            await self._record_failure(action, e)
        finally:
            await intent_store.save(intent)

        await self._notify(action, notify)
```

Notice: **journal BEFORE execute**. We pre-commit the intent to perform the action; if execution fails the journal still reflects what was attempted.

### 3.6 Audit Journal

**File:** `src/coremind/action/journal.py`

- Append-only JSONL at `~/.coremind/audit.log`.
- Hash-chained: each line includes `prev_hash` + `signature`.
- `coremind audit verify` walks the chain end-to-end and reports any break.
- The journal file's permissions are enforced at startup: owner-only, chmod 600.

### 3.7 Approval Gate

**File:** `src/coremind/action/approvals.py`

```python
class ApprovalGate:
    def __init__(self, notification_port: NotificationPort, store: ApprovalStore):
        self.notify = notification_port
        self.store = store

    async def request(
        self,
        intent: Intent,
        timeout: timedelta = timedelta(hours=24),
    ) -> None:
        intent.status = "pending_approval"
        intent.expires_at = utcnow() + timeout
        await self.store.save_intent(intent)

        receipt = await self.notify.notify(
            message=self._format(intent),
            category="ask",
            actions=[
                ApprovalAction(label="✅ Approve", value="approve"),
                ApprovalAction(label="❌ Deny", value="deny"),
                ApprovalAction(label="⏸ Snooze 1h", value="snooze:3600"),
            ],
            intent_id=intent.id,
        )
        await self.store.save_pending(PendingApproval(intent_id=intent.id, receipt=receipt))

    async def handle_response(
        self,
        intent_id: str,
        decision: Literal["approve", "deny", "snooze"],
        snooze_seconds: int | None = None,
        note: str | None = None,
        responder: UserRef | None = None,
    ) -> None: ...

    async def expire_stale(self) -> None:
        """Scheduled task: marks expired pending approvals and emits
        `meta.intent.expired` events. Never auto-executes."""
```

**Key semantics (matching `ARCHITECTURE.md §15.3`):**

- Default TTL is **24 hours**, configurable.
- On expiration, the intent is marked `expired` and a `meta.intent.expired` event is emitted. It is **never** silently executed.
- **Snoozing is explicit and bounded**: an intent can be snoozed **once**. A second snooze attempt on the same intent forces the user to answer or to accept expiration.
- Every approval response is recorded in the journal as a signed event carrying the responder's identity.

Approval responses can come from:
- CLI: `coremind approvals approve <id>`
- Any `NotificationPort` adapter that supports callbacks (Telegram inline buttons, dashboard, etc.)
- Web dashboard (Phase 4 surfaces it; the port is implemented here)

### 3.8 Notification Port — interface + Telegram adapter

The interaction model (`ARCHITECTURE.md §15.5`) requires a single abstraction through which CoreMind talks to the user. That abstraction is `NotificationPort`.

**File:** `src/coremind/notify/port.py`

```python
class ApprovalAction(BaseModel):
    label: str                     # human-facing button text
    value: str                     # machine-facing response value, e.g. "approve"

class ApprovalResponse(BaseModel):
    intent_id: str
    decision: Literal["approve", "deny", "snooze"]
    snooze_seconds: int | None = None
    note: str | None = None
    responder: UserRef
    received_at: datetime

class NotificationReceipt(BaseModel):
    port_id: str
    channel_message_id: str
    sent_at: datetime

class NotificationPort(Protocol):
    id: str                        # e.g. "telegram", "dashboard"
    supports_callbacks: bool       # can this port deliver approval responses?

    async def notify(
        self,
        *,
        message: str,
        category: Literal["info", "suggest", "ask"],
        actions: list[ApprovalAction] | None,
        intent_id: str | None,
    ) -> NotificationReceipt: ...

    async def subscribe_responses(self) -> AsyncIterator[ApprovalResponse]: ...
```

**File:** `src/coremind/notify/adapters/telegram.py` — the first adapter.

- Uses bot API (user's own bot), credentials via `secrets:telegram_bot_token`.
- `notify` sends a message; when `category == "ask"`, inline buttons are rendered from `actions`.
- `subscribe_responses` yields `ApprovalResponse` objects from callback queries (long-poll or webhook — user's choice in config).
- `supports_callbacks = True`.

**Selection:** config declares a **primary** port and an ordered fallback list. The daemon tries the primary first; on failure or timeout, falls back in order.

```toml
[notify]
primary = "telegram"
fallbacks = ["dashboard", "email"]
```

### 3.8b Quiet hours & focus windows

**File:** `src/coremind/notify/quiet_hours.py`

Implements the policy described in `ARCHITECTURE.md §15.6`:

- Quiet hours (configurable, default 23:00 → 07:00):
  - `info` and `suggest`: **deferred** to the next active window.
  - `ask` for non-urgent domains: queued at lower urgency.
  - `ask` for safety/security classes: delivered immediately regardless.
- Focus windows (declared by user, ad-hoc or recurring):
  - All non-`ask` notifications suppressed.
  - `ask` notifications delivered but without sound/vibration hints (port-dependent).
- Presence signals (phone, HA motion, calendar busy) inform urgency scoring but never override the user's declared schedule.

The quiet-hours filter sits **between** the `ApprovalGate` / executor and the `NotificationPort`. Plugins and effectors cannot bypass it.

### 3.9 Effector plugin upgrade — Home Assistant

Upgrade `plugins/homeassistant/` to **bidirectional**:
- In addition to emitting events, implements `InvokeAction` RPC.
- Accepted operations: `turn_on`, `turn_off`, `set_brightness`, `set_temperature`, `trigger_scene`.
- Each operation validates parameters against HA's service schema before calling.
- Returns structured `ActionResult` with success/error detail.

Manifest update:
```toml
[accepts_operations]
"homeassistant.turn_on" = { entity_types = ["light", "switch"] }
"homeassistant.turn_off" = { entity_types = ["light", "switch"] }
"homeassistant.set_brightness" = { entity_types = ["light"], param_range = { brightness = [0, 255] } }
# ...
```

### 3.10 CLI additions

```
coremind intent list --status pending
coremind intent show <id>
coremind intent approve <id>
coremind intent reject <id> [--note "..."]
coremind intent snooze <id> --for 1h

coremind action list --last 24h
coremind action show <id>
coremind action reverse <id>          # invokes the reversal operation

coremind approvals pending
coremind approvals approve <id>
coremind approvals deny <id>
coremind approvals snooze <id> --for 1h

coremind notify test --port telegram  # sends a test notification through a port
coremind notify status                 # primary port + fallbacks + health
coremind quiet-hours show              # current policy + what's deferred

coremind audit verify                  # walks journal hash chain
coremind audit tail
```

### 3.11 Tests

- Salience ranking: test cases with known good/bad rankings (golden tests)
- Router: each category lands in the right path (`safe` → silent exec, `suggest` → grace-delayed exec, `ask` → approval)
- Journal: hash chain integrity; tampering detection test
- Reversal: plant an action with a reversal, reverse it, verify state restored
- Approval flow end-to-end: issue intent → `NotificationPort.notify` → simulated response → execution
- Approval expiration: pending approval past TTL → `expired` status + `meta.intent.expired` event emitted; no execution
- Approval snooze: snooze once works; second snooze is refused
- **Forced approval override block:** plugin attempts to return `safe` for a financial action → router forces `ask`, emits `security.category.override_blocked`, plugin is flagged
- Forced approval across confidence: high-confidence (0.99) financial intent still routed through approval
- Quiet hours: `suggest` notifications during quiet hours are deferred; safety-class `ask` still delivered
- Notification port fallback: primary port fails → fallback port receives the notification; both receipts journaled

---

## Success Criteria

1. `coremind intent list` shows autonomously-generated intents within minutes of startup, given a live world.
2. A `safe` intent (e.g. "turn on bedroom humidifier at 90% mist when humidity drops below 40%") executes without user interaction and appears in the audit log.
3. A `suggest` intent notifies the user immediately, executes after the configured grace window unless cancelled, and is journaled either way.
4. An `ask` intent produces a Telegram approval request with inline buttons — `✅ Approve`, `❌ Deny`, `⏸ Snooze 1h`. All four outcomes (approved, denied, snoozed, expired) are journaled.
5. `coremind audit verify` returns green on a long-running daemon.
6. `coremind action reverse <id>` successfully reverses a reversible action.
7. **Forced-approval override-block**: at 0.99 confidence, an action whose class is forced-`ask` (e.g. sending an email, financial transfer) waits for explicit user approval. A plugin attempting to return `safe` for such an action is sandboxed and a `security.category.override_blocked` event is journaled.
8. Quiet-hours policy: during declared quiet hours, `suggest` notifications are deferred and delivered in the next active window; safety-class `ask` notifications are still delivered immediately.
9. No action has ever been executed without a corresponding signed journal entry. No journal entry is missing a prior intent. No approval response is missing a signature.

---

## Explicitly Out of Scope

- Reflection (L7) — no self-evaluation yet; salience and confidence are static heuristics.
- **Approval-history learning** (auto-promotion of `ask` → `suggest` based on consistent approvals, per `ARCHITECTURE.md §15.7`) — implemented in Phase 4.
- The web dashboard UI (CLI only for now; the dashboard `NotificationPort` adapter is implemented but the UI arrives in Phase 4).
- Multiple Notification Port adapters beyond Telegram + dashboard (Signal, Discord, email in Phase 4).
- Federated / multi-user scenarios.

---

## Handoff to Phase 4

Phase 4 begins with:
- A system that autonomously acts with graduated agency.
- Complete audit trail of every autonomous side effect.
- At least one effector plugin and one approval channel operational.

**Next:** [`PHASE_4_REFLECTION_ECOSYSTEM.md`](PHASE_4_REFLECTION_ECOSYSTEM.md)
