from __future__ import annotations

from typing import Any, Mapping


_CONFIDENCE_RANK = {"rejected": 0, "unknown": 0, "C": 1, "B": 2, "A": 3}


def upgrade_is_better(
    previous: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None,
    *,
    minimum_gain: float,
) -> tuple[bool, str]:
    """Compare final prepared quality, never the candidate filename alone."""
    if not candidate or not bool(candidate.get("accepted")):
        return False, "candidate quality gate failed"
    candidate_score = float(candidate.get("score") or 0.0)
    candidate_rank = _CONFIDENCE_RANK.get(str(candidate.get("confidence") or "unknown"), 0)
    if not previous:
        return True, "first measured subtitle quality"
    previous_score = float(previous.get("score") or 0.0)
    previous_rank = _CONFIDENCE_RANK.get(str(previous.get("confidence") or "unknown"), 0)
    if candidate_rank < previous_rank:
        return False, "alignment confidence would decrease"
    gain = candidate_score - previous_score
    if candidate_rank > previous_rank and gain >= 0:
        return True, f"confidence improved; quality +{gain:.1f}"
    if gain >= max(0.0, float(minimum_gain)):
        return True, f"quality improved by {gain:.1f}"
    return False, f"quality gain {gain:.1f} is below {minimum_gain:.1f}"
