from __future__ import annotations

from PIL import Image

from pudge.manga_ocr_worker import (
    _LAYOUT_DETECTOR,
    _LAYOUT_LINE_SOURCE,
    _layout_recovery_enabled,
    _merge_layout_recovery_regions,
    _observed_geometry_blocks_layout,
    _recognize_regions,
    _supported_short_layout_text,
)


def _layout(text: str = "海賊") -> dict[str, object]:
    return {
        "text": text,
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.40,
        "y": 0.40,
        "width": 0.04,
        "height": 0.18,
        "source": _LAYOUT_LINE_SOURCE,
        "detector": _LAYOUT_DETECTOR,
        "geometry_source": _LAYOUT_DETECTOR,
        "geometry_status": "observed",
        "provenance": {
            "component_count": 6,
            "black_ratio": 0.12,
            "midtone_ratio": 0.08,
            "layout_score": 9.0,
        },
    }


def _exact_segment() -> dict[str, object]:
    return {
        "text": "海",
        "source": "vision-accurate-range-v2",
        "x": 0.4,
        "y": 0.4,
        "width": 0.02,
        "height": 0.03,
    }


def test_wide_exact_vision_geometry_does_not_block_tight_manga_lane() -> None:
    proposal = _layout()
    wide = {
        "orientation": "vertical",
        "x": 0.32,
        "y": 0.36,
        "width": 0.18,
        "height": 0.26,
        "segments": [_exact_segment()],
    }
    assert _observed_geometry_blocks_layout(wide, proposal) is False


def test_horizontal_exact_vision_geometry_does_not_block_vertical_lane() -> None:
    proposal = _layout()
    horizontal = {
        "orientation": "horizontal",
        "x": 0.36,
        "y": 0.43,
        "width": 0.12,
        "height": 0.05,
        "segments": [_exact_segment()],
    }
    assert _observed_geometry_blocks_layout(horizontal, proposal) is False


def test_same_tight_exact_vertical_lane_still_blocks_duplicate() -> None:
    proposal = _layout()
    exact = {
        "orientation": "vertical",
        "x": 0.399,
        "y": 0.402,
        "width": 0.041,
        "height": 0.176,
        "segments": [_exact_segment()],
    }
    assert _observed_geometry_blocks_layout(exact, proposal) is True


def test_merge_keeps_layout_alongside_wide_exact_region() -> None:
    proposal = _layout("おれ達の")
    wide = {
        "text": "おれ達の大いなる旅に",
        "orientation": "vertical",
        "x": 0.32,
        "y": 0.36,
        "width": 0.18,
        "height": 0.26,
        "segments": [_exact_segment()],
    }
    merged = _merge_layout_recovery_regions([wide, proposal])
    assert proposal["text"] in [row.get("text") for row in merged]
    assert wide["text"] in [row.get("text") for row in merged]


def test_primary_strong_one_kana_layout_is_allowed() -> None:
    item = _layout("な．．．")
    assert _supported_short_layout_text(item, "な．．．") is True


def test_three_component_single_kanji_noise_is_still_rejected() -> None:
    item = _layout("白")
    item["provenance"] = {
        "component_count": 3,
        "black_ratio": 0.14,
        "midtone_ratio": 0.07,
    }
    assert _supported_short_layout_text(item, "白") is False


class _RetryModel:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _image: Image.Image) -> str:
        self.calls += 1
        return "～～～っ！！" if self.calls == 1 else "とって～～～っ！！！"


def test_layout_context_retry_runs_only_after_unusable_first_hypothesis(monkeypatch) -> None:
    image = Image.new("RGB", (300, 400), "white")
    region = _layout("")
    monkeypatch.setattr("pudge.manga_ocr_worker._prepare_regions_for_ocr", lambda rows, image: rows)
    monkeypatch.setattr("pudge.manga_ocr_worker._manga_layout_line_proposals", lambda image, rows: [])
    model = _RetryModel()
    rows = _recognize_regions(model, image, [region])
    assert model.calls == 2
    assert rows[0]["text"] == "とって～～～っ！！！"
    assert rows[0]["selected_hypothesis_id"] == "manga-ocr-square-retry"


class _GoodFirstModel:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _image: Image.Image) -> str:
        self.calls += 1
        return "証拠を見せて"


def test_layout_context_retry_never_expands_already_usable_text(monkeypatch) -> None:
    image = Image.new("RGB", (300, 400), "white")
    region = _layout("")
    monkeypatch.setattr("pudge.manga_ocr_worker._prepare_regions_for_ocr", lambda rows, image: rows)
    monkeypatch.setattr("pudge.manga_ocr_worker._manga_layout_line_proposals", lambda image, rows: [])
    model = _GoodFirstModel()
    rows = _recognize_regions(model, image, [region])
    assert model.calls == 1
    assert rows[0]["text"] == "証拠を見せて"


def test_layout_search_sentinel_is_never_sent_to_mangaocr(monkeypatch) -> None:
    image = Image.new("RGB", (300, 400), "white")
    sentinel = {
        "text": "",
        "raw_text": "",
        "orientation": "mixed",
        "detector": "layout-search-sentinel",
        "source": "layout-search-sentinel",
        "layout_search_only": True,
        "x": 0.0,
        "y": 0.0,
        "width": 0.0,
        "height": 0.0,
    }
    monkeypatch.setattr("pudge.manga_ocr_worker._prepare_regions_for_ocr", lambda rows, image: rows)
    monkeypatch.setattr("pudge.manga_ocr_worker._manga_layout_line_proposals", lambda image, rows: [])

    class Never:
        def __call__(self, _image: Image.Image) -> str:
            raise AssertionError("sentinel must not be OCRed")

    assert _recognize_regions(Never(), image, [sentinel]) == []


def test_successful_detector_miss_is_persisted_as_verified_empty() -> None:
    from pathlib import Path

    source = Path("pudge/manga.py").read_text(encoding="utf-8")
    assert "if not detected:" in source
    assert (
        'status, reason, retryable = "empty_verified", '
        '"successful_detector_no_text", False'
    ) in source
    assert '"reason": "detector_miss_or_recognizer_unavailable"' in source


def _horizontal_heavy_regions() -> list[dict[str, object]]:
    return [{"orientation": "horizontal"} for _ in range(14)] + [{"orientation": "vertical"}]


def _primary_lane(x: float, *, height: float = 0.10, width: float = 0.035) -> dict[str, object]:
    return {
        "x": x,
        "y": 0.30,
        "width": width,
        "height": height,
        "orientation": "vertical",
    }


def test_horizontal_heavy_compact_toc_stays_protected() -> None:
    primary = [_primary_lane(0.20 + index * 0.055) for index in range(7)]
    assert _layout_recovery_enabled(_horizontal_heavy_regions(), primary) is False


def test_horizontal_heavy_manga_page_can_recover_when_vertical_lanes_span_page() -> None:
    primary = [
        _primary_lane(x)
        for x in (0.05, 0.18, 0.31, 0.48, 0.65, 0.82, 0.91)
    ]
    assert _layout_recovery_enabled(_horizontal_heavy_regions(), primary) is True


def _wide_horizontal_row(y: float) -> dict[str, object]:
    return {
        "orientation": "horizontal",
        "x": 0.08,
        "y": y,
        "width": 0.52,
        "height": 0.05,
    }


def test_wide_horizontal_toc_rows_block_even_spanning_primary_lanes() -> None:
    regions = [_wide_horizontal_row(0.20 + index * 0.07) for index in range(5)]
    primary = [
        _primary_lane(x)
        for x in (0.05, 0.18, 0.31, 0.48, 0.65, 0.82, 0.91)
    ]
    assert _layout_recovery_enabled(regions, primary) is False


def test_single_wide_horizontal_noise_row_does_not_block_p009_override() -> None:
    regions = _horizontal_heavy_regions()
    regions[0] = _wide_horizontal_row(0.50)
    primary = [
        _primary_lane(x)
        for x in (0.05, 0.18, 0.31, 0.48, 0.65, 0.82, 0.91)
    ]
    assert _layout_recovery_enabled(regions, primary) is True


def test_sparse_horizontal_large_block_with_compact_lanes_is_toc_protected() -> None:
    regions = [
        {
            "orientation": "horizontal",
            "x": 0.10,
            "y": 0.20,
            "width": 0.50,
            "height": 0.08,
        },
        {
            "orientation": "horizontal",
            "x": 0.25,
            "y": 0.68,
            "width": 0.44,
            "height": 0.045,
        },
        {
            "orientation": "horizontal",
            "x": 0.07,
            "y": 0.30,
            "width": 0.86,
            "height": 0.44,
        },
    ]
    primary = [_primary_lane(x) for x in (0.10, 0.18, 0.27, 0.36, 0.45, 0.53)]
    assert _layout_recovery_enabled(regions, primary) is False


def test_sparse_horizontal_page_without_large_block_is_not_toc_blocked() -> None:
    regions = [
        {
            "orientation": "horizontal",
            "x": 0.10,
            "y": 0.20,
            "width": 0.30,
            "height": 0.07,
        },
        {
            "orientation": "horizontal",
            "x": 0.55,
            "y": 0.62,
            "width": 0.26,
            "height": 0.06,
        },
    ]
    primary = [_primary_lane(x) for x in (0.05, 0.18, 0.31, 0.48, 0.65, 0.82)]
    assert _layout_recovery_enabled(regions, primary) is True
