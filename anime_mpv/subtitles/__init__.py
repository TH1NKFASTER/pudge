"""Typed building blocks for subtitle discovery, alignment and validation.

The legacy :mod:`anime_mpv.syncing` module remains the compatibility facade for
the CLI.  New code should exchange the models from this package instead of
growing more free-form dictionaries.
"""

from .models import (
    AlignmentConfidence,
    AlignmentResult,
    SubtitleJobStage,
    SubtitleQuality,
)

__all__ = [
    "AlignmentConfidence",
    "AlignmentResult",
    "SubtitleJobStage",
    "SubtitleQuality",
]
