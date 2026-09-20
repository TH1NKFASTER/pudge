import pudge.manga_ocr_worker as worker


def _segment(text: str, *, x: float = 0.40, y: float = 0.40) -> dict[str, object]:
    return {
        "text": text,
        "x": x,
        "y": y,
        "width": 0.012,
        "height": 0.018,
        "source": "vision-accurate-range-v2",
    }


def _region(
    text: str,
    *,
    raw_text: str,
    segment_texts: list[str],
    confidence: float = 0.30,
    width: float = 0.035,
    height: float = 0.025,
    orientation: str = "horizontal",
) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": raw_text,
        "orientation": orientation,
        "x": 0.40,
        "y": 0.40,
        "width": width,
        "height": height,
        "confidence": confidence,
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_segment(value, x=0.40 + index * 0.013) for index, value in enumerate(segment_texts)],
    }


def test_v68_suppresses_small_geometry_mangaocr_overclaim() -> None:
    region = _region(
        "あーだいじょ",
        raw_text="あ",
        segment_texts=["あ"],
        confidence=0.50,
        width=0.033053,
        height=0.027,
    )

    assert worker._suppress_small_geometry_mangaocr_overclaims([region]) == []


def test_v68_suppresses_vertical_overclaim_when_detector_and_segment_agree_on_short_surface() -> None:
    region = _region(
        "だけど彼はそうい",
        raw_text="・れ",
        segment_texts=["れ"],
        confidence=0.30,
        width=0.122526,
        height=0.043667,
        orientation="vertical",
    )

    assert worker._suppress_small_geometry_mangaocr_overclaims([region]) == []


def test_v68_keeps_short_real_sfx_with_only_partial_observed_geometry() -> None:
    region = _region(
        "ビクッ",
        raw_text="ビ",
        segment_texts=["ビ"],
        confidence=0.30,
        width=0.033053,
        height=0.027,
    )

    assert worker._suppress_small_geometry_mangaocr_overclaims([region]) == [region]


def test_v68_keeps_high_confidence_region_even_when_geometry_is_partial() -> None:
    region = _region(
        "だいじょうぶ",
        raw_text="だ",
        segment_texts=["だ"],
        confidence=0.92,
        width=0.033053,
        height=0.027,
    )

    assert worker._suppress_small_geometry_mangaocr_overclaims([region]) == [region]


def test_v68_suppresses_one_kana_when_both_observed_surfaces_are_symbol_only() -> None:
    region = _region(
        "ッ",
        raw_text="》）",
        segment_texts=["）", "）"],
        confidence=0.30,
        width=0.046211,
        height=0.032,
    )

    assert worker._suppress_detector_disagreement_art_noise([region]) == []


def test_v68_keeps_small_kana_normalization_when_observed_surface_is_japanese() -> None:
    region = _region(
        "っ",
        raw_text="つ",
        segment_texts=["つ"],
        confidence=0.30,
        width=0.035,
        height=0.027,
    )

    assert worker._suppress_detector_disagreement_art_noise([region]) == [region]


def test_v68_pipeline_generation_markers() -> None:
    from pathlib import Path

    manga_source = Path(worker.__file__).with_name("manga.py").read_text(encoding="utf-8")
    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
