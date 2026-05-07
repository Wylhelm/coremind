"""Predictive memory layer for CoreMind.

This module provides the PredictiveMemory class that sits between the reasoning
loop (L4) and the intention loop (L5). It stores temporal patterns and uses them
to generate falsifiable predictions about near-future state.

The predictive memory uses the existing SemanticMemory backend to store and
retrieve patterns, then computes confidence scores and generates predictions
that can be consumed by the intention loop for proactive intent generation.
"""

from coremind.prediction.predictor import PredictiveMemory
from coremind.prediction.schemas import Prediction, PredictionEvidence, PredictionStatus

__all__ = [
    "Prediction",
    "PredictionEvidence",
    "PredictionStatus",
    "PredictiveMemory",
]
