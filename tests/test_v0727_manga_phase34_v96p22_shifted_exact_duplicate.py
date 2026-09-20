from __future__ import annotations

from pudge import manga_ocr_worker as worker


def lane(text="いいか", *, x=0.649816, y=0.6215, w=0.021421, h=0.051167, coverage=0.8305, conf=0.6609, detector="manga-ink-components-v1"):
    return {
        "text": text, "source": worker._LAYOUT_LINE_SOURCE,
        "orientation": "vertical", "detector": detector,
        "x": x, "y": y, "width": w, "height": h,
        "confidence": conf, "provenance": {"component_count": 3, "component_coverage": coverage},
    }


def test_v96p22_same_physical_p36_lane_suppresses_only_weak_shifted_duplicate():
    strong=lane()
    weak=lane(x=0.665605,y=0.6265,w=0.016158,h=0.043667,coverage=0.48,conf=0.6316)
    assert worker._region_iou(strong,weak)<0.30
    assert worker._suppress_exact_text_overlap_duplicates([strong,weak]) == [strong]
    assert worker._suppress_exact_text_overlap_duplicates([weak,strong]) == [strong]


def test_v96p22_same_text_in_neighbor_column_is_not_suppressed():
    a=lane(); b=lane(x=0.67,coverage=0.48,conf=0.6316)
    assert worker._suppress_exact_text_overlap_duplicates([a,b]) == [a,b]


def test_v96p22_no_suppression_without_unequal_physical_evidence():
    a=lane(); b=lane(x=0.665605,y=0.6265,w=0.016158,h=0.043667,coverage=0.72,conf=0.6316)
    assert worker._suppress_exact_text_overlap_duplicates([a,b]) == [a,b]


def test_v96p22_no_suppression_different_text_or_detector():
    a=lane(); kw={"x":0.665605,"y":0.6265,"w":0.016158,"h":0.043667,"coverage":0.48,"conf":0.6316}
    for b in (lane(text="いいね",**kw), lane(detector="manga-raw-components-v1",**kw)):
        assert worker._suppress_exact_text_overlap_duplicates([a,b]) == [a,b]


def test_v96p22_generation_markers():
    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
