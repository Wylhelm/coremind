"""Self-Model plugin — personal context understanding for CoreMind.

This package gives CoreMind a continuously-updated model of the user:
identity, relationships, goals, projects, routines, and preferences.
Facts are extracted from passive observation of existing data sources
and enriched via LLM-powered inference at graduated confidence levels.
"""

from coremind.self_model.config import SelfModelConfig
from coremind.self_model.entities import (
    ConfidenceMethod,
    GoalEntity,
    IdentityEntity,
    PersonEntity,
    PreferenceEntity,
    ProjectEntity,
    RoutineEntity,
    SelfFact,
    SelfModelEntityType,
)
from coremind.self_model.errors import (
    ExtractionError,
    SelfModelError,
    SelfModelStoreError,
)

__all__ = [
    "ConfidenceMethod",
    "ExtractionError",
    "GoalEntity",
    "IdentityEntity",
    "PersonEntity",
    "PreferenceEntity",
    "ProjectEntity",
    "RoutineEntity",
    "SelfFact",
    "SelfModelConfig",
    "SelfModelEntityType",
    "SelfModelError",
    "SelfModelStoreError",
]
