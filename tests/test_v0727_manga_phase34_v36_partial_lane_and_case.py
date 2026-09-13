from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _primary() -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.206,
        "y": 0.376,
        "width": 0.027,
        "height": 0.101,
        "source": "manga-layout-line-v1",
        "detector": "manga-ink-components-v1",
        "provenance": {
            "component_count": 3,
            "component_coverage": 0.94,
            "black_ratio": 0.14,
            "midtone_ratio": 0.12,
        },
    }


def _raw() -> dict[str, object]:
    return {
        "orientation": "vertical",
        "x": 0.208,
        "y": 0.377,
        "width": 0.024,
        "height": 0.099,
        "source": "manga-layout-line-v1",
        "detector": "manga-raw-components-v1",
        "provenance": {
            "component_count": 9,
            "component_coverage": 1.03,
        },
    }


def _weak_fragment(*, source: str = "", height: float = 0.024) -> dict[str, object]:
    return {
        "text": "れは",
        "orientation": "vertical",
        "x": 0.204,
        "y": 0.372,
        "width": 0.031,
        "height": height,
        "source": source,
        "segments": [
            {
                "text": "",
                "orientation": "horizontal",
                "x": 0.21,
                "y": 0.378,
                "width": 0.02,
                "height": 0.012,
                "source": "",
            }
        ],
    }


def test_raw_component_support_recovers_merged_full_lane(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: [_raw()])
    try:
        plain = _primary()
        assert worker._layout_text_geometry_plausible(plain, "ちゃんとおれは") is False
        [supported] = worker._attach_partial_weak_raw_component_support(
            image, [_weak_fragment()], [plain]
        )
        assert supported["provenance"]["raw_component_count_support"] == 9
        assert supported["provenance"]["raw_component_support_kind"] == "partial-weak-same-lane-v1"
        assert worker._layout_text_geometry_plausible(supported, "ちゃんとおれは") is True
    finally:
        image.close()


def test_raw_component_support_requires_source_less_short_fragment(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: [_raw()])
    try:
        [expanded] = worker._attach_partial_weak_raw_component_support(
            image, [_weak_fragment(source="expanded-vertical-seed")], [_primary()]
        )
        assert "raw_component_count_support" not in expanded["provenance"]

        [too_tall] = worker._attach_partial_weak_raw_component_support(
            image, [_weak_fragment(height=0.070)], [_primary()]
        )
        assert "raw_component_count_support" not in too_tall["provenance"]
    finally:
        image.close()


def _latin_piece(text: str) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": text,
        "orientation": "horizontal",
        "source": "vision-accurate/line-split-v2",
        "full_region_ocr_relabel": True,
        "x": 0.20,
        "y": 0.70,
        "width": 0.30,
        "height": 0.05,
        "segments": [
            {
                "text": ch,
                "orientation": "horizontal",
                "x": 0.20 + index * 0.05,
                "y": 0.70,
                "width": 0.04,
                "height": 0.05,
                "source": "vision-accurate-range-v2",
            }
            for index, ch in enumerate(text)
        ],
    }


class _Model:
    def __init__(self, text: str) -> None:
        self.text = text

    def __call__(self, _image: Image.Image) -> str:
        return self.text


def test_short_mixed_case_latin_label_uses_local_case_consensus() -> None:
    image = Image.new("RGB", (800, 600), "white")
    try:
        repaired = worker._refresh_horizontal_line_with_mangaocr(
            _Model("ｖｏｌ．１"), image, _latin_piece("VOl.1")
        )
    finally:
        image.close()
    assert repaired["text"] == "vol.1"
    assert "".join(str(segment["text"]) for segment in repaired["segments"]) == "vol.1"
    assert repaired["latin_case_consensus"] == {
        "from": "VOl.1",
        "to": "vol.1",
        "source": "local-mangaocr-case-v1",
    }


def test_short_latin_case_consensus_does_not_rewrite_uniform_or_different_text() -> None:
    image = Image.new("RGB", (800, 600), "white")
    try:
        uniform = worker._refresh_horizontal_line_with_mangaocr(
            _Model("ｏｎｅｐｉｅｃｅ"), image, _latin_piece("ONEPIECE")
        )
        different = worker._refresh_horizontal_line_with_mangaocr(
            _Model("ｖｏｌ．２"), image, _latin_piece("VOl.1")
        )
    finally:
        image.close()
    assert uniform["text"] == "ONEPIECE"
    assert different["text"] == "VOl.1"
