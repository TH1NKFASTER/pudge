import pudge.manga_ocr_worker as worker


def _segment(text: str) -> dict[str, object]:
    return {
        "text": text,
        "x": 0.40,
        "y": 0.40,
        "width": 0.02,
        "height": 0.02,
        "source": "vision-accurate-range-v2",
    }


def _region(
    text: str,
    *,
    x: float = 0.40,
    y: float = 0.40,
    width: float = 0.05,
    height: float = 0.10,
    confidence: float = 0.6,
    source: str = "manga-layout-line-v1",
    segments: list[dict[str, object]] | None = None,
    orientation: str = "vertical",
) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": text,
        "orientation": orientation,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": confidence,
        "source": source,
        "selected_hypothesis_id": "manga-ocr",
        "segments": list(segments or []),
    }


def test_v67_removes_overlapping_short_fragment_when_complete_layout_region_exists() -> None:
    fragment = _region(
        "ろ",
        x=0.405,
        y=0.405,
        width=0.035,
        height=0.055,
        confidence=0.30,
        source="manga-ocr/line-split-v2",
        segments=[_segment("ろ")],
        orientation="horizontal",
    )
    complete = _region(
        "止めろ",
        x=0.400,
        y=0.400,
        width=0.050,
        height=0.095,
        confidence=0.69,
        segments=[_segment(char) for char in "止めろ"],
    )

    assert worker._suppress_contained_text_overlap_duplicates([fragment, complete]) == [complete]


def test_v67_prefers_geometry_agreement_over_longer_hallucinated_surface() -> None:
    hallucinated = _region(
        "魔獣ねーっ",
        x=0.400,
        y=0.400,
        width=0.080,
        height=0.050,
        confidence=0.30,
        source="",
        segments=[_segment(char) for char in "ね磨き獣号"],
        orientation="horizontal",
    )
    observed = _region(
        "ねーっ",
        x=0.420,
        y=0.405,
        width=0.050,
        height=0.060,
        confidence=0.61,
        segments=[_segment(char) for char in "ねーっ"],
    )

    assert worker._suppress_contained_text_overlap_duplicates([hallucinated, observed]) == [observed]


def test_v67_prefers_more_complete_text_when_evidence_is_otherwise_equal() -> None:
    shorter = _region(
        "いくぞ！",
        x=0.400,
        y=0.400,
        width=0.050,
        height=0.100,
        confidence=0.6521,
        segments=[_segment(char) for char in "いくぞ！"],
    )
    longer = _region(
        "いくぞ！！",
        x=0.401,
        y=0.402,
        width=0.052,
        height=0.103,
        confidence=0.6472,
        segments=[_segment(char) for char in "いくぞ！！"],
    )

    assert worker._suppress_contained_text_overlap_duplicates([shorter, longer]) == [longer]


def test_v67_keeps_contained_text_when_regions_do_not_overlap() -> None:
    short = _region("お", x=0.10, y=0.10, segments=[_segment("お")])
    long = _region("おい", x=0.70, y=0.70, segments=[_segment("お"), _segment("い")])

    assert worker._suppress_contained_text_overlap_duplicates([short, long]) == [short, long]


def test_v67_keeps_overlapping_unrelated_text() -> None:
    left = _region("海賊", x=0.40, y=0.40, segments=[_segment("海"), _segment("賊")])
    right = _region("ルフィ", x=0.405, y=0.405, segments=[_segment("ル"), _segment("フ"), _segment("ィ")])

    assert worker._suppress_contained_text_overlap_duplicates([left, right]) == [left, right]


def test_v67_pipeline_generation_markers() -> None:
    from pathlib import Path

    manga_source = Path(worker.__file__).with_name("manga.py").read_text(encoding="utf-8")
    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
