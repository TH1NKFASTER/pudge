from __future__ import annotations

from pudge import manga_ocr_worker as worker


def _region(
    text: str,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    confidence: float,
    detector: str,
    component_count: int,
    component_coverage: float,
) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": text,
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": confidence,
        "detector": detector,
        "source": worker._LAYOUT_LINE_SOURCE,
        "geometry_status": "approximate",
        "segments": [
            {
                "text": ch,
                "orientation": "vertical",
                "x": x,
                "y": y + i * (height / max(1, len(text))),
                "width": width,
                "height": height / max(1, len(text)),
                "source": "layout-line-ink-v2+tight-v1",
                "geometry_status": "approximate",
            }
            for i, ch in enumerate(text)
        ],
        "provenance": {
            "component_count": component_count,
            "component_coverage": component_coverage,
        },
    }


def test_v96p14_suppresses_weaker_terminal_glyph_conflict_on_same_vertical_lane() -> None:
    suppress = getattr(worker, "_suppress_terminal_glyph_conflict_duplicates", None)
    assert suppress is not None

    # Real p32 geometry: both candidates describe the same bubble column.
    # The weak ink-components retry reads the last glyph as し, while the
    # stronger raw-components lane reads the visible と in 何事かと.
    weak = _region(
        "何事かし",
        x=0.097184,
        y=0.169,
        width=0.025368,
        height=0.048667,
        confidence=0.6415,
        detector="manga-ink-components-v1",
        component_count=3,
        component_coverage=0.5,
    )
    strong = _region(
        "何事かと",
        x=0.077632,
        y=0.1475,
        width=0.030263,
        height=0.07,
        confidence=0.756,
        detector="manga-raw-components-v1",
        component_count=5,
        component_coverage=1.0122,
    )

    cleaned = suppress([weak, strong])
    assert [row["text"] for row in cleaned] == ["何事かと"]


def test_v96p14_keeps_terminal_disagreement_without_stronger_physical_evidence() -> None:
    suppress = getattr(worker, "_suppress_terminal_glyph_conflict_duplicates", None)
    assert suppress is not None

    left = _region(
        "今日は雨",
        x=0.40,
        y=0.20,
        width=0.03,
        height=0.08,
        confidence=0.80,
        detector="manga-ink-components-v1",
        component_count=4,
        component_coverage=0.95,
    )
    right = _region(
        "今日は雪",
        x=0.39,
        y=0.20,
        width=0.03,
        height=0.08,
        confidence=0.79,
        detector="manga-raw-components-v1",
        component_count=4,
        component_coverage=0.96,
    )

    assert suppress([left, right]) == [left, right]
