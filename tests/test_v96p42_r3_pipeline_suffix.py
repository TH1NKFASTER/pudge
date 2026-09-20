"""Check the actual raw-gap proposal stage, not just an isolated padded bbox."""
from __future__ import annotations

from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _three_narrow_lanes(*, final_stroke: bool) -> tuple[Image.Image, list[dict], list[dict]]:
    image = Image.new("RGB", (320, 300), "white")
    draw = ImageDraw.Draw(image)
    components: list[dict] = []
    for x in (40, 70, 100):
        for y in (40, 55, 70, 85, 100, 115):
            draw.rectangle((x + 2, y + 1, x + 9, y + 8), fill="black")
            components.append({"x": float(x), "y": float(y), "width": 12.0,
                               "height": 9.0, "cx": float(x + 6)})
    # Below the raw bottom (124 px): one 12 px tall stroke and two shorter strokes.
    draw.rectangle((102, 127, 104, 138), fill="black")
    draw.rectangle((106, 128, 110, 129), fill="black")
    if final_stroke:
        draw.rectangle((106, 136, 110, 137), fill="black")
    existing = [
        {"text": "既存の文章", "orientation": "vertical", "source": worker._LAYOUT_LINE_SOURCE,
         "x": x / 320, "y": 1 - 171 / 300, "width": 14 / 320, "height": 131 / 300}
        for x in (190, 220, 250)
    ]
    return image, existing, components


def test_suffix_detected_through_normal_proposal_pipeline(monkeypatch):
    image, existing, components = _three_narrow_lanes(final_stroke=True)
    monkeypatch.setattr(worker, "_raw_layout_component_candidates", lambda _: components)
    proposals = worker._multi_column_gap_proposals_v96p41(image, existing)
    actual = [p for p in proposals if p["provenance"]["detector_bbox_px"][0] == 99.0]
    assert len(actual) == 1
    assert actual[0]["provenance"]["detector_bbox_px"] == [99.0, 39.0, 113.0, 140.0]
    assert actual[0]["text"] == ""  # Must still be read by independent OCR consensus.


def test_suffix_cannot_extend_without_final_stroke(monkeypatch):
    image, existing, components = _three_narrow_lanes(final_stroke=False)
    monkeypatch.setattr(worker, "_raw_layout_component_candidates", lambda _: components)
    proposals = worker._multi_column_gap_proposals_v96p41(image, existing)
    actual = [p for p in proposals if p["provenance"]["detector_bbox_px"][0] == 99.0]
    assert len(actual) == 1
    assert actual[0]["provenance"]["detector_bbox_px"] == [99.0, 39.0, 113.0, 125.0]
