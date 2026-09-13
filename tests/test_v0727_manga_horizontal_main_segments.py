from __future__ import annotations

from pudge.manga_ocr_worker import _clean_horizontal_line_segments, _split_horizontal_multiline_region


def test_toc_ruby_is_removed_but_main_text_and_page_number_survive() -> None:
    group = [
        {"text": "第5話", "x": 0.084, "y": 0.386, "width": 0.145, "height": 0.0333},
        {"text": "だい", "x": 0.087, "y": 0.365, "width": 0.037, "height": 0.0117},
        {"text": "“海賊王と大剣豪、一", "x": 0.226, "y": 0.382, "width": 0.401, "height": 0.0396},
        {"text": "ひとり", "x": 0.263, "y": 0.363, "width": 0.071, "height": 0.015},
        {"text": "125", "x": 0.816, "y": 0.387, "width": 0.087, "height": 0.03},
    ]
    cleaned = _clean_horizontal_line_segments(group)
    assert [item["text"] for item in cleaned] == ["第5話", "“海賊王と大剣豪、一", "125"]


def test_duplicate_enclosing_toc_observation_is_removed() -> None:
    group = [
        {"text": "第4話 海軍大佐手のモーガン、一105", "x": 0.0816, "y": 0.4367, "width": 0.8184, "height": 0.05},
        {"text": "第4話 海軍大佐斧手のモーガン、", "x": 0.0842, "y": 0.4367, "width": 0.6868, "height": 0.0383},
        {"text": "だい", "x": 0.0868, "y": 0.4183, "width": 0.0368, "height": 0.0133},
        {"text": "かいぞくおう", "x": 0.2605, "y": 0.42, "width": 0.1184, "height": 0.0117},
        {"text": "-105", "x": 0.8026, "y": 0.4417, "width": 0.0974, "height": 0.0283},
    ]
    cleaned = _clean_horizontal_line_segments(group)
    assert [item["text"] for item in cleaned] == ["第4話 海軍大佐斧手のモーガン、", "-105"]


def test_existing_fast_character_split_compatibility_is_preserved() -> None:
    segments = []
    for row_y, text in ((0.60, "第1話冒"), (0.52, "第2話男")):
        for index, character in enumerate(text):
            segments.append({
                "text": character,
                "orientation": "horizontal",
                "x": 0.10 + index * 0.05,
                "y": row_y,
                "width": 0.04,
                "height": 0.04,
                "source": "vision-fast-character-v1",
            })
    for index, character in enumerate("ぼうけん"):
        segments.append({
            "text": character,
            "orientation": "horizontal",
            "x": 0.35 + index * 0.025,
            "y": 0.565,
            "width": 0.02,
            "height": 0.012,
            "source": "vision-fast-character-v1",
        })
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
