import pudge.manga_ocr_worker as worker


def _segment(text: str, *, source: str = "layout-line-proportional-v1") -> dict[str, object]:
    return {
        "text": text,
        "x": 0.40,
        "y": 0.40,
        "width": 0.02,
        "height": 0.02,
        "source": source,
    }


def _region(
    text: str,
    *,
    x: float = 0.40,
    y: float = 0.40,
    width: float = 0.05,
    height: float = 0.10,
    confidence: float = 0.5,
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


def test_v66_rejects_exhausted_recovery_region_without_character_geometry() -> None:
    expanded = _region(
        "それは．．．何でしょう",
        source="expanded-vision-rectangle",
        confidence=0.25,
        segments=[],
    )
    dark_block = _region(
        "そういうことで、",
        source="dark-block-proposal",
        confidence=0.62,
        segments=[],
    )

    assert worker._suppress_unverified_recovery_regions([expanded, dark_block]) == []


def test_v66_keeps_geometryless_normal_region_outside_recovery_sources() -> None:
    # v66 must not become a blanket `segments empty => delete` rule.
    ordinary = _region("来い！！", source="", confidence=0.25, segments=[])

    assert worker._suppress_unverified_recovery_regions([ordinary]) == [ordinary]


def test_v66_keeps_expanded_vertical_seed_without_geometry() -> None:
    # Full-volume p69 is a real speech bubble that still exits the old recovery
    # path without character boxes.  Do not suppress this source until it has a
    # dedicated geometry retry.
    real_dialogue = _region(
        "渦巻！？",
        source="expanded-vertical-seed",
        confidence=0.50,
        segments=[],
    )

    assert worker._suppress_unverified_recovery_regions([real_dialogue]) == [real_dialogue]


def test_v66_keeps_recovery_region_after_geometry_was_recovered() -> None:
    recovered = _region(
        "何でしょう",
        source="expanded-vision-rectangle",
        confidence=0.25,
        segments=[_segment("何"), _segment("で"), _segment("し"), _segment("ょ"), _segment("う")],
    )

    assert worker._suppress_unverified_recovery_regions([recovered]) == [recovered]


def test_v66_exact_overlap_dedup_prefers_text_geometry_agreement() -> None:
    weak = _region(
        "何だ",
        x=0.400,
        y=0.400,
        width=0.050,
        height=0.060,
        confidence=0.70,
        source="",
        segments=[_segment("ア"), _segment("ー")],
    )
    strong = _region(
        "何だ",
        x=0.405,
        y=0.405,
        width=0.048,
        height=0.062,
        confidence=0.60,
        segments=[_segment("何"), _segment("だ")],
    )

    assert worker._suppress_exact_text_overlap_duplicates([weak, strong]) == [strong]


def test_v66_exact_overlap_dedup_prefers_confidence_when_evidence_is_equal() -> None:
    high = _region(
        "悪かった",
        x=0.40,
        y=0.40,
        confidence=0.82,
        segments=[_segment(char) for char in "悪かった"],
    )
    low = _region(
        "悪かった",
        x=0.405,
        y=0.405,
        confidence=0.61,
        segments=[_segment(char) for char in "悪かった"],
    )

    assert worker._suppress_exact_text_overlap_duplicates([low, high]) == [high]


def test_v66_exact_overlap_dedup_keeps_same_text_when_regions_are_disjoint() -> None:
    left = _region("はい", x=0.10, y=0.10, segments=[_segment("は"), _segment("い")])
    right = _region("はい", x=0.70, y=0.70, segments=[_segment("は"), _segment("い")])

    assert worker._suppress_exact_text_overlap_duplicates([left, right]) == [left, right]


def test_v66_exact_overlap_dedup_keeps_overlapping_different_text() -> None:
    short = _region("仲間じゃ", x=0.40, y=0.40, segments=[_segment(char) for char in "仲間じゃ"])
    long = _region("仲間じゃない", x=0.405, y=0.405, segments=[_segment(char) for char in "仲間じゃない"])

    assert worker._suppress_exact_text_overlap_duplicates([short, long]) == [short, long]


def test_v66_pipeline_generation_markers() -> None:
    import pudge.manga as manga

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert manga._REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert "-regions-v96p27.json" in open(manga.__file__, encoding="utf-8").read()
