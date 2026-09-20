"""v96p25: short physical speech lanes and bounded trailing small-kana reread."""
from __future__ import annotations


from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _page() -> Image.Image:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((75, 909, 98, 927), fill="black")
    draw.rectangle((75, 937, 98, 955), fill="black")
    return image


def _short_candidate(*, detector: str = "manga-raw-components-v1") -> dict[str, object]:
    return {
        "source": worker._LAYOUT_LINE_SOURCE,
        "orientation": "vertical",
        "detector": detector,
        "x": 75 / 760,
        "y": 1 - 956 / 1200,
        "width": 24 / 760,
        "height": 47 / 1200,
        "provenance": {"component_count": 2, "component_coverage": 0.87,
                       "detector_bbox_px": [75, 909, 99, 956]},
    }


def test_v96p25_short_bubble_two_views_can_restore_missing_lane(monkeypatch):
    candidate = _short_candidate()
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda image: [candidate])
    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda image: [])
    image = _page()
    try:
        result = worker._recover_isolated_short_bubble_lanes(lambda crop: "ふん", image, [])
        assert len(result) == 1
        assert result[0]["text"] == "ふん"
        assert len(result[0]["segments"]) == 2
        assert result[0]["recognizer_retry"] == "isolated-short-bubble-consensus-v1"
    finally:
        image.close()


def test_v96p25_existing_region_blocks_short_bubble_duplicate(monkeypatch):
    candidate = _short_candidate()
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda image: [candidate])
    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda image: [])
    image = _page()
    try:
        output = worker._recover_isolated_short_bubble_lanes(
            lambda crop: "ふん", image, [{**candidate, "text": "ふん"}]
        )
        assert len(output) == 1
    finally:
        image.close()


def test_v96p25_dark_surroundings_and_disagreeing_views_are_rejected(monkeypatch):
    candidate = _short_candidate()
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda image: [candidate])
    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda image: [])
    image = _page()
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 900, 68, 970), fill="black")
    try:
        assert worker._recover_isolated_short_bubble_lanes(lambda crop: "ふん", image, []) == []
    finally:
        image.close()

    image = _page()
    try:
        surfaces = iter(("ふん", "ふも", "ふら"))
        assert worker._recover_isolated_short_bubble_lanes(
            lambda crop: next(surfaces), image, []
        ) == []
    finally:
        image.close()


def test_v96p25_raw_trailing_misread_requires_two_matching_bounded_views(monkeypatch):
    row = {
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._RAW_LAYOUT_DETECTOR,
        "orientation": "vertical",
        "text": "なっわ",
        "confidence": 0.54,
        "x": 486 / 760,
        "y": 1 - 834 / 1200,
        "width": 17 / 760,
        "height": 34 / 1200,
        "provenance": {"component_count": 2, "detector_bbox_px": [486, 800, 503, 834]},
    }
    extended = {**row, "y": 1 - 859 / 1200, "height": 59 / 1200}
    monkeypatch.setattr(worker, "_vertical_trailing_ink_geometry", lambda image, item: extended)
    image = _page()
    try:
        good = worker._recover_short_raw_trailing_misread(lambda crop: "なった", image, row)
        assert good["text"] == "なった"
        assert good["height"] == extended["height"]
        assert good["provenance"]["short_raw_trailing_misread"]["extension_px"] == 25
        assert len(good["segments"]) == 3
        assert row["text"] == "なっわ"
        views = iter(("なった", "なっき"))
        assert worker._recover_short_raw_trailing_misread(
            lambda crop: next(views), image, row
        )["text"] == "なっわ"
        assert worker._recover_short_raw_trailing_misread(
            lambda crop: "つった", image, row
        )["text"] == "なっわ"
    finally:
        image.close()


def test_v96p25_false_physical_trailing_extension_is_rejected(monkeypatch):
    row = _short_candidate()
    row.update({"text": "なっわ", "confidence": 0.54})
    image = _page()
    try:
        monkeypatch.setattr(worker, "_vertical_trailing_ink_geometry", lambda image, item: None)
        assert worker._recover_short_raw_trailing_misread(lambda crop: "なった", image, row) is row
    finally:
        image.close()


def test_v96p25_fragment_below_same_column_is_not_new_short_bubble(monkeypatch):
    """Side-white terminal glyphs are part of the upstream vertical sentence."""
    candidate = _short_candidate()
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda image: [candidate])
    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda image: [])
    image = _page()
    draw = ImageDraw.Draw(image)
    # Synthetic previous glyph, centred on the same lane, just above the new
    # fragment, with white lateral margins (the p39 failure mode).
    draw.rectangle((80, 896, 92, 905), fill="black")
    try:
        assert worker._recover_isolated_short_bubble_lanes(
            lambda crop: "ふん", image, []
        ) == []
    finally:
        image.close()


def test_v96p25_distant_earlier_text_does_not_block_independent_short_bubble(monkeypatch):
    candidate = _short_candidate()
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda image: [candidate])
    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda image: [])
    image = _page()
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 870, 92, 880), fill="black")
    try:
        result = worker._recover_isolated_short_bubble_lanes(
            lambda crop: "ふん", image, []
        )
        assert len(result) == 1
        assert result[0]["text"] == "ふん"
    finally:
        image.close()
