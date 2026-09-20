import pudge.manga_ocr_worker as worker


def _hseg(text: str, x: float, y: float, width: float, height: float) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "vision-accurate-range-v2",
    }


def _vseg(text: str, x: float, y: float, width: float, height: float) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "layout-line-ink-v2+tight-v1",
    }


def _vertical_peer(text: str, *, x: float, y: float, width: float, height: float,
                   segments: list[dict[str, object]]) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "manga-layout-line-v1",
        "segments": segments,
    }


def test_v64_suppresses_multi_segment_furigana_echo_next_to_kanji() -> None:
    candidate = {
        "text": "こんじゃ",
        "raw_text": "・え",
        "orientation": "horizontal",
        "x": 0.231579,
        "y": 0.916667,
        "width": 0.024421,
        "height": 0.015,
        "confidence": 0.3,
        "detector": "vision-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            _hseg("・", 0.231579, 0.916667, 0.017434, 0.015),
            _hseg("え", 0.243750, 0.916667, 0.012250, 0.015),
        ],
    }
    peer = _vertical_peer(
        "根性と",
        x=0.201132,
        y=0.874833,
        width=0.046421,
        height=0.062,
        segments=[
            _vseg("根", 0.202632, 0.915833, 0.043421, 0.019167),
            _vseg("性", 0.202632, 0.895833, 0.044737, 0.020000),
            _vseg("と", 0.210526, 0.876667, 0.019737, 0.018333),
        ],
    )

    assert worker._suppress_tiny_horizontal_ruby_echo_regions([candidate, peer]) == [peer]


def test_v64_ruby_cleanup_keeps_observed_repeated_kana_geometry() -> None:
    candidate = {
        "text": "いい",
        "raw_text": "い",
        "orientation": "horizontal",
        "x": 0.30,
        "y": 0.80,
        "width": 0.030,
        "height": 0.022,
        "confidence": 0.5,
        "detector": "vision-original",
        "source": "",
        "geometry_source": "repeated-kana-ink-split-v1",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            _hseg("い", 0.302, 0.803, 0.008, 0.015),
            _hseg("い", 0.311, 0.803, 0.008, 0.015),
        ],
    }
    peer = _vertical_peer(
        "言い方はァ",
        x=0.27,
        y=0.75,
        width=0.05,
        height=0.12,
        segments=[_vseg("言", 0.280, 0.800, 0.045, 0.020)],
    )

    assert worker._suppress_tiny_horizontal_ruby_echo_regions([candidate, peer]) == [candidate, peer]


def _horizontal_duplicate(text: str, *, x: float, y: float, width: float, height: float) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": text[:1],
        "orientation": "horizontal",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": 0.5,
        "detector": "vision-inverted",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_hseg(text[:1], x, y, width * 0.7, height)],
    }


def test_v64_suppresses_horizontal_prefix_duplicate_of_vertical_lane() -> None:
    candidate = _horizontal_duplicate("なん", x=0.739, y=0.902, width=0.041, height=0.037)
    peer = _vertical_peer(
        "なんだ",
        x=0.742,
        y=0.876,
        width=0.037,
        height=0.071,
        segments=[_vseg("な", 0.742, 0.918, 0.030, 0.018)],
    )

    assert worker._suppress_horizontal_prefix_duplicates([candidate, peer]) == [peer]


def test_v64_suppresses_partial_overlap_prefix_duplicate() -> None:
    candidate = _horizontal_duplicate("えー", x=0.220316, y=0.907333, width=0.046211, height=0.035333)
    peer = _vertical_peer(
        "えーー",
        x=0.224816,
        y=0.848167,
        width=0.037211,
        height=0.090333,
        segments=[
            _vseg("え", 0.227632, 0.915000, 0.032895, 0.021667),
            _vseg("ー", 0.236842, 0.879167, 0.013158, 0.028333),
            _vseg("ー", 0.236842, 0.850000, 0.013158, 0.029167),
        ],
    )

    assert worker._suppress_horizontal_prefix_duplicates([candidate, peer]) == [peer]


def test_v64_keeps_overlap_when_horizontal_text_is_not_peer_prefix() -> None:
    candidate = _horizontal_duplicate("なー", x=0.857, y=0.909, width=0.044, height=0.032)
    peer = _vertical_peer(
        "ないか",
        x=0.861,
        y=0.875,
        width=0.040,
        height=0.080,
        segments=[_vseg("な", 0.861, 0.920, 0.030, 0.020)],
    )

    assert worker._suppress_horizontal_prefix_duplicates([candidate, peer]) == [candidate, peer]


def test_v64_pipeline_generation_markers() -> None:
    import pudge.manga as manga

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert manga._REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert "-regions-v96p27.json" in open(manga.__file__, encoding="utf-8").read()
