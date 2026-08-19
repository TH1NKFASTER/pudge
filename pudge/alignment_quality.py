from __future__ import annotations

from itertools import pairwise
from typing import Any, Iterable


ALIGNMENT_REPORT_SCHEMA = "pudge-alignment-report-v1"


def build_alignment_report(
    alignment: dict[str, Any],
    source_chapters: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    sources = [dict(row) for row in source_chapters if isinstance(row, dict)]
    source_by_index = {
        int(row.get("chapter_index") or 0): row
        for row in sources
    }
    aligned = [dict(row) for row in alignment.get("chapters") or [] if isinstance(row, dict)]
    aligned_indexes = {int(row.get("chapter_index") or 0) for row in aligned}
    total_characters = sum(max(0, int(row.get("normalized_length") or len(str(row.get("text") or "")))) for row in sources)
    matched_characters = sum(max(0, int(row.get("normalized_length") or 0)) for row in aligned)
    warnings: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []

    for row in aligned:
        chapter_index = int(row.get("chapter_index") or 0)
        length = max(1, int(row.get("normalized_length") or 0))
        anchors = [dict(anchor) for anchor in row.get("anchors") or [] if isinstance(anchor, dict)]
        long_gaps: list[dict[str, Any]] = []
        for left, right in pairwise(anchors):
            seconds = float(right.get("time") or 0.0) - float(left.get("time") or 0.0)
            characters = float(right.get("offset") or 0.0) - float(left.get("offset") or 0.0)
            if seconds >= 2.0 and characters >= 2.0:
                long_gaps.append(
                    {
                        "start_offset": float(left.get("offset") or 0.0),
                        "end_offset": float(right.get("offset") or 0.0),
                        "seconds": round(seconds, 3),
                        "characters": round(characters, 3),
                    }
                )
        confidence = float(row.get("confidence") or 0.0)
        density = len(anchors) * 100.0 / length
        chapter_report = {
            "chapter_index": chapter_index,
            "title": str(row.get("title") or source_by_index.get(chapter_index, {}).get("title") or ""),
            "confidence": round(confidence, 4),
            "normalized_length": length,
            "anchor_count": len(anchors),
            "anchors_per_100_characters": round(density, 2),
            "punctuation_pause_count": int(row.get("punctuation_pause_count") or 0),
            "long_gaps": long_gaps,
        }
        chapters.append(chapter_report)
        if confidence < 0.55:
            warnings.append({"kind": "low_confidence", "chapter_index": chapter_index, "value": round(confidence, 4)})
        if density < 3.0:
            warnings.append({"kind": "sparse_anchors", "chapter_index": chapter_index, "value": round(density, 2)})
        if long_gaps:
            warnings.append({"kind": "long_interpolation", "chapter_index": chapter_index, "count": len(long_gaps)})

    for chapter_index, row in sorted(source_by_index.items()):
        if chapter_index not in aligned_indexes:
            warnings.append(
                {
                    "kind": "unmatched_chapter",
                    "chapter_index": chapter_index,
                    "title": str(row.get("title") or ""),
                }
            )

    coverage = matched_characters / max(1, total_characters)
    confidence = float(alignment.get("confidence") or 0.0)
    if coverage >= 0.90 and confidence >= 0.85 and not any(row["kind"] == "unmatched_chapter" for row in warnings):
        grade = "good"
    elif coverage >= 0.65 and confidence >= 0.60:
        grade = "review"
    else:
        grade = "poor"
    return {
        "schema": ALIGNMENT_REPORT_SCHEMA,
        "grade": grade,
        "summary": {
            "source_chapters": len(sources),
            "matched_chapters": len(aligned),
            "coverage": round(coverage, 4),
            "confidence": round(confidence, 4),
            "anchor_count": int(alignment.get("anchor_count") or 0),
            "punctuation_pause_count": int(alignment.get("punctuation_pause_count") or 0),
            "warning_count": len(warnings),
        },
        "warnings": warnings,
        "chapters": chapters,
    }
