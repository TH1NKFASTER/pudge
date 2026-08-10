from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


class SubtitleJobStage(StrEnum):
    QUEUED = "queued"
    DISCOVERING = "discovering"
    DOWNLOADING_CANDIDATES = "downloading_candidates"
    NORMALIZING = "normalizing"
    ALIGNING = "aligning"
    VALIDATING = "validating"
    SELECTING = "selecting"
    READY = "ready"
    WAITING_SOURCE = "waiting_source"
    RETRY_SCHEDULED = "retry_scheduled"
    NEEDS_ACTION = "needs_action"


class AlignmentConfidence(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class SubtitleQuality:
    """Comparable quality of the prepared subtitle, independent of its name."""

    score: float
    confidence: AlignmentConfidence
    accepted: bool
    reason: str
    identity_score: float = 0.0
    timing_score: float = 0.0
    structure_score: float = 0.0
    activity_score: float = 0.0
    holdout_p95_seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["confidence"] = self.confidence.value
        return payload


@dataclass(slots=True)
class AlignmentResult:
    """Stable typed view over an alignment engine result.

    ``details`` retains engine-specific diagnostics during the gradual split of
    the historical syncing module.  Callers can therefore migrate one stage at
    a time without losing regression information.
    """

    strategy: str
    accepted: bool
    confidence: AlignmentConfidence
    reason: str
    output_path: Path | None = None
    anchors: list[dict[str, Any]] = field(default_factory=list)
    residual_p95_seconds: float | None = None
    activity_before: float | None = None
    activity_after: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, result: Mapping[str, Any]) -> "AlignmentResult":
        validation = result.get("timing_reference_validation")
        validation = validation if isinstance(validation, Mapping) else {}
        repair = result.get("reference_piecewise_repair")
        repair = repair if isinstance(repair, Mapping) else {}
        holdout = repair.get("holdout_p95_seconds")
        try:
            holdout_value = float(holdout) if holdout is not None else None
        except (TypeError, ValueError):
            holdout_value = None
        confidence_raw = str(result.get("confidence") or "unknown")
        try:
            confidence = AlignmentConfidence(confidence_raw)
        except ValueError:
            confidence = AlignmentConfidence.UNKNOWN
        output = str(result.get("output") or "").strip()
        anchors = repair.get("timeline_anchors") or result.get("anchors") or []
        return cls(
            strategy=str(result.get("engine") or "unknown"),
            accepted=bool(result.get("sync_was_successful")),
            confidence=confidence,
            reason=str(result.get("reason") or "unknown"),
            output_path=Path(output) if output else None,
            anchors=[dict(item) for item in anchors if isinstance(item, Mapping)],
            residual_p95_seconds=holdout_value,
            details=dict(result),
        )

    def as_dict(self) -> dict[str, Any]:
        payload = dict(self.details)
        payload.update(
            {
                "engine": self.strategy,
                "sync_was_successful": self.accepted,
                "confidence": self.confidence.value,
                "reason": self.reason,
                "output": str(self.output_path or ""),
            }
        )
        return payload
