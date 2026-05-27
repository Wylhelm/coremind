"""Safety validator for the meta-cognition layer (L8).

Enforces forbidden paths and hard bounds. Pure logic, no I/O.
Rejects proposals that would violate safety constraints.
"""

from __future__ import annotations

import fnmatch

from coremind.meta.schemas import ProposedAdjustment, ValidationResult


class MetaSafetyValidator:
    """Enforces forbidden paths and hard bounds on proposed adjustments."""

    def __init__(
        self,
        forbidden_paths: list[str],
        hard_bounds: dict[str, tuple[float, float]],
    ) -> None:
        self._forbidden_paths = forbidden_paths
        self._hard_bounds = hard_bounds

    def validate(self, proposal: ProposedAdjustment) -> ValidationResult:
        """Return valid=True if safe, valid=False with reason otherwise."""
        path = proposal.parameter_path

        # 1. Forbidden path check
        for pattern in self._forbidden_paths:
            if fnmatch.fnmatch(path, pattern):
                return ValidationResult(
                    valid=False,
                    reason=f"Parameter path '{path}' matches forbidden pattern '{pattern}'",
                )

        # 2. Hard bounds check
        bound = self._find_bound(path)
        if bound is not None:
            min_v, max_v = bound
            if proposal.new_value < min_v:
                return ValidationResult(
                    valid=False,
                    reason=(f"Value {proposal.new_value} below hard minimum {min_v} for '{path}'"),
                )
            if proposal.new_value > max_v:
                return ValidationResult(
                    valid=False,
                    reason=(f"Value {proposal.new_value} above hard maximum {max_v} for '{path}'"),
                )

        return ValidationResult(valid=True)

    def _find_bound(self, path: str) -> tuple[float, float] | None:
        """Find the matching hard bound — exact match first, then glob."""
        # Exact match takes priority
        if path in self._hard_bounds:
            return self._hard_bounds[path]

        # Glob match
        for pattern, bound in self._hard_bounds.items():
            if fnmatch.fnmatch(path, pattern):
                return bound

        return None
