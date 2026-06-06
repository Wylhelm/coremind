"""Self-Model exception hierarchy.

All self-model errors descend from :class:`SelfModelError`, which itself
inherits from :class:`~coremind.errors.CoreMindError`.
"""

from coremind.errors import CoreMindError


class SelfModelError(CoreMindError):
    """Base class for all self-model exceptions."""


class SelfModelStoreError(SelfModelError):
    """Raised when a self-model store operation (CRUD, migration) fails."""


class ExtractionError(SelfModelError):
    """Raised when the LLM extraction pipeline fails to produce valid facts."""


class CollectorError(SelfModelError):
    """Raised when a data collector cannot fetch or parse source data."""


class ConfidenceError(SelfModelError):
    """Raised when a confidence value violates invariants (e.g. declared < 0.95)."""
