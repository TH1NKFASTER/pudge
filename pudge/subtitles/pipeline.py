from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..syncing import optimize_candidates as _optimize_candidates
from ..syncing import optimize_subtitle as _optimize_subtitle
from ..syncing import subtitle_quality_accepted
from .models import AlignmentResult, SubtitleJobStage


@dataclass(slots=True)
class AlignmentDecision:
    result: AlignmentResult
    accepted: bool
    quality_reason: str


class SubtitleAlignmentPipeline:
    """Typed boundary around the historical alignment engines.

    Callers now depend on explicit stages and a stable decision object.  The
    individual legacy algorithms can be extracted from ``syncing.py`` behind
    this boundary without another CLI/manager migration.
    """

    def __init__(self, *, stage_callback: Callable[[SubtitleJobStage], None] | None = None) -> None:
        self.stage_callback = stage_callback

    def _stage(self, stage: SubtitleJobStage) -> None:
        if self.stage_callback is not None:
            self.stage_callback(stage)

    def optimize_subtitle(self, *args: Any, **kwargs: Any) -> tuple[Path, dict[str, object]]:
        self._stage(SubtitleJobStage.NORMALIZING)
        self._stage(SubtitleJobStage.ALIGNING)
        path, result = _optimize_subtitle(*args, **kwargs)
        self._stage(SubtitleJobStage.VALIDATING)
        return path, result

    def optimize_candidates(self, *args: Any, **kwargs: Any) -> tuple[Any, Path, dict[str, object]]:
        self._stage(SubtitleJobStage.DISCOVERING)
        self._stage(SubtitleJobStage.ALIGNING)
        candidate, path, result = _optimize_candidates(*args, **kwargs)
        self._stage(SubtitleJobStage.VALIDATING)
        return candidate, path, result

    @staticmethod
    def decision(result: dict[str, Any]) -> AlignmentDecision:
        accepted, reason = subtitle_quality_accepted(result)
        return AlignmentDecision(AlignmentResult.from_mapping(result), accepted, reason)


_DEFAULT_PIPELINE = SubtitleAlignmentPipeline()
optimize_subtitle = _DEFAULT_PIPELINE.optimize_subtitle
optimize_candidates = _DEFAULT_PIPELINE.optimize_candidates
