from __future__ import annotations

import math
from typing import Any, Mapping

from .models import AlignmentConfidence, SubtitleQuality


def _number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _candidate_context(result: Mapping[str, Any]) -> Mapping[str, Any]:
    value = result.get("candidate_context")
    return value if isinstance(value, Mapping) else {}


def quality_from_result(
    result: Mapping[str, Any],
    *,
    accepted: bool,
    reason: str,
) -> SubtitleQuality:
    """Build a stable 0..100 quality score from independent evidence.

    Filename/source identity is deliberately capped at 25 points.  A candidate
    can only become an automatic upgrade when its final timing, structure and
    activity are at least as good as the previous prepared subtitle.
    """
    context = _candidate_context(result)
    identity = 0.0
    if context.get("entry_anilist_match"):
        identity += 12.0
    if context.get("episode_match") in {"exact", "range"}:
        identity += 8.0
    identity += min(5.0, max(0.0, _number(context.get("title_similarity")) - 70.0) / 6.0)

    alignment = _number(result.get("alignment_score"))
    # ffsubsync scores are not calibrated probabilities; a smooth saturating
    # transform keeps large values useful without letting them dominate.
    timing = min(30.0, max(0.0, 15.0 + math.copysign(math.log1p(abs(alignment)), alignment) * 2.5))
    if result.get("reference_alignment_reliable"):
        timing = 30.0

    structure = 0.0
    validation = result.get("timing_reference_validation")
    if isinstance(validation, Mapping):
        detail = validation.get("reference_output_structure")
        if not isinstance(detail, Mapping):
            detail = validation.get("structure")
        if isinstance(detail, Mapping):
            structure = 20.0 * min(1.0, max(0.0, _number(detail.get("retained_ratio"))))
    if not structure and result.get("sync_was_successful"):
        structure = 12.0

    activity = 0.0
    activity_payload = result.get("reference_activity")
    if not isinstance(activity_payload, Mapping) and isinstance(validation, Mapping):
        activity_payload = validation.get("reference_activity")
    if isinstance(activity_payload, Mapping) and activity_payload.get("available"):
        activity = 25.0 * min(1.0, max(0.0, _number(activity_payload.get("weighted"))))
    elif result.get("sync_was_successful"):
        activity = 12.0

    repair = result.get("reference_piecewise_repair")
    holdout = None
    if isinstance(repair, Mapping):
        raw = repair.get("holdout_p95_seconds")
        holdout = _number(raw, default=float("nan")) if raw is not None else None
        if holdout is not None and not math.isfinite(holdout):
            holdout = None
    score = max(0.0, min(100.0, identity + timing + structure + activity))
    if not accepted:
        score = min(score, 39.0)

    if not accepted:
        confidence = AlignmentConfidence.REJECTED
    elif result.get("reference_alignment_reliable") and (holdout is None or holdout <= 1.0):
        confidence = AlignmentConfidence.A
    elif timing >= 22.0 and structure >= 15.0:
        confidence = AlignmentConfidence.B
    else:
        confidence = AlignmentConfidence.C
    return SubtitleQuality(
        score=round(score, 3),
        confidence=confidence,
        accepted=accepted,
        reason=reason,
        identity_score=round(identity, 3),
        timing_score=round(timing, 3),
        structure_score=round(structure, 3),
        activity_score=round(activity, 3),
        holdout_p95_seconds=holdout,
    )
