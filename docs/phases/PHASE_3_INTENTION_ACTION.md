# Phase 3 — Intention + Action (L5 + L6)

**Duration:** ~1.5 weeks
**Prerequisite:** Phase 2 complete
**Deliverable:** CoreMind becomes **proactive**. It generates its own questions, decides what to act on, and executes with graduated agency — fully signed and reversible.

This is the phase where CoreMind stops being a smart observer and starts being an autonomous agent. Every design decision here must preserve reversibility and user control.

---

## Goals

- Intention loop generates internal prompts from world + memory + reasoning.
- Intents are ranked by salience and gated by confidence.
- Action layer executes Safe-tier autonomously, notifies for Suggest-tier, and asks approval for Uncertain-tier.
- Every action is signed, journaled, and reversible when applicable.
- User has a control surface to approve/dismiss/reverse from the CLI and any configured channel (e.g. Telegram, Slack).
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
- [ ] `src/coremind/channels/base.py` — channel adapter interface
- [ ] `src/coremind/channels/telegram.py` — first channel adapter (approval via Telegram)
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

class Intent(BaseModel):
    id: str
    created_at: datetime
    question: InternalQuestion
    proposed_action: ActionProposal | None
    salience: float                   # 0.0 – 1.0
    confidence: float                 # 0.0 – 1.0
    category: Literal["safe", "optimization", "uncertain"]
    status: Literal["pending", "approved", "rejected", "executing", "done", "failed", "expired"]
    human_feedback: str | None = None
```

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

        if self._requires_forced_approval(intent):
            await self.approvals.request(intent)
            return

        if intent.category == "safe":
            await self.executor.execute(intent, notify="summary")
        elif intent.category == "optimization":
            await self.executor.execute(intent, notify="immediate")
        else:  # uncertain
            await self.approvals.request(intent)
```

`_requires_forced_approval` returns True regardless of confidence when the action:
- Sends data off-machine (email, webhook to external host, API call to outside)
- Touches finances
- Modifies critical system config (cron, systemd, /etc, shell rc)
- Matches user's explicit `require_approval` list

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
    async def request(self, intent: Intent, timeout: timedelta = timedelta(hours=6)) -> None:
        pending = PendingApproval(intent_id=intent.id, channel=self._default_channel, expires_at=utcnow()+timeout)
        await self.store.save(pending)
        await self.channel.send_approval_request(intent)

    async def handle_response(self, intent_id: str, decision: Literal["approve", "reject"], note: str | None) -> None: ...
```

Approval responses can come from:
- CLI: `coremind approvals approve <id>`
- Channel callback (Telegram button, Slack action, etc.)
- Web dashboard (Phase 4)

### 3.8 Channel Adapter — Telegram (first)

**File:** `src/coremind/channels/telegram.py`

Implements the base interface:
```python
class ChannelAdapter(Protocol):
    async def send_notification(self, text: str, metadata: dict | None = None) -> None: ...
    async def send_approval_request(self, intent: Intent) -> None: ...
    async def start_listener(self) -> None: ...     # for callback buttons
```

Telegram impl:
- Uses bot API (user's own bot)
- Approval request is sent as a message with inline buttons: ✅ Approve / ❌ Reject / ⏸ Later
- Callbacks reach the daemon via long-poll or webhook (user's choice)

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

coremind action list --last 24h
coremind action show <id>
coremind action reverse <id>          # invokes the reversal operation

coremind approvals pending
coremind approvals approve <id>
coremind approvals reject <id>

coremind audit verify                 # walks journal hash chain
coremind audit tail
```

### 3.11 Tests

- Salience ranking: test cases with known good/bad rankings (golden tests)
- Router: each category lands in the right path (safe → silent exec, uncertain → approval)
- Journal: hash chain integrity; tampering detection test
- Reversal: plant an action with a reversal, reverse it, verify state restored
- Approval flow end-to-end: issue intent → request approval → simulated channel response → execution
- Forced approval: assert that high-confidence intents touching financial class still require approval

---

## Success Criteria

1. `coremind intent list` shows autonomously-generated intents within minutes of startup, given a live world.
2. A safe-tier intent (e.g. "turn on bedroom humidifier at 90% mist when humidity drops below 40%") executes without user interaction and appears in the audit log.
3. An uncertain-tier intent produces a Telegram approval request with inline buttons; clicking Approve executes it; clicking Reject logs the rejection.
4. `coremind audit verify` returns green on a long-running daemon.
5. `coremind action reverse <id>` successfully reverses a reversible action.
6. Force-approval works: even at 0.99 confidence, an action sending data off-machine waits for explicit user approval.
7. No action has ever been executed without a corresponding signed journal entry. No journal entry is missing a prior intent.

---

## Explicitly Out of Scope

- Reflection (L7) — no self-evaluation yet; salience and confidence are static heuristics.
- The web dashboard (CLI only for now).
- Multiple channel adapters (Telegram only; Slack/Discord/email in Phase 4).
- Federated / multi-user scenarios.

---

## Handoff to Phase 4

Phase 4 begins with:
- A system that autonomously acts with graduated agency.
- Complete audit trail of every autonomous side effect.
- At least one effector plugin and one approval channel operational.

**Next:** [`PHASE_4_REFLECTION_ECOSYSTEM.md`](PHASE_4_REFLECTION_ECOSYSTEM.md)
