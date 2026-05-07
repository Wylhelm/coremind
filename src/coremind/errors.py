"""CoreMind error hierarchy.

All framework exceptions are rooted here so callers can catch
``CoreMindError`` for generic handling or specific subclasses for targeted
recovery.
"""


class CoreMindError(Exception):
    """Base class for all CoreMind exceptions."""


class SignatureError(CoreMindError):
    """Raised when a WorldEvent signature fails verification."""


class KeyManagementError(CoreMindError):
    """Raised when key generation, loading, or storage fails."""


class StoreError(CoreMindError):
    """Raised when a World Model store operation fails."""


class SummarizerError(CoreMindError):
    """Raised when an LLM summarizer fails to produce an episode summary."""


class SemanticMemoryError(CoreMindError):
    """Raised when a semantic memory operation (embed, store, search, delete) fails."""


class ProceduralMemoryError(CoreMindError):
    """Raised when a procedural memory operation (add, match, reinforce, deprecate) fails."""


class EmbeddingError(CoreMindError):
    """Raised when an embedding provider fails to produce a vector."""


class LLMError(CoreMindError):
    """Raised when an LLM call fails (transport, auth, parsing, or budget exceeded)."""


class ReasoningError(CoreMindError):
    """Raised when the reasoning layer (L4) cycle cannot complete."""


class IntentionError(CoreMindError):
    """Raised when the intention layer (L5) cycle cannot complete."""


class ActionError(CoreMindError):
    """Raised when the action layer (L6) cannot dispatch or execute an action."""


class ApprovalError(CoreMindError):
    """Raised when an approval request cannot be created or resolved."""


class NotificationError(CoreMindError):
    """Raised when a notification port cannot deliver a message."""


class JournalError(CoreMindError):
    """Raised when the audit journal cannot be read, written, or verified."""


class ForcedCategoryError(CoreMindError):
    """Raised when a plugin attempts to override a forced-approval action class."""


class ReflectionError(CoreMindError):
    """Raised when the reflection layer (L7) cycle cannot complete."""


class PredictionError(CoreMindError):
    """Raised when the predictive memory layer cannot generate or verify a prediction."""
