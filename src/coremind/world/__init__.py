"""CoreMind World Model (L2) — entities, relationships, and event records."""

from coremind.world.compressed_prompt import CompressedPrompt, CompressedPromptBuilder
from coremind.world.pipeline import WorldEncodingPipeline

__all__ = [
    "CompressedPrompt",
    "CompressedPromptBuilder",
    "WorldEncodingPipeline",
]
