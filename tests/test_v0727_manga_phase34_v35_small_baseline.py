from __future__ import annotations

from pudge.manga_ocr_worker import _split_horizontal_multiline_region


def _glyphs(text: str, *, y: float, height: float, x: float, step: float) -> list[dict[str, object]]:
    return [
        {
            "text": char,
            "orientation": "horizontal",
            "x": x + index * step,
            "y": y,
            "width": step * 0.82,
            "height": height,
            "source": "vision-accurate-range-v2",
        }
        for index, char in enumerate(text)
    ]


def test_smaller_standalone_subtitle_survives_ruby_filter() -> None:
    segments = []
    segments += _glyphs("だい", y=0.205, height=0.0133, x=0.37, step=0.025)
    segments += _glyphs("第1話", y=0.1667, height=0.0417, x=0.37, step=0.055)
    segments += _glyphs("ROMANGEDAVIN", y=0.080, height=0.074, x=0.045, step=0.072)
    segments += _glyphs("冒険の夜明け", y=0.032, height=0.0381, x=0.328, step=0.053)

    region = {
        "text": "だい 第1話 ROMANGE DAWN 冒険の夜明け",
        "raw_text": "だい 第1話 ROMANGE DAWN 冒険の夜明け",
        "orientation": "horizontal",
        "x": 0.039,
        "y": 0.025,
        "width": 0.935,
        "height": 0.199,
        "segments": segments,
    }
    pieces = _split_horizontal_multiline_region(region)
    assert [piece["text"] for piece in pieces] == [
        "第1話",
        "ROMANGEDAVIN",
        "冒険の夜明け",
    ]


def test_interleaved_tiny_furigana_is_still_removed() -> None:
    segments = []
    segments += _glyphs("第1話冒", y=0.60, height=0.04, x=0.10, step=0.05)
    segments += _glyphs("第2話男", y=0.52, height=0.04, x=0.10, step=0.05)
    segments += _glyphs("ぼうけん", y=0.565, height=0.012, x=0.35, step=0.025)
    region = {
        "orientation": "horizontal",
        "source": "manga-ocr",
        "x": 0.08,
        "y": 0.50,
        "width": 0.80,
        "height": 0.18,
        "segments": segments,
    }
    pieces = _split_horizontal_multiline_region(region)
    assert [piece["text"] for piece in pieces] == ["第1話冒", "第2話男"]


from pudge.manga import _repair_repeated_latin_page_titles


def _latin_region(text: str) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": text,
        "orientation": "horizontal",
        "segments": [
            {
                "text": char,
                "orientation": "horizontal",
                "x": 0.05 + index * 0.04,
                "y": 0.08,
                "width": 0.035,
                "height": 0.07,
                "source": "vision-accurate-range-v2",
            }
            for index, char in enumerate(text)
        ],
    }


def test_repeated_same_book_latin_consensus_repairs_noisy_outlier_geometry() -> None:
    current = _latin_region("ROMANGEDAVIN")
    peers = [
        [{"text": "ROMANCE DAWN-冒険の夜明け", "orientation": "horizontal"}],
        [{"text": "第1話ROMANCEDAWN冒険の夜明け", "orientation": "horizontal"}],
        [{"text": "ROMANCE DAWN", "orientation": "horizontal"}],
    ]
    repaired = _repair_repeated_latin_page_titles([current], peers)
    assert [item["text"] for item in repaired] == ["ROMANCEDAWN"]
    segments = repaired[0]["segments"]
    assert "".join(str(item["text"]) for item in segments) == "ROMANCEDAWN"
    assert len(segments) == 11
    assert segments[5]["text"] == "C"
    assert segments[9]["text"] == "W"
    assert float(segments[9]["width"]) > 0.035
    assert repaired[0]["book_latin_consensus"]["peer_pages"] == 3


def test_repeated_latin_consensus_requires_two_peer_pages() -> None:
    current = _latin_region("ROMANGEDAVIN")
    peers = [[{"text": "ROMANCE DAWN", "orientation": "horizontal"}]]
    repaired = _repair_repeated_latin_page_titles([current], peers)
    assert repaired[0]["text"] == "ROMANGEDAVIN"


def test_repeated_latin_consensus_does_not_rewrite_mixed_japanese_region() -> None:
    current = _latin_region("ROMANGEDAVIN")
    current["text"] = "第1話ROMANGEDAVIN"
    current["raw_text"] = current["text"]
    peers = [
        [{"text": "ROMANCE DAWN", "orientation": "horizontal"}],
        [{"text": "ROMANCEDAWN", "orientation": "horizontal"}],
    ]
    repaired = _repair_repeated_latin_page_titles([current], peers)
    assert repaired[0]["text"] == "第1話ROMANGEDAVIN"
