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


def _ruby_candidate(text: str, raw: str, *, x: float, y: float, width: float = 0.028, height: float = 0.022,
                    seg_width: float = 0.015, seg_height: float = 0.010) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": raw,
        "orientation": "horizontal",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": 0.5,
        "detector": "vision-contrast",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_hseg(raw, x + 0.006, y + 0.006, seg_width, seg_height)],
    }


def _vertical_peer(text: str, *, x: float, y: float, segments: list[dict[str, object]], width: float = 0.05,
                   height: float = 0.18) -> dict[str, object]:
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


def test_v63_ruby_cleanup_helper_exists() -> None:
    assert hasattr(worker, "_suppress_tiny_horizontal_ruby_echo_regions")


def test_v63_suppresses_tiny_kana_ruby_aligned_to_kanji() -> None:
    candidate = _ruby_candidate("あし", "あ", x=0.539, y=0.916)
    peer = _vertical_peer(
        "足をどけろ！！",
        x=0.496,
        y=0.761,
        segments=[
            _vseg("足", 0.500, 0.914, 0.045, 0.027),
            _vseg("を", 0.500, 0.886, 0.030, 0.024),
        ],
    )

    cleaned = worker._suppress_tiny_horizontal_ruby_echo_regions([candidate, peer])

    assert cleaned == [peer]


def test_v63_suppresses_ruby_spanning_two_kanji_segments() -> None:
    candidate = _ruby_candidate("いてここ", "て", x=0.126, y=0.319, seg_width=0.013, seg_height=0.010)
    peer = _vertical_peer(
        "でも．．．相手は",
        x=0.105,
        y=0.307,
        width=0.027,
        height=0.090,
        segments=[
            _vseg("相", 0.108, 0.331, 0.021, 0.0083),
            _vseg("手", 0.107, 0.323, 0.025, 0.0083),
        ],
    )

    cleaned = worker._suppress_tiny_horizontal_ruby_echo_regions([candidate, peer])

    assert cleaned == [peer]


def test_v63_keeps_real_small_base_text_when_not_smaller_than_peer_glyph() -> None:
    candidate = _ruby_candidate("に．．", "に", x=0.065, y=0.294, width=0.038, height=0.027,
                                seg_width=0.026, seg_height=0.015)
    peer = _vertical_peer(
        "デザート",
        x=0.104,
        y=0.238,
        width=0.037,
        height=0.082,
        segments=[_vseg("デ", 0.104, 0.294, 0.0316, 0.0167)],
    )

    cleaned = worker._suppress_tiny_horizontal_ruby_echo_regions([candidate, peer])

    assert cleaned == [candidate, peer]


def test_v63_symbol_art_guard_exists() -> None:
    assert hasattr(worker, "_suppress_short_symbol_only_art_noise")


def test_v63_suppresses_anchor_like_symbol_hallucinated_as_japanese() -> None:
    candidate = {
        "text": "もし",
        "raw_text": "↓",
        "orientation": "horizontal",
        "x": 0.576,
        "y": 0.614,
        "width": 0.033,
        "height": 0.025,
        "confidence": 0.5,
        "detector": "vision-contrast",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_hseg("↓", 0.582, 0.620, 0.021, 0.013)],
    }

    assert worker._suppress_short_symbol_only_art_noise([candidate]) == []


def test_v63_symbol_guard_keeps_real_japanese_detector_surface() -> None:
    candidate = {
        "text": "もし",
        "raw_text": "も",
        "orientation": "horizontal",
        "x": 0.576,
        "y": 0.614,
        "width": 0.033,
        "height": 0.025,
        "confidence": 0.5,
        "detector": "vision-contrast",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_hseg("も", 0.582, 0.620, 0.021, 0.013)],
    }

    assert worker._suppress_short_symbol_only_art_noise([candidate]) == [candidate]


def test_v63_pipeline_generation_markers() -> None:
    import pudge.manga as manga
    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert manga._REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert "-regions-v96p27.json" in open(manga.__file__, encoding="utf-8").read()
