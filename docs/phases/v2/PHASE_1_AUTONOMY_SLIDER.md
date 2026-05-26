# Phase 1 — Autonomy Slider

**Target:** CoreMind v2
**Duration estimate:** 1 week
**Agent:** Opus in VS Code
**Prerequisites:** None (can be implemented first)

---

## 1. Problem Statement

### Current State

CoreMind v1 uses a **binary forced-category system** defined in `src/coremind/action/schemas.py`:

```python
type ActionCategory = Literal["safe", "suggest", "ask", "conversation"]
```

And enforced in `src/coremind/action/action_classes.py`:

```python
# Destructive operations are hardcoded ASK
_ASK_CLASSES = {"vacuum", "lock", "garage_door", "finance.transfer", ...}
# Informational are hardcoded SAFE
_SAFE_CLASSES = {"calendar", "weather", "vikunja.get", ...}
# Everything else defaults to SUGGEST
```

### Problems

1. **No per-domain differentiation.** Finance and vacuum are both ASK. Lights and HVAC are both SUGGEST. The user cannot say "I trust you with lights at 0.9 but keep locks at 0.1."

2. **No learning.** The system never graduates from ASK→SUGGEST→SAFE even if the user approves 100 times in a row.

3. **No granularity.** A 99%-confident action and a 51%-confident action are treated identically.

4. **User frustration.** The notification journal suppresses repeats, but doesn't address *why* repeats happen.

### Goal

Replace the binary system with a **per-domain graduated autonomy slider** (float 0.0 to 1.0) where:
- `0.0` = Always ASK (even with 100% confidence)
- `1.0` = Always auto-execute (even with low confidence — the user trusts the system completely for this domain)
- Defaults start conservative and can be adjusted by the user
- L7/L8 can **propose** slider increases based on approval history

---

## 2. Design

### 2.1 Domain Registry

Each domain maps to one or more `action_class` prefixes:

| Domain | Default Slider | Action Classes | Rationale |
|---|---|---|---|
| `lights` | 0.8 | `light.*` | Low risk, easily reversible |
| `hvac` | 0.7 | `climate.*`, `thermostat.*` | Comfort, moderate energy impact |
| `calendar` | 0.8 | `calendar.*` | Read-only mostly |
| `weather` | 1.0 | `weather.*` | Purely informational |
| `vacuum` | 0.3 | `vacuum.*`, `robot.*` | Noise, cat disturbance, moderate risk |
| `locks` | 0.1 | `lock.*`, `garage_door.*` | Security — almost always ASK |
| `finance` | 0.1 | `finance.*`, `transaction.*` | Money — always verify |
| `messaging` | 0.2 | `messaging.*`, `email.send.*`, `notification.send.*` | External communication |
| `health` | 0.5 | `health.*` | Personal data, moderate sensitivity |
| `media` | 0.7 | `media.*`, `speaker.*`, `tv.*` | Entertainment, low risk |
| `presence` | 0.6 | `presence.*`, `safety.presence_alert` | Notifications about user state |
| `default` | 0.4 | All others | Conservative fallback |

### 2.2 Decision Algorithm

```python
def resolve_agency(
    action_class: str,
    confidence: float,
    slider_config: AutonomyConfig,
) -> ActionCategory:
    """Determine whether to auto-execute, suggest, or ask.
    
    Args:
        action_class: The action's class (e.g., "light.toggle")
        confidence: LLM's confidence in this action (0.0-1.0)
        slider_config: User's per-domain slider settings
    
    Returns:
        "safe" = auto-execute, "suggest" = notify + grace, "ask" = block
    """
    # 1. Resolve domain from action_class
    domain = _classify_domain(action_class, slider_config.domains)
    
    # 2. Get slider value for this domain
    slider = slider_config.get_slider(domain)
    
    # 3. Hard safety overrides (always ASK regardless of slider)
    if slider_config.is_hard_ask(action_class):
        return "ask"
    
    # 4. Decision threshold:
    #    confidence >= slider           → "safe" (auto-execute)
    #    confidence >= slider * 0.6     → "suggest" (notify + grace)
    #    otherwise                      → "ask" (block)
    if confidence >= slider:
        return "safe"
    elif confidence >= slider * 0.6:
        return "suggest"
    else:
        return "ask"
```

**Example scenarios:**

| Domain | Slider | Confidence | Result | Reasoning |
|---|---|---|---|---|
| lights | 0.8 | 0.95 | safe | High trust domain, high confidence |
| lights | 0.8 | 0.70 | suggest | Trusted domain but low confidence |
| lights | 0.8 | 0.40 | ask | Below suggest threshold |
| finance | 0.1 | 0.99 | ask | Hard ASK override for finance |
| vacuum | 0.3 | 0.85 | safe | Slider says 0.3, confidence 0.85 > 0.3 |
| vacuum | 0.3 | 0.25 | suggest | Below slider but above 0.3*0.6=0.18 |

### 2.3 Configuration Schema

```toml
# ~/.coremind/config.toml

[autonomy]
# Global default slider for unclassified domains
default_slider = 0.4

# Per-domain sliders
[autonomy.domains]
lights = 0.8
hvac = 0.7
calendar = 0.8
weather = 1.0
vacuum = 0.3
locks = 0.1
finance = 0.1
messaging = 0.2
health = 0.5
media = 0.7
presence = 0.6

# Hard ASK classes — NEVER auto-execute, regardless of slider
[autonomy.hard_ask]
classes = [
    "finance.transfer",
    "finance.payment",
    "lock.unlock",
    "garage_door.open",
    "messaging.send_external",
    "email.send",
    "security.disable",
    "plugin.install",
    "config.modify",
]

# Hard SAFE classes — NEVER ask for approval
[autonomy.hard_safe]
classes = [
    "calendar.fetch",
    "weather.fetch",
    "vikunja.get",
    "health.read",
]

# Allow L8 to propose slider adjustments
[autonomy.graduation]
enabled = true
min_approvals_before_promotion = 10      # Must approve 10 times before suggesting promotion
min_approval_rate_for_promotion = 0.8    # Must have >80% approval rate
max_promotion_per_proposal = 0.1         # Max increase per proposal
promotion_cooldown_days = 7              # At most one promotion per domain per week
require_user_approval = true             # Graduation proposals must be approved by user
```

### 2.4 Graduation Mechanism

When L7/L8 detects a domain with consistently high approval rates, it proposes a slider increase:

```python
class SliderGraduationProposal(BaseModel):
    """Proposal to increase a domain's autonomy slider."""
    domain: str
    current_slider: float
    proposed_slider: float
    approval_rate: float           # e.g., 0.92 (92% of actions approved)
    total_actions: int             # Total actions in observation window
    approved_actions: int
    observation_window_days: int   # How long we've been observing
    
class GraduationPolicy:
    """Evaluates whether a domain qualifies for slider promotion."""
    
    def evaluate(
        self,
        domain: str,
        action_history: list[Action],
        config: GraduationConfig,
    ) -> SliderGraduationProposal | None:
        """Return a promotion proposal if the domain qualifies, else None."""
        
        # Filter actions for this domain in the observation window
        cutoff = datetime.now(UTC) - timedelta(days=config.min_observation_days)
        relevant = [a for a in action_history 
                    if _classify_domain(a.action_class) == domain 
                    and a.timestamp >= cutoff
                    and a.result is not None]
        
        if len(relevant) < config.min_approvals_before_promotion:
            return None
        
        approved = sum(1 for a in relevant 
                      if a.result.status in ("ok", "approved"))
        approval_rate = approved / len(relevant)
        
        if approval_rate < config.min_approval_rate_for_promotion:
            return None
        
        # Calculate new slider (cap at max_promotion_per_proposal increase)
        increase = min(config.max_promotion_per_proposal, 1.0 - current_slider)
        new_slider = current_slider + increase
        
        return SliderGraduationProposal(
            domain=domain,
            current_slider=current_slider,
            proposed_slider=new_slider,
            approval_rate=approval_rate,
            total_actions=len(relevant),
            approved_actions=approved,
            observation_window_days=config.min_observation_days,
        )
```

---

## 3. Files to Create/Modify

### New Files

| File | Purpose |
|---|---|
| `src/coremind/action/autonomy.py` | AutonomyConfig, DomainSlider, resolve_agency(), domain classification |
| `src/coremind/action/graduation.py` | SliderGraduationProposal, GraduationPolicy |
| `src/coremind/action/schemas_autonomy.py` | Autonomy-specific Pydantic models |

### Modified Files

| File | Change |
|---|---|
| `src/coremind/action/schemas.py` | Keep `ActionCategory` for backward compatibility, add compatibility layer |
| `src/coremind/action/router.py` | Replace `get_forced_category()` with `resolve_agency()` |
| `src/coremind/action/executor.py` | Use `resolve_agency()` instead of hardcoded category checks |
| `src/coremind/action/action_classes.py` | Deprecate — replace with domain-based classification |
| `src/coremind/core/config.py` | Add `AutonomyConfig` to `DaemonConfig` |
| `src/coremind/core/daemon.py` | Initialize autonomy system, load slider config |
| `src/coremind/cli/__init__.py` | Add `coremind autonomy` commands |
| `src/coremind/dashboard/views.py` | Add slider UI to dashboard |
| `~/.coremind/config.toml` | Add `[autonomy]` section |

---

## 4. Data Model

### 4.1 AutonomyConfig

```python
class DomainConfig(BaseModel):
    """Configuration for a single domain."""
    slider: float = Field(default=0.5, ge=0.0, le=1.0)
    action_classes: list[str] = Field(default_factory=list)
    description: str = ""

class HardAskRule(BaseModel):
    """A rule that forces ASK regardless of slider."""
    action_class: str
    reason: str = ""

class HardSafeRule(BaseModel):
    """A rule that forces SAFE regardless of slider."""
    action_class: str

class GraduationConfig(BaseModel):
    """Configuration for the slider graduation mechanism."""
    enabled: bool = True
    min_approvals_before_promotion: int = 10
    min_approval_rate_for_promotion: float = 0.8
    max_promotion_per_proposal: float = 0.1
    min_observation_days: int = 30
    promotion_cooldown_days: int = 7
    require_user_approval: bool = True

class AutonomyConfig(BaseModel):
    """Complete autonomy configuration."""
    default_slider: float = Field(default=0.4, ge=0.0, le=1.0)
    domains: dict[str, float] = Field(default_factory=dict)
    hard_ask: list[HardAskRule] = Field(default_factory=list)
    hard_safe: list[HardSafeRule] = Field(default_factory=list)
    graduation: GraduationConfig = Field(default_factory=GraduationConfig)
    
    def get_slider(self, domain: str) -> float:
        return self.domains.get(domain, self.default_slider)
    
    def is_hard_ask(self, action_class: str) -> bool:
        return any(rule.action_class == action_class for rule in self.hard_ask)
    
    def is_hard_safe(self, action_class: str) -> bool:
        return any(rule.action_class == action_class for rule in self.hard_safe)
```

### 4.2 Domain Classification

```python
# Classification lookup: action_class prefix → domain
_DOMAIN_CLASSIFICATION: dict[str, str] = {
    "light": "lights",
    "switch.light": "lights",
    "climate": "hvac",
    "thermostat": "hvac",
    "calendar": "calendar",
    "weather": "weather",
    "vacuum": "vacuum",
    "robot": "vacuum",
    "lock": "locks",
    "garage_door": "locks",
    "finance": "finance",
    "transaction": "finance",
    "messaging": "messaging",
    "email.send": "messaging",
    "notification.send": "messaging",
    "health": "health",
    "media": "media",
    "speaker": "media",
    "tv": "media",
    "presence": "presence",
    "notify_user": "presence",
}


def classify_action(action_class: str) -> str:
    """Map an action_class to its domain.
    
    Uses longest-prefix matching: "finance.transfer" matches "finance" before "transfer" alone.
    """
    # Sort by key length descending for longest-prefix match
    sorted_classes = sorted(_DOMAIN_CLASSIFICATION.items(), key=lambda x: -len(x[0]))
    for prefix, domain in sorted_classes:
        if action_class.startswith(prefix):
            return domain
    return "default"
```

### 4.3 Database Migration

Store slider adjustments in SurrealDB for audit:

```sql
DEFINE TABLE autonomy_change SCHEMAFULL;
DEFINE FIELD domain ON autonomy_change TYPE string;
DEFINE FIELD old_slider ON autonomy_change TYPE float;
DEFINE FIELD new_slider ON autonomy_change TYPE float;
DEFINE FIELD reason ON autonomy_change TYPE string;
DEFINE FIELD changed_by ON autonomy_change TYPE string;  -- "user" or "meta_loop"
DEFINE FIELD changed_at ON autonomy_change TYPE datetime;
DEFINE FIELD approval_id ON autonomy_change OPTION TYPE string;  -- if meta-proposed
```

---

## 5. API & CLI

### 5.1 CLI Commands

```bash
# View current autonomy settings
$ coremind autonomy show
Domain          Slider   Class
──────────────────────────────────────
lights          0.80     auto-execute (confidence ≥ 0.80)
hvac            0.70     auto-execute (confidence ≥ 0.70)
calendar        0.80     auto-execute (confidence ≥ 0.80)
weather         1.00     auto-execute (always, low risk)
vacuum          0.30     auto-execute (confidence ≥ 0.30)
locks           0.10     auto-execute (confidence ≥ 0.10) [HARD ASK overrides]
finance         0.10     [HARD ASK always]
messaging       0.20     auto-execute (confidence ≥ 0.20)
health          0.50     auto-execute (confidence ≥ 0.50)
media           0.70     auto-execute (confidence ≥ 0.70)
presence        0.60     auto-execute (confidence ≥ 0.60)
default         0.40     auto-execute (confidence ≥ 0.40)

# Set a domain slider
$ coremind autonomy set lights 0.9
✓ lights slider updated: 0.80 → 0.90

# Show pending graduation proposals
$ coremind autonomy proposals
Domain          Current   Proposed   Approval   Actions
──────────────────────────────────────────────────────────
lights          0.80      0.90       94%        47/50
media           0.70      0.80       89%        32/36

# Approve a proposal
$ coremind autonomy approve lights
✓ lights slider promoted to 0.90
```

### 5.2 Dashboard UI

Add an **Autonomy** page (`/autonomy`) with:
- Slider controls per domain (range inputs)
- Color coding: green (≥0.7), yellow (0.3–0.7), red (<0.3)
- Hard overrides displayed as lock icons
- Graduation proposals panel with approve/deny buttons
- Change history table

### 5.3 API Endpoints

```python
# New dashboard API routes
@routes.get("/api/autonomy")
async def autonomy_config_json(request: web.Request) -> web.Response:
    """Return current autonomy configuration as JSON."""
    ...

@routes.post("/api/autonomy/set")
async def autonomy_set(request: web.Request) -> web.Response:
    """Update a domain slider. Body: {domain: str, slider: float}."""
    ...

@routes.get("/api/autonomy/proposals")
async def autonomy_proposals(request: web.Request) -> web.Response:
    """Return pending graduation proposals."""
    ...

@routes.post("/api/autonomy/proposals/{proposal_id}/approve")
async def autonomy_approve_proposal(request: web.Request) -> web.Response:
    """Approve a graduation proposal."""
    ...
```

---

## 6. Migration Path

### Phase 1: Dual System (Week 1)

1. Add `AutonomyConfig` with default sliders that map 1:1 to existing categories:
   - SAFE actions → slider 1.0
   - SUGGEST actions → slider 0.5
   - ASK actions → slider 0.1
   - CONVERSATION → slider 0.4

2. `resolve_agency()` produces the SAME results as `get_forced_category()` with these defaults.

3. All existing tests pass without modification.

### Phase 2: Enable Sliders (Week 1–2)

1. Add `[autonomy]` config section to `config.toml`.
2. Dashboard shows slider controls.
3. CLI commands work.
4. System runs with slider-based decisions.

### Phase 3: Graduation (Week 2+)

1. L7 reflection starts tracking approval rates per domain.
2. Graduation proposals appear in dashboard.
3. User approves → slider increases.

### Backward Compatibility

- `ActionCategory` Literal remains unchanged.
- `get_forced_category()` becomes a wrapper around `resolve_agency()`.
- Old config files without `[autonomy]` use defaults that match v1 behavior.

---

## 7. Tests

### 7.1 Unit Tests

```python
# tests/test_autonomy.py

class TestDomainClassification:
    def test_light_domain(self):
        assert classify_action("light.turn_on") == "lights"
        assert classify_action("light.set_brightness") == "lights"
    
    def test_finance_is_always_classified(self):
        assert classify_action("finance.transfer.send") == "finance"
        assert classify_action("finance.check_balance") == "finance"
    
    def test_unknown_falls_to_default(self):
        assert classify_action("some.unknown.operation") == "default"


class TestAutonomyConfig:
    def test_get_slider_returns_domain_value(self):
        config = AutonomyConfig(domains={"lights": 0.8, "finance": 0.1})
        assert config.get_slider("lights") == 0.8
    
    def test_get_slider_falls_back_to_default(self):
        config = AutonomyConfig(default_slider=0.4, domains={})
        assert config.get_slider("unknown") == 0.4
    
    def test_hard_ask_identified_correctly(self):
        config = AutonomyConfig(
            hard_ask=[HardAskRule(action_class="finance.transfer", reason="money")]
        )
        assert config.is_hard_ask("finance.transfer") is True
        assert config.is_hard_ask("light.toggle") is False


class TestResolveAgency:
    def test_high_trust_high_confidence_returns_safe(self):
        config = AutonomyConfig(domains={"lights": 0.8})
        result = resolve_agency("light.turn_on", confidence=0.95, slider_config=config)
        assert result == "safe"
    
    def test_high_trust_low_confidence_returns_suggest(self):
        config = AutonomyConfig(domains={"lights": 0.8})
        result = resolve_agency("light.turn_on", confidence=0.65, slider_config=config)
        assert result == "suggest"
    
    def test_hard_ask_overrides_everything(self):
        config = AutonomyConfig(
            domains={"finance": 1.0},  # slider at max!
            hard_ask=[HardAskRule(action_class="finance.transfer", reason="money")]
        )
        result = resolve_agency("finance.transfer", confidence=0.99, slider_config=config)
        assert result == "ask"  # Hard override wins
    
    def test_low_trust_always_ask(self):
        config = AutonomyConfig(domains={"locks": 0.1})
        result = resolve_agency("lock.unlock", confidence=0.05, slider_config=config)
        assert result == "ask"
    
    def test_suggest_threshold(self):
        # slider=0.5, suggest threshold = 0.5*0.6 = 0.3
        config = AutonomyConfig(domains={"media": 0.5})
        assert resolve_agency("media.set_volume", 0.6, config) == "safe"
        assert resolve_agency("media.set_volume", 0.4, config) == "suggest"
        assert resolve_agency("media.set_volume", 0.2, config) == "ask"
```

### 7.2 Integration Tests

```python
@pytest.mark.integration
async def test_autonomy_config_loaded_on_startup(test_daemon):
    """Daemon loads autonomy config from config.toml."""
    config = test_daemon.autonomy_config
    assert config.domains["lights"] == 0.8

@pytest.mark.integration
async def test_slider_update_applied_live(test_daemon):
    """Updating a slider takes effect immediately."""
    test_daemon.autonomy_config.domains["lights"] = 0.9
    result = resolve_agency("light.turn_on", 0.85, test_daemon.autonomy_config)
    assert result == "suggest"  # 0.85 < 0.9 → suggest instead of safe

@pytest.mark.integration  
async def test_graduation_proposal_generated(test_daemon, action_history):
    """After 10+ approvals at >80% rate, graduation proposal appears."""
    # Seed 10 approved actions for lights domain
    for _ in range(10):
        action_history.append(make_action("light.turn_on", result="ok"))
    
    proposals = await test_daemon.get_graduation_proposals()
    assert any(p.domain == "lights" for p in proposals)
```

---

## 8. Success Criteria

- [ ] User can set per-domain slider via CLI and dashboard
- [ ] `resolve_agency()` produces correct category for all test cases
- [ ] Hard ASK/SAFE overrides work regardless of slider
- [ ] Existing actions continue to work (backward compatible)
- [ ] Graduation proposals appear when thresholds met
- [ ] User can approve/deny graduation proposals
- [ ] All existing tests pass
- [ ] New tests cover all decision paths
