# Phase 2 — Self-Improving Meta-Loop (L8)

**Target:** CoreMind v2
**Duration estimate:** 1 week
**Agent:** Opus in VS Code
**Prerequisites:** Phase 1 (Autonomy Slider) recommended but not required

---

## 1. Problem Statement

CoreMind v1 has **fixed parameters everywhere**:
- `min_salience = 0.35`
- `min_confidence = 0.40`
- Notification cooldowns hardcoded per channel
- Plugin poll intervals fixed in config
- Investigation pruner thresholds static

**Real issues observed in production:**
1. Repeated identical questions ("Roborock hasn't cleaned since X" for weeks)
2. Ignored notifications never get cooldowns increased
3. Plugin errors don't slow down their poll cadence
4. Approval patterns don't drive slider promotions
5. The system never *learns* from user behavior

The user shouldn't have to manually tune these. The system should observe its own performance and adapt — within strict safety bounds.

---

## 2. Design

### 2.1 L8 — The Meta Layer

L8 sits **above** L7 (reflection) and observes the whole stack:

```
              ┌─────────────────┐
              │  L8 - META       │
              │                  │
              │  ┌────────────┐  │
              │  │ Observer    │  │  ← reads metrics from L2-L7
              │  └─────┬──────┘  │
              │        ▼         │
              │  ┌────────────┐  │
              │  │ Evaluator   │  │  ← applies policies
              │  └─────┬──────┘  │
              │        ▼         │
              │  ┌────────────┐  │
              │  │ Safety Val. │  │  ← rejects forbidden adjustments
              │  └─────┬──────┘  │
              │        ▼         │
              │  ┌────────────┐  │
              │  │ Adjuster    │  │  ← applies + audits + propagates
              │  └────────────┘  │
              └────────┬─────────┘
                       │
                       ▼
              L2-L7 parameters
```

### 2.2 Meta-Observations

#### 2.2.1 What L8 Tracks

| Observation | Source | Computation Window |
|---|---|---|
| `intent_repeat_rate` | L5 intent history | Last 6h |
| `notification_engagement_rate` | L6 audit + user interactions | Last 7d |
| `domain_approval_rate` | L6 approvals per domain | Last 30d |
| `plugin_error_rate` | L1 perception error counts | Last 1h |
| `plugin_latency_p95` | L1 perception timing | Last 1h |
| `token_per_useful_intent` | L4 token usage / L5 intents | Last 24h |
| `investigation_success_rate` | L4 investigation outcomes | Last 7d |
| `low_quality_intent_rate` | L5 intents below threshold | Last 24h |
| `stale_entity_rate` | L2 entities not updated | Per cycle |

#### 2.2.2 Observation Schema

```python
class MetaObservation(BaseModel):
    """A single measured metric about system performance."""
    observation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: str                       # e.g., "intent_repeat_rate"
    value: float
    threshold: float                # The threshold this observation tests against
    window_seconds: float           # Computation window
    triggers_policy: bool = False   # Did this observation trigger a policy?
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 2.3 Adjustment Policies

#### 2.3.1 Policy Schema

```python
class AdjustmentPolicy(BaseModel):
    """A rule that maps observations to parameter changes."""
    name: str
    description: str
    observation_kind: str
    trigger_condition: Literal["above", "below", "between"]
    threshold: float
    threshold_upper: float | None = None   # for "between"
    parameter_path: str             # dotted path, e.g., "intention.min_salience"
    direction: Literal["increase", "decrease"]
    delta: float                    # absolute change per adjustment
    min_value: float                # safety bound
    max_value: float                # safety bound
    cooldown_seconds: float         # min time between same-parameter adjustments
    requires_user_approval: bool = False
    enabled: bool = True
```

#### 2.3.2 Built-in Policies

```python
DEFAULT_POLICIES: list[AdjustmentPolicy] = [
    AdjustmentPolicy(
        name="lower_salience_when_quiet",
        description="Lower min_salience when system generates few intents",
        observation_kind="intents_per_hour",
        trigger_condition="below",
        threshold=1.0,                  # less than 1 intent per hour
        parameter_path="intention.min_salience",
        direction="decrease",
        delta=0.05,
        min_value=0.20,                 # never go below 0.20
        max_value=0.70,
        cooldown_seconds=21600,         # 6 hours
    ),
    AdjustmentPolicy(
        name="raise_salience_when_noisy",
        description="Raise min_salience when many low-quality intents",
        observation_kind="low_quality_intent_rate",
        trigger_condition="above",
        threshold=0.5,                  # 50%+ of intents below quality bar
        parameter_path="intention.min_salience",
        direction="increase",
        delta=0.05,
        min_value=0.20,
        max_value=0.70,
        cooldown_seconds=21600,
    ),
    AdjustmentPolicy(
        name="increase_cooldown_on_ignored",
        description="Increase notification cooldown for topics consistently ignored",
        observation_kind="notification_ignore_rate",
        trigger_condition="above",
        threshold=0.7,
        parameter_path="notifications.cooldown_seconds.<topic>",
        direction="increase",
        delta=3600.0,                   # +1 hour
        min_value=60.0,                 # min 1 minute
        max_value=86400.0,              # max 24 hours
        cooldown_seconds=86400.0,
    ),
    AdjustmentPolicy(
        name="decrease_cooldown_on_engaged",
        description="Decrease cooldown for topics user engages with",
        observation_kind="notification_engagement_rate",
        trigger_condition="above",
        threshold=0.8,
        parameter_path="notifications.cooldown_seconds.<topic>",
        direction="decrease",
        delta=1800.0,                   # -30 minutes
        min_value=60.0,
        max_value=86400.0,
        cooldown_seconds=86400.0,
    ),
    AdjustmentPolicy(
        name="throttle_failing_plugin",
        description="Double poll interval for plugins with high error rates",
        observation_kind="plugin_error_rate",
        trigger_condition="above",
        threshold=0.5,
        parameter_path="plugins.<plugin_id>.poll_interval_seconds",
        direction="increase",
        delta=0.0,                      # multiplied, not added (handled specially)
        min_value=30.0,
        max_value=86400.0,
        cooldown_seconds=3600.0,
    ),
    AdjustmentPolicy(
        name="restore_plugin_cadence",
        description="Restore plugin cadence when errors clear",
        observation_kind="plugin_error_rate",
        trigger_condition="below",
        threshold=0.05,
        parameter_path="plugins.<plugin_id>.poll_interval_seconds",
        direction="decrease",
        delta=0.0,                      # halved
        min_value=30.0,
        max_value=86400.0,
        cooldown_seconds=3600.0,
    ),
    AdjustmentPolicy(
        name="propose_slider_promotion",
        description="Propose autonomy slider increase for high-approval domains",
        observation_kind="domain_approval_rate",
        trigger_condition="above",
        threshold=0.8,
        parameter_path="autonomy.domains.<domain>",
        direction="increase",
        delta=0.1,
        min_value=0.0,
        max_value=1.0,
        cooldown_seconds=604800.0,      # 7 days
        requires_user_approval=True,    # User must approve
    ),
]
```

### 2.4 Hard Safety Boundaries

L8 **CANNOT** modify any of these — enforced by `MetaSafetyValidator`:

```python
FORBIDDEN_PARAMETER_PATHS = [
    # Approval gates
    "autonomy.hard_ask",
    "autonomy.hard_safe",
    
    # Quiet hours
    "intention.quiet_hours",
    "notifications.quiet_hours",
    
    # Secrets
    "secrets.*",
    
    # Plugin permissions
    "plugins.*.permissions",
    "plugins.*.action_classes",
    
    # Audit
    "audit.*",
    "logging.*",
    
    # Meta-loop itself
    "meta.forbidden_parameter_paths",
    "meta.safety_bounds",
    "meta.enabled",
]

HARD_BOUNDS = {
    "intention.min_salience": (0.20, 0.70),
    "intention.min_confidence": (0.20, 0.80),
    "reasoning.interval_seconds": (60.0, 7200.0),         # 1min to 2h
    "intention.interval_seconds": (60.0, 3600.0),         # 1min to 1h
    "reflection.interval_seconds": (1800.0, 86400.0),     # 30min to 24h
    "plugins.*.poll_interval_seconds": (30.0, 86400.0),
    "notifications.cooldown_seconds.*": (60.0, 86400.0),
    "autonomy.domains.*": (0.0, 1.0),
}
```

### 2.5 Adjustment Lifecycle

```python
class AdjustmentRecord(BaseModel):
    """Record of an applied adjustment."""
    adjustment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    policy_name: str
    parameter_path: str
    old_value: Any
    new_value: Any
    reason: str
    triggered_by_observation_id: str
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rollback_at: datetime | None = None
    user_approved: bool = False
    user_approved_at: datetime | None = None
```

#### 2.5.1 Flow

```
1. Observer collects observations (every 5 min)
2. For each observation:
   a. Find matching policies
   b. Check policy cooldowns
   c. If triggered, build AdjustmentRecord
3. Validate via MetaSafetyValidator:
   a. Reject if parameter_path is forbidden
   b. Reject if new_value out of bounds
   c. Reject if multiple competing adjustments
4. If requires_user_approval:
   a. Add to graduation_proposals queue
   b. Notify user via standard channel
   c. Wait for approval
5. Apply adjustment:
   a. Update in-memory config
   b. Persist to SurrealDB (meta_adjustment table)
   c. Propagate to dependent components
   d. Log audit event
```

---

## 3. Files to Create/Modify

### New Files

| File | Purpose |
|---|---|
| `src/coremind/meta/__init__.py` | Package init |
| `src/coremind/meta/observer.py` | MetaObserver — collects observations |
| `src/coremind/meta/policies.py` | DEFAULT_POLICIES, AdjustmentPolicy |
| `src/coremind/meta/evaluator.py` | PolicyEvaluator — matches observations to policies |
| `src/coremind/meta/safety_validator.py` | MetaSafetyValidator — enforces bounds |
| `src/coremind/meta/adjuster.py` | MetaAdjuster — applies + audits adjustments |
| `src/coremind/meta/schemas.py` | All Pydantic models |
| `src/coremind/meta/loop.py` | MetaLoop — the orchestrator |
| `tests/test_meta_observer.py` | Observer tests |
| `tests/test_meta_evaluator.py` | Evaluator tests |
| `tests/test_meta_safety.py` | Safety validator tests |
| `tests/test_meta_loop.py` | Integration tests |

### Modified Files

| File | Change |
|---|---|
| `src/coremind/core/daemon.py` | Initialize and start MetaLoop |
| `src/coremind/core/config.py` | Add MetaConfig section |
| `src/coremind/intention/loop.py` | Expose `min_salience` as adjustable |
| `src/coremind/dashboard/views.py` | Add meta-loop dashboard page |
| `src/coremind/cli/__init__.py` | Add `coremind meta` commands |
| `~/.coremind/config.toml` | Add `[meta]` section |

---

## 4. Implementation Details

### 4.1 MetaObserver

```python
class MetaObserver:
    """Collects observations about system performance."""
    
    def __init__(
        self,
        intention_store,
        action_store,
        plugin_registry,
        narrative_store,
    ) -> None:
        self._intention = intention_store
        self._action = action_store
        self._plugins = plugin_registry
        self._narrative = narrative_store
    
    async def observe_all(self) -> list[MetaObservation]:
        """Collect all observation kinds."""
        return [
            await self._observe_intent_repeat_rate(),
            await self._observe_notification_engagement_rate(),
            *await self._observe_domain_approval_rates(),
            *await self._observe_plugin_error_rates(),
            await self._observe_token_efficiency(),
            await self._observe_investigation_success_rate(),
            await self._observe_low_quality_intent_rate(),
        ]
    
    async def _observe_intent_repeat_rate(self) -> MetaObservation:
        """Compute the fraction of intents that are repeats in the last 6h."""
        window = timedelta(hours=6)
        intents = await self._intention.list_recent(window)
        if not intents:
            value = 0.0
        else:
            # Group by hash of (topic, key_entities)
            from collections import Counter
            keys = [self._intent_key(i) for i in intents]
            counts = Counter(keys)
            repeats = sum(c - 1 for c in counts.values() if c > 1)
            value = repeats / len(intents)
        
        return MetaObservation(
            kind="intent_repeat_rate",
            value=value,
            threshold=0.3,  # >30% repeats is bad
            window_seconds=window.total_seconds(),
            metadata={"total_intents": len(intents)},
        )
    
    async def _observe_notification_engagement_rate(self) -> MetaObservation:
        """Compute fraction of notifications user engaged with."""
        window = timedelta(days=7)
        notifications = await self._action.list_notifications(window)
        if not notifications:
            value = 0.5  # neutral when no data
        else:
            engaged = sum(1 for n in notifications if n.user_interacted)
            value = engaged / len(notifications)
        
        return MetaObservation(
            kind="notification_engagement_rate",
            value=value,
            threshold=0.3,  # <30% engagement is bad
            window_seconds=window.total_seconds(),
            metadata={"total": len(notifications)},
        )
    
    async def _observe_domain_approval_rates(self) -> list[MetaObservation]:
        """Per-domain approval rates."""
        window = timedelta(days=30)
        actions = await self._action.list_recent(window)
        observations = []
        
        from collections import defaultdict
        per_domain: dict[str, list] = defaultdict(list)
        for a in actions:
            if a.category != "ask":
                continue
            domain = classify_action(a.action_class)
            per_domain[domain].append(a)
        
        for domain, domain_actions in per_domain.items():
            if len(domain_actions) < 10:
                continue
            approved = sum(1 for a in domain_actions if a.result and a.result.status == "approved")
            rate = approved / len(domain_actions)
            observations.append(MetaObservation(
                kind="domain_approval_rate",
                value=rate,
                threshold=0.8,
                window_seconds=window.total_seconds(),
                metadata={"domain": domain, "total": len(domain_actions), "approved": approved},
            ))
        return observations
    
    async def _observe_plugin_error_rates(self) -> list[MetaObservation]:
        """Per-plugin error rates over last 1h."""
        window = timedelta(hours=1)
        plugins = await self._plugins.list_active()
        observations = []
        for plugin in plugins:
            stats = await self._plugins.get_stats(plugin.id, window)
            if stats.total_calls == 0:
                continue
            error_rate = stats.errors / stats.total_calls
            observations.append(MetaObservation(
                kind="plugin_error_rate",
                value=error_rate,
                threshold=0.5,
                window_seconds=window.total_seconds(),
                metadata={"plugin_id": plugin.id, "total_calls": stats.total_calls, "errors": stats.errors},
            ))
        return observations
    
    async def _observe_token_efficiency(self) -> MetaObservation:
        """Tokens per useful intent."""
        window = timedelta(hours=24)
        token_count = await self._narrative.total_tokens(window)
        intent_count = await self._intention.count_useful(window)
        value = token_count / max(intent_count, 1)
        return MetaObservation(
            kind="token_per_useful_intent",
            value=value,
            threshold=5000.0,
            window_seconds=window.total_seconds(),
            metadata={"tokens": token_count, "intents": intent_count},
        )
    
    async def _observe_investigation_success_rate(self) -> MetaObservation:
        """Fraction of investigations that conclude successfully."""
        window = timedelta(days=7)
        investigations = await self._narrative.list_investigations(window)
        if not investigations:
            value = 1.0  # no data, assume fine
        else:
            success = sum(1 for i in investigations if i.status == "resolved")
            value = success / len(investigations)
        return MetaObservation(
            kind="investigation_success_rate",
            value=value,
            threshold=0.6,
            window_seconds=window.total_seconds(),
            metadata={"total": len(investigations)},
        )
    
    async def _observe_low_quality_intent_rate(self) -> MetaObservation:
        """Fraction of intents below quality threshold."""
        window = timedelta(hours=24)
        intents = await self._intention.list_recent(window)
        if not intents:
            value = 0.0
        else:
            low_q = sum(1 for i in intents if i.salience < 0.4 or i.confidence < 0.5)
            value = low_q / len(intents)
        return MetaObservation(
            kind="low_quality_intent_rate",
            value=value,
            threshold=0.5,
            window_seconds=window.total_seconds(),
        )
    
    def _intent_key(self, intent) -> str:
        """Hash an intent for repeat detection."""
        import hashlib
        text = f"{intent.topic}|{','.join(sorted(intent.key_entities))}"
        return hashlib.md5(text.encode()).hexdigest()
```

### 4.2 PolicyEvaluator

```python
class PolicyEvaluator:
    """Matches observations to policies and proposes adjustments."""
    
    def __init__(self, policies: list[AdjustmentPolicy], history: AdjustmentHistory):
        self._policies = policies
        self._history = history
    
    def evaluate(
        self,
        observations: list[MetaObservation],
    ) -> list[ProposedAdjustment]:
        proposals = []
        now = datetime.now(UTC)
        
        for obs in observations:
            for policy in self._policies:
                if not policy.enabled:
                    continue
                if policy.observation_kind != obs.kind:
                    continue
                if not self._matches_condition(obs.value, policy):
                    continue
                
                # Resolve parameter path (substitute wildcards from metadata)
                param_path = self._resolve_path(policy.parameter_path, obs.metadata)
                
                # Check cooldown
                last = self._history.last_adjustment(param_path)
                if last and (now - last.applied_at).total_seconds() < policy.cooldown_seconds:
                    continue
                
                # Compute new value
                old_value = self._read_current_value(param_path)
                new_value = self._compute_new_value(old_value, policy)
                
                # Clamp to bounds
                new_value = max(policy.min_value, min(policy.max_value, new_value))
                
                if new_value == old_value:
                    continue  # no change to make
                
                proposals.append(ProposedAdjustment(
                    policy=policy,
                    observation=obs,
                    parameter_path=param_path,
                    old_value=old_value,
                    new_value=new_value,
                ))
        
        return proposals
    
    def _matches_condition(self, value: float, policy: AdjustmentPolicy) -> bool:
        if policy.trigger_condition == "above":
            return value > policy.threshold
        elif policy.trigger_condition == "below":
            return value < policy.threshold
        elif policy.trigger_condition == "between":
            return policy.threshold <= value <= (policy.threshold_upper or policy.threshold)
        return False
    
    def _resolve_path(self, template: str, metadata: dict) -> str:
        """Substitute <placeholders> with metadata values."""
        result = template
        for key, value in metadata.items():
            result = result.replace(f"<{key}>", str(value))
        return result
    
    def _compute_new_value(self, old: float, policy: AdjustmentPolicy) -> float:
        # Special case: poll intervals are multiplied, not added
        if "poll_interval" in policy.parameter_path and policy.delta == 0.0:
            factor = 2.0 if policy.direction == "increase" else 0.5
            return old * factor
        
        # Default: add or subtract delta
        if policy.direction == "increase":
            return old + policy.delta
        else:
            return old - policy.delta
```

### 4.3 MetaSafetyValidator

```python
class MetaSafetyValidator:
    """Enforces forbidden paths and hard bounds."""
    
    def __init__(
        self,
        forbidden_paths: list[str],
        hard_bounds: dict[str, tuple[float, float]],
    ):
        self._forbidden = forbidden_paths
        self._bounds = hard_bounds
    
    def validate(self, proposal: ProposedAdjustment) -> ValidationResult:
        # 1. Check forbidden paths
        for forbidden_pattern in self._forbidden:
            if self._matches(proposal.parameter_path, forbidden_pattern):
                return ValidationResult(
                    valid=False,
                    reason=f"Parameter path '{proposal.parameter_path}' is forbidden (matched '{forbidden_pattern}')",
                )
        
        # 2. Check hard bounds
        bounds = self._find_bounds(proposal.parameter_path)
        if bounds:
            min_val, max_val = bounds
            if proposal.new_value < min_val:
                return ValidationResult(
                    valid=False,
                    reason=f"New value {proposal.new_value} below hard minimum {min_val}",
                )
            if proposal.new_value > max_val:
                return ValidationResult(
                    valid=False,
                    reason=f"New value {proposal.new_value} above hard maximum {max_val}",
                )
        
        return ValidationResult(valid=True, reason="")
    
    def _matches(self, path: str, pattern: str) -> bool:
        """Match path against pattern supporting * wildcards."""
        import fnmatch
        return fnmatch.fnmatch(path, pattern)
    
    def _find_bounds(self, path: str) -> tuple[float, float] | None:
        # Exact match first
        if path in self._bounds:
            return self._bounds[path]
        # Then pattern match
        for pattern, bounds in self._bounds.items():
            if self._matches(path, pattern):
                return bounds
        return None


class ValidationResult(BaseModel):
    valid: bool
    reason: str
```

### 4.4 MetaAdjuster

```python
class MetaAdjuster:
    """Applies adjustments and propagates them to the running system."""
    
    def __init__(
        self,
        config_store,
        narrative_store,
        event_bus,
    ):
        self._config = config_store
        self._narrative = narrative_store
        self._bus = event_bus
    
    async def apply(self, proposal: ProposedAdjustment) -> AdjustmentRecord:
        # 1. Persist record
        record = AdjustmentRecord(
            policy_name=proposal.policy.name,
            parameter_path=proposal.parameter_path,
            old_value=proposal.old_value,
            new_value=proposal.new_value,
            reason=f"{proposal.policy.description} (observation: {proposal.observation.kind}={proposal.observation.value:.3f})",
            triggered_by_observation_id=proposal.observation.observation_id,
            user_approved=not proposal.policy.requires_user_approval,
        )
        await self._narrative.save_adjustment(record)
        
        # 2. Apply to config
        await self._config.set(proposal.parameter_path, proposal.new_value)
        
        # 3. Notify components
        await self._bus.publish("meta.adjustment.applied", record.model_dump())
        
        # 4. Audit log
        log.info(
            "meta.adjustment_applied",
            policy=proposal.policy.name,
            path=proposal.parameter_path,
            old=proposal.old_value,
            new=proposal.new_value,
        )
        
        return record
    
    async def rollback(self, adjustment_id: str) -> None:
        record = await self._narrative.get_adjustment(adjustment_id)
        if not record:
            raise ValueError(f"Adjustment {adjustment_id} not found")
        
        # Restore old value
        await self._config.set(record.parameter_path, record.old_value)
        
        # Mark as rolled back
        record.rollback_at = datetime.now(UTC)
        await self._narrative.update_adjustment(record)
        
        await self._bus.publish("meta.adjustment.rolled_back", record.model_dump())
```

### 4.5 MetaLoop

```python
class MetaLoop:
    """Orchestrates the meta-loop. Runs periodically."""
    
    def __init__(
        self,
        observer: MetaObserver,
        evaluator: PolicyEvaluator,
        validator: MetaSafetyValidator,
        adjuster: MetaAdjuster,
        approval_queue,
        config: MetaConfig,
    ):
        self._observer = observer
        self._evaluator = evaluator
        self._validator = validator
        self._adjuster = adjuster
        self._approval_queue = approval_queue
        self._config = config
        self._task: asyncio.Task | None = None
    
    async def start(self) -> None:
        if not self._config.enabled:
            log.info("meta.disabled")
            return
        self._task = asyncio.create_task(self._run_forever())
    
    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
    
    async def _run_forever(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as e:
                log.exception("meta.tick_failed", error=str(e))
            await asyncio.sleep(self._config.observation_interval_seconds)
    
    async def _tick(self) -> None:
        log.info("meta.tick_start")
        
        # 1. Observe
        observations = await self._observer.observe_all()
        await self._save_observations(observations)
        
        # 2. Evaluate
        proposals = self._evaluator.evaluate(observations)
        if not proposals:
            log.debug("meta.no_proposals")
            return
        
        # 3. Validate & apply
        for proposal in proposals:
            validation = self._validator.validate(proposal)
            if not validation.valid:
                log.warning(
                    "meta.proposal_rejected",
                    policy=proposal.policy.name,
                    path=proposal.parameter_path,
                    reason=validation.reason,
                )
                continue
            
            if proposal.policy.requires_user_approval:
                await self._approval_queue.add(proposal)
                continue
            
            try:
                await self._adjuster.apply(proposal)
            except Exception as e:
                log.exception("meta.apply_failed", policy=proposal.policy.name, error=str(e))
        
        log.info("meta.tick_complete", observations=len(observations), proposals=len(proposals))
```

---

## 5. Configuration

```toml
# ~/.coremind/config.toml

[meta]
enabled = true
observation_interval_seconds = 300         # observe every 5 minutes
max_adjustments_per_hour = 4                # rate limit
require_observation_window_days = 1         # ignore observations before this
log_observations = true                     # persist all observations
log_observations_retention_days = 30

[meta.policies]
# Override defaults by name
lower_salience_when_quiet = { enabled = true, cooldown_seconds = 21600 }
propose_slider_promotion = { enabled = true, requires_user_approval = true }

# Custom policies can be added here
[[meta.custom_policies]]
name = "my_custom_policy"
description = "..."
observation_kind = "..."
# etc
```

---

## 6. CLI Commands

```bash
# Status
coremind meta status
# → Output:
# Meta-loop: ENABLED
# Last tick: 2 minutes ago
# Observations this hour: 12
# Adjustments this hour: 1
# Pending proposals: 2 (require user approval)

# List observations
coremind meta observations --kind intent_repeat_rate --last 24h
# → Table of observations

# List adjustments
coremind meta adjustments --last 7d
# → Table: timestamp, policy, parameter, old → new, reason

# List policies
coremind meta policies
# → Table: name, enabled, observation_kind, threshold, parameter_path

# Override a policy
coremind meta override --policy raise_salience_when_noisy --disabled
coremind meta override --policy raise_salience_when_noisy --enabled

# Rollback
coremind meta rollback <adjustment_id>

# Pending proposals (user approval required)
coremind meta proposals
coremind meta approve <proposal_id>
coremind meta deny <proposal_id>
```

---

## 7. Dashboard

### `/meta` Page Layout

```
┌─ Meta-Loop Status ────────────────────────────────────────┐
│                                                            │
│  Status: ENABLED   Last tick: 2 min ago                    │
│  Observations (24h): 288   Adjustments (24h): 3            │
│                                                            │
└────────────────────────────────────────────────────────────┘

┌─ Pending Proposals (require approval) ────────────────────┐
│                                                            │
│  ▸ Promote `lights` slider 0.8 → 0.9                       │
│    Reason: 94% approval rate over 50 actions (30 days)     │
│    [Approve]  [Deny]  [Snooze]                             │
│                                                            │
└────────────────────────────────────────────────────────────┘

┌─ Recent Observations ─────────────────────────────────────┐
│                                                            │
│  intent_repeat_rate           0.42  ↑ above 0.30 threshold │
│  notification_engagement_rate 0.65  ✓ within bounds        │
│  domain_approval_rate (lights) 0.94 ↑ promotion candidate  │
│  plugin_error_rate (homeassistant) 0.02 ✓                  │
│                                                            │
└────────────────────────────────────────────────────────────┘

┌─ Adjustment History (7d) ─────────────────────────────────┐
│                                                            │
│  2026-05-25 10:32  raise_salience_when_noisy               │
│    intention.min_salience: 0.35 → 0.40                     │
│    [Rollback]                                              │
│                                                            │
│  2026-05-24 18:11  throttle_failing_plugin                 │
│    plugins.govee.poll_interval_seconds: 300 → 600          │
│    [Rollback]                                              │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

---

## 8. Tests

### 8.1 Unit Tests

```python
# tests/test_meta_observer.py
async def test_observe_intent_repeat_rate(mock_intention_store):
    """Repeated intents are correctly counted."""
    mock_intention_store.list_recent.return_value = [
        make_intent(topic="lights", entities=["bureau"]),
        make_intent(topic="lights", entities=["bureau"]),  # repeat
        make_intent(topic="lights", entities=["bureau"]),  # repeat
        make_intent(topic="hvac", entities=["thermostat"]),
    ]
    observer = MetaObserver(mock_intention_store, ...)
    result = await observer._observe_intent_repeat_rate()
    assert result.value == 0.5  # 2 repeats / 4 total

# tests/test_meta_evaluator.py
def test_evaluator_triggers_policy_above_threshold():
    """Observation above threshold produces proposal."""
    policy = AdjustmentPolicy(
        name="test", observation_kind="test_kind",
        trigger_condition="above", threshold=0.5,
        parameter_path="test.param", direction="increase",
        delta=0.1, min_value=0.0, max_value=1.0,
        cooldown_seconds=0,
    )
    evaluator = PolicyEvaluator([policy], EmptyHistory())
    obs = [MetaObservation(kind="test_kind", value=0.6, threshold=0.5, window_seconds=60)]
    proposals = evaluator.evaluate(obs)
    assert len(proposals) == 1
    assert proposals[0].new_value == 0.1  # assuming current=0.0

# tests/test_meta_safety.py
def test_validator_rejects_forbidden_path():
    """Validator rejects modifications to forbidden paths."""
    validator = MetaSafetyValidator(
        forbidden_paths=["autonomy.hard_ask.*"],
        hard_bounds={},
    )
    proposal = ProposedAdjustment(
        parameter_path="autonomy.hard_ask.finance",
        new_value="something",
        old_value="other",
        # ...
    )
    result = validator.validate(proposal)
    assert not result.valid
    assert "forbidden" in result.reason.lower()

def test_validator_clamps_to_hard_bounds():
    """Adjustments outside hard bounds are rejected."""
    validator = MetaSafetyValidator(
        forbidden_paths=[],
        hard_bounds={"intention.min_salience": (0.2, 0.7)},
    )
    proposal = ProposedAdjustment(
        parameter_path="intention.min_salience",
        new_value=0.85,  # exceeds 0.7 max
        old_value=0.5,
    )
    result = validator.validate(proposal)
    assert not result.valid
```

### 8.2 Integration Tests

```python
# tests/test_meta_loop.py
async def test_full_meta_loop_cycle(test_daemon, fake_action_history):
    """Run one meta-loop tick and verify adjustments happen."""
    # Seed history with high approval rate for lights
    for _ in range(15):
        fake_action_history.add(make_action("light.turn_on", category="ask", approved=True))
    
    await test_daemon.meta_loop._tick()
    
    # Should have created a graduation proposal
    proposals = await test_daemon.approval_queue.list()
    assert any(p.parameter_path == "autonomy.domains.lights" for p in proposals)

async def test_meta_loop_cannot_modify_hard_ask(test_daemon):
    """Even if a policy tries, hard_ask paths are rejected."""
    # Create a malicious-looking policy
    bad_policy = AdjustmentPolicy(
        name="bypass_attempt",
        observation_kind="intent_repeat_rate",
        trigger_condition="above",
        threshold=0.0,  # always triggers
        parameter_path="autonomy.hard_ask.finance.transfer",
        direction="decrease",
        delta=1.0,
        min_value=0.0, max_value=1.0,
        cooldown_seconds=0,
    )
    test_daemon.meta_loop._evaluator._policies.append(bad_policy)
    
    await test_daemon.meta_loop._tick()
    
    # The hard_ask config should be unchanged
    config = await test_daemon.config_store.get_all()
    assert "finance.transfer" in [r.action_class for r in config["autonomy"]["hard_ask"]]

async def test_rollback_works(test_daemon):
    """Adjustments can be rolled back."""
    # Apply an adjustment
    record = await test_daemon.meta_adjuster.apply(make_proposal(
        parameter_path="intention.min_salience",
        old_value=0.35, new_value=0.40,
    ))
    
    # Verify applied
    assert (await test_daemon.config_store.get("intention.min_salience")) == 0.40
    
    # Rollback
    await test_daemon.meta_adjuster.rollback(record.adjustment_id)
    
    # Verify reverted
    assert (await test_daemon.config_store.get("intention.min_salience")) == 0.35
```

### 8.3 Safety Tests (Critical)

```python
async def test_safety_validator_blocks_all_forbidden_paths():
    """Every forbidden path is correctly blocked."""
    validator = MetaSafetyValidator(
        forbidden_paths=FORBIDDEN_PARAMETER_PATHS,
        hard_bounds=HARD_BOUNDS,
    )
    for forbidden in FORBIDDEN_PARAMETER_PATHS:
        # Test exact match or wildcard
        test_path = forbidden.replace("*", "test")
        proposal = make_proposal(parameter_path=test_path, new_value="x")
        result = validator.validate(proposal)
        assert not result.valid, f"Failed to block: {forbidden}"

async def test_safety_validator_enforces_all_hard_bounds():
    """Every hard bound is enforced."""
    validator = MetaSafetyValidator(
        forbidden_paths=[],
        hard_bounds=HARD_BOUNDS,
    )
    for path, (min_v, max_v) in HARD_BOUNDS.items():
        # Test exceeding max
        proposal = make_proposal(parameter_path=path, new_value=max_v + 1)
        assert not validator.validate(proposal).valid
        # Test below min
        proposal = make_proposal(parameter_path=path, new_value=min_v - 1)
        assert not validator.validate(proposal).valid
```

---

## 9. Success Criteria

- [ ] L8 observes all defined metrics on schedule
- [ ] Policies trigger correctly based on observations
- [ ] SafetyValidator rejects every forbidden path attempt
- [ ] All adjustments are persisted with reason and rollback info
- [ ] User approval flow works for graduation proposals
- [ ] CLI commands return expected output
- [ ] Dashboard displays current state
- [ ] All existing tests pass
- [ ] Safety tests cover 100% of forbidden paths and hard bounds
- [ ] After 1 week in production, at least 1 adjustment has been made (proving it works), but no forbidden paths have been touched (proving safety)
