import pudge.manga_ocr_worker as worker


def _seg(text: str, x: float, width: float, *, y: float = 0.35, height: float = 0.03) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "vision-accurate-range-v2",
    }


def _candidate(
    text: str,
    raw: str,
    segments: list[dict[str, object]],
    *,
    confidence: float,
    width: float,
    height: float,
    geometry_source: str = "",
) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": raw,
        "orientation": "horizontal",
        "x": 0.53,
        "y": 0.35,
        "width": width,
        "height": height,
        "confidence": confidence,
        "detector": "vision-inverted",
        "source": "",
        "geometry_source": geometry_source,
        "selected_hypothesis_id": "manga-ocr",
        "segments": segments,
    }


def test_v65_rejects_low_confidence_one_glyph_mangaocr_disagreement() -> None:
    roof_art = _candidate(
        "ッ",
        "火）",
        [_seg("火", 0.536, 0.034), _seg("）", 0.570, 0.019)],
        confidence=0.3,
        width=0.065,
        height=0.044,
    )
    panel_art = _candidate(
        "は",
        "はな",
        [_seg("は", 0.592, 0.063), _seg("な", 0.649, 0.030)],
        confidence=0.3,
        width=0.088,
        height=0.043,
        geometry_source="horizontal-narrow-expand-v1",
    )

    assert worker._suppress_detector_disagreement_art_noise([roof_art, panel_art]) == []


def test_v65_keeps_single_glyph_when_detector_agrees() -> None:
    real = _candidate(
        "あ",
        "あ",
        [_seg("あ", 0.53, 0.03)],
        confidence=0.3,
        width=0.04,
        height=0.035,
    )

    assert worker._suppress_detector_disagreement_art_noise([real]) == [real]


def test_v65_rejects_repeated_laughter_art_disagreement() -> None:
    art = _candidate(
        "なははは",
        "ははは",
        [
            _seg("は", 0.210, 0.031, y=0.754, height=0.017),
            _seg("は", 0.241, 0.023, y=0.754, height=0.017),
            _seg("は", 0.264, 0.027, y=0.754, height=0.017),
            _seg("は", 0.290, 0.021, y=0.754, height=0.017),
        ],
        confidence=0.5,
        width=0.113,
        height=0.030,
    )

    assert worker._suppress_detector_disagreement_art_noise([art]) == []


def test_v65_keeps_repeated_laughter_when_surfaces_agree() -> None:
    real = _candidate(
        "ははは",
        "ははは",
        [
            _seg("は", 0.20, 0.025),
            _seg("は", 0.23, 0.025),
            _seg("は", 0.26, 0.025),
        ],
        confidence=0.5,
        width=0.10,
        height=0.035,
    )

    assert worker._suppress_detector_disagreement_art_noise([real]) == [real]


def test_v65_pipeline_generation_markers() -> None:
    import pudge.manga as manga

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert manga._REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert "-regions-v96p27.json" in open(manga.__file__, encoding="utf-8").read()
