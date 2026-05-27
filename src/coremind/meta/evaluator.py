"""Policy evaluator for the meta-cognition layer (L8).

Matches observations against policies, respects cooldowns, and proposes
adjustments. Synchronous, stateless (given inputs), no I/O.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from coremind.meta.protocols import AdjustmentHistoryProtocol, ConfigReaderProtocol
from coremind.meta.schemas import AdjustmentPolicy, MetaObservation, ProposedAdjustment

_PLACEHOLDER_RE = re.compile(r"<([^>]+)>")


class PolicyEvaluator:
    """Matches observations to policies and proposes adjustments."""

    def __init__(
        self,
        policies: list[AdjustmentPolicy],
        adjustment_history: AdjustmentHistoryProtocol,
        config_reader: ConfigReaderProtocol,
    ) -> None:
        self._policies = policies
        self._history = adjustment_history
        self._config = config_reader

    def evaluate(self, observations: list[MetaObservation]) -> list[ProposedAdjustment]:
        """Return proposed adjustments for all triggered policies."""
        proposals: list[ProposedAdjustment] = []

        for obs in observations:
            for policy in self._policies:
                proposal = self._evaluate_pair(obs, policy)
                if proposal is not None:
                    proposals.append(proposal)

        return proposals

    def _evaluate_pair(
        self,
        obs: MetaObservation,
        policy: AdjustmentPolicy,
    ) -> ProposedAdjustment | None:
        """Evaluate a single (observation, policy) pair."""
        # 1. Kind must match
        if policy.observation_kind != obs.kind:
            return None

        # 2. Policy must be enabled
        if not policy.enabled:
            return None

        # 3. Check trigger condition
        if not self._is_triggered(obs, policy):
            return None

        # 4. Resolve placeholder tokens in parameter path
        parameter_path = self._resolve_path(policy.parameter_path, obs.metadata)

        # 5. Check cooldown
        if self._is_on_cooldown(parameter_path, policy):
            return None

        # 6. Read current value
        old_value = self._config.get(parameter_path)

        # 7. Compute new value
        new_value = self._compute_new_value(old_value, policy, parameter_path)

        # 8. Clamp to policy bounds
        new_value = max(policy.min_value, min(policy.max_value, new_value))

        # 9. Skip if unchanged
        if new_value == old_value:
            return None

        return ProposedAdjustment(
            policy=policy,
            observation=obs,
            parameter_path=parameter_path,
            old_value=old_value,
            new_value=new_value,
        )

    def _is_triggered(self, obs: MetaObservation, policy: AdjustmentPolicy) -> bool:
        """Check whether the observation breaches the policy threshold."""
        if policy.trigger_condition == "above":
            return obs.value > policy.threshold
        if policy.trigger_condition == "below":
            return obs.value < policy.threshold
        # "between"
        upper = policy.threshold_upper if policy.threshold_upper is not None else policy.threshold
        return policy.threshold <= obs.value <= upper

    def _resolve_path(self, path: str, metadata: dict[str, object]) -> str:
        """Replace <placeholder> tokens with values from metadata."""

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            value = metadata.get(key)
            if value is None:
                return match.group(0)  # leave unresolved
            return str(value)

        return _PLACEHOLDER_RE.sub(_replace, path)

    def _is_on_cooldown(self, parameter_path: str, policy: AdjustmentPolicy) -> bool:
        """Check if the policy's cooldown has not yet elapsed."""
        last = self._history.last_adjustment(parameter_path)
        if last is None:
            return False
        cooldown = timedelta(seconds=policy.cooldown_seconds)
        return last.applied_at + cooldown > datetime.now(UTC)

    def _compute_new_value(
        self,
        old_value: float,
        policy: AdjustmentPolicy,
        parameter_path: str,
    ) -> float:
        """Compute the proposed new value based on policy direction and delta."""
        # Special case: poll_interval paths with delta=0.0 use multiplicative logic
        if policy.delta == 0.0 and "poll_interval" in parameter_path:
            if policy.direction == "increase":
                return old_value * 2.0
            return old_value * 0.5

        if policy.direction == "increase":
            return old_value + policy.delta
        return old_value - policy.delta
