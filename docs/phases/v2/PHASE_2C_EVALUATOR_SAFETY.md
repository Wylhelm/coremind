# Phase 2C — Policy Evaluator & Safety Validator

**Version:** 0.1 (Design)
**Status:** Draft
**Audience:** Coding agents (Opus in VS Code)

**Parent:** [PHASE_2_SELF_IMPROVEMENT.md](PHASE_2_SELF_IMPROVEMENT.md)
**Prerequisites:** Phase 2A (schemas/constants)
**Estimated effort:** 2–3 hours

---

## 1. Goal

Implement two pure-logic components:

1. **`PolicyEvaluator`** — matches observations against policies, respects cooldowns, proposes adjustments.
2. **`MetaSafetyValidator`** — rejects proposals that target forbidden paths or violate hard bounds.

Both are synchronous, stateless (given inputs), and trivially testable.

---

## 2. Deliverables

| File | Purpose |
| --- | --- |
| `src/coremind/meta/evaluator.py` | `PolicyEvaluator` class |
| `src/coremind/meta/safety_validator.py` | `MetaSafetyValidator` class |
| `tests/meta/test_evaluator.py` | Evaluator unit tests |
| `tests/meta/test_safety.py` | Safety validator unit tests (exhaustive) |

---

## 3. PolicyEvaluator

### 3.1 Interface

```python
class PolicyEvaluator:
    """Matches observations to policies and proposes adjustments."""

    def __init__(
        self,
        policies: list[AdjustmentPolicy],
        adjustment_history: AdjustmentHistoryProtocol,
        config_reader: ConfigReaderProtocol,
    ) -> None: ...

    def evaluate(self, observations: list[MetaObservation]) -> list[ProposedAdjustment]:
        """Return proposed adjustments for all triggered policies."""
        ...
```

### 3.2 Logic per observation

For each `(observation, policy)` pair where `policy.observation_kind == observation.kind`:

1. Check `policy.enabled` — skip if disabled.
2. Check trigger condition:
   - `"above"` → `observation.value > policy.threshold`
   - `"below"` → `observation.value < policy.threshold`
   - `"between"` → `policy.threshold <= observation.value <= policy.threshold_upper`
3. Resolve parameter path — substitute `<placeholders>` from `observation.metadata`.
4. Check cooldown — query `adjustment_history.last_adjustment(parameter_path)`. Skip if within cooldown.
5. Read current value — `config_reader.get(parameter_path)`.
6. Compute new value:
   - If `"poll_interval"` in path and `delta == 0.0`: multiply by 2 (increase) or 0.5 (decrease).
   - Otherwise: `old ± delta` based on direction.
7. Clamp to `[policy.min_value, policy.max_value]`.
8. Skip if `new_value == old_value`.
9. Yield `ProposedAdjustment`.

### 3.3 Protocols

```python
class AdjustmentHistoryProtocol(Protocol):
    def last_adjustment(self, parameter_path: str) -> AdjustmentRecord | None: ...

class ConfigReaderProtocol(Protocol):
    def get(self, dotted_path: str) -> float: ...
```

---

## 4. MetaSafetyValidator

### 4.1 Interface

```python
class MetaSafetyValidator:
    """Enforces forbidden paths and hard bounds. Pure logic, no I/O."""

    def __init__(
        self,
        forbidden_paths: list[str],
        hard_bounds: dict[str, tuple[float, float]],
    ) -> None: ...

    def validate(self, proposal: ProposedAdjustment) -> ValidationResult:
        """Return valid=True if safe, valid=False with reason otherwise."""
        ...
```

### 4.2 Logic

1. **Forbidden path check** — use `fnmatch.fnmatch(proposal.parameter_path, pattern)` for each forbidden pattern. Reject on match.
2. **Hard bounds check** — find matching bound (exact first, then glob). If `new_value < min` or `new_value > max`, reject.
3. Pass → `ValidationResult(valid=True)`.

---

## 5. Tests

### 5.1 Evaluator Tests

```python
# tests/meta/test_evaluator.py

def test_triggers_policy_above_threshold():
    """Observation above threshold produces a proposal."""

def test_does_not_trigger_below_threshold():
    """Observation below threshold produces nothing."""

def test_respects_cooldown():
    """Policy not triggered if last adjustment is within cooldown."""

def test_skips_disabled_policy():
    """Disabled policies are never triggered."""

def test_resolves_placeholder_in_path():
    """<plugin_id> in parameter_path is substituted from metadata."""

def test_clamps_to_policy_bounds():
    """new_value is clamped to [min_value, max_value]."""

def test_poll_interval_multiplied_not_added():
    """poll_interval paths with delta=0.0 use 2x/0.5x logic."""

def test_no_proposal_when_value_unchanged():
    """If clamping makes new_value == old_value, skip."""

def test_multiple_policies_same_observation():
    """Multiple policies can match a single observation."""
```

### 5.2 Safety Validator Tests (Exhaustive)

```python
# tests/meta/test_safety.py

def test_rejects_every_forbidden_path():
    """Iterate all FORBIDDEN_PARAMETER_PATHS and verify rejection."""
    validator = MetaSafetyValidator(FORBIDDEN_PARAMETER_PATHS, HARD_BOUNDS)
    for pattern in FORBIDDEN_PARAMETER_PATHS:
        test_path = pattern.replace("*", "anything")
        proposal = make_proposal(parameter_path=test_path, new_value=0.5)
        result = validator.validate(proposal)
        assert not result.valid, f"Should have blocked: {pattern}"

def test_rejects_above_hard_max():
    """Value above hard max is rejected."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)
    proposal = make_proposal(
        parameter_path="intention.min_salience",
        new_value=0.85,  # max is 0.70
    )
    assert not validator.validate(proposal).valid

def test_rejects_below_hard_min():
    """Value below hard min is rejected."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)
    proposal = make_proposal(
        parameter_path="intention.min_salience",
        new_value=0.10,  # min is 0.20
    )
    assert not validator.validate(proposal).valid

def test_accepts_value_within_bounds():
    """Value within bounds passes."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)
    proposal = make_proposal(
        parameter_path="intention.min_salience",
        new_value=0.45,
    )
    assert validator.validate(proposal).valid

def test_wildcard_bounds_match():
    """Glob patterns in HARD_BOUNDS match concrete paths."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)
    proposal = make_proposal(
        parameter_path="plugins.homeassistant.poll_interval_seconds",
        new_value=100000.0,  # above 86400 max
    )
    assert not validator.validate(proposal).valid

def test_unforbidden_path_passes():
    """A path not in forbidden list is allowed."""
    validator = MetaSafetyValidator(FORBIDDEN_PARAMETER_PATHS, {})
    proposal = make_proposal(
        parameter_path="intention.min_salience",
        new_value=0.5,
    )
    assert validator.validate(proposal).valid

def test_enforces_all_hard_bounds():
    """Every entry in HARD_BOUNDS is enforceable (min and max)."""
    validator = MetaSafetyValidator([], HARD_BOUNDS)
    for path_pattern, (min_v, max_v) in HARD_BOUNDS.items():
        concrete_path = path_pattern.replace("*", "test")
        # Exceeds max
        result = validator.validate(make_proposal(parameter_path=concrete_path, new_value=max_v + 1))
        assert not result.valid, f"Should block max for {path_pattern}"
        # Below min
        result = validator.validate(make_proposal(parameter_path=concrete_path, new_value=min_v - 1))
        assert not result.valid, f"Should block min for {path_pattern}"
```

---

## 6. Success Criteria

- [ ] `PolicyEvaluator.evaluate()` correctly proposes adjustments for all trigger conditions
- [ ] `MetaSafetyValidator.validate()` blocks every forbidden path
- [ ] `MetaSafetyValidator.validate()` enforces every hard bound
- [ ] Both classes are synchronous, pure logic, no I/O
- [ ] 100% coverage on safety-critical paths (forbidden + bounds)
- [ ] `mypy --strict` passes

---

## 7. Out of Scope

- Collecting observations (Phase 2B)
- Applying adjustments (Phase 2D)
- Persisting records (Phase 2D)
- CLI/dashboard (Phase 2E)
