from __future__ import annotations

from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


def _extended() -> dict[str, object]:
    return {
        "text": "置いてきた",
        "raw_text": "置いてきた",
        "orientation": "vertical",
        "x": 0.40,
        "y": 0.20,
        "width": 0.15,
        "height": 0.35,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {
            "leading_ink_geometry": {
                "old_top_px": 100,
                "new_top_px": 40,
                "extension_px": 60,
                "max_gap_px": 17,
                "scan_x_px": [80, 120],
            }
        },
    }


def test_edge_dominated_extension_rejects_cross_column_prefix() -> None:
    image = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(image)
    # Ink only at the outside edges emulates a neighbouring vertical column.
    draw.rectangle((80, 48, 86, 92), fill="black")
    draw.rectangle((114, 48, 120, 92), fill="black")
    assert worker._leading_extension_has_centered_ink(image, _extended()) is False


def test_center_supported_extension_accepts_real_missing_prefix() -> None:
    image = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((92, 45, 108, 62), fill="black")
    draw.rectangle((92, 70, 108, 90), fill="black")
    assert worker._leading_extension_has_centered_ink(image, _extended()) is True


def test_recovery_keeps_original_when_retry_prefix_is_edge_bleed(monkeypatch) -> None:
    image = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 48, 86, 92), fill="black")
    draw.rectangle((114, 48, 120, 92), fill="black")
    region = {
        "text": "置いてきた",
        "raw_text": "置いてきた",
        "orientation": "vertical",
        "x": 0.40,
        "y": 0.20,
        "width": 0.15,
        "height": 0.25,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "hypotheses": [{"id": "manga-ocr", "text": "置いてきた", "selected": True}],
    }
    monkeypatch.setattr(worker, "_vertical_leading_ink_geometry", lambda _image, _item: _extended())
    recovered = worker._recover_vertical_leading_context(
        lambda _crop: "く、置いてきた", image, region
    )
    assert recovered["text"] == "置いてきた"
    assert recovered.get("selected_hypothesis_id") != "manga-ocr-leading-ink"


def test_recovery_keeps_legitimate_centered_prefix(monkeypatch) -> None:
    image = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((92, 45, 108, 62), fill="black")
    draw.rectangle((92, 70, 108, 90), fill="black")
    region = {
        "text": "にとって",
        "raw_text": "にとって",
        "orientation": "vertical",
        "x": 0.40,
        "y": 0.20,
        "width": 0.15,
        "height": 0.25,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "hypotheses": [{"id": "manga-ocr", "text": "にとって", "selected": True}],
    }
    ext = _extended()
    ext["text"] = "にとって"
    ext["raw_text"] = "にとって"
    monkeypatch.setattr(worker, "_vertical_leading_ink_geometry", lambda _image, _item: ext)
    recovered = worker._recover_vertical_leading_context(
        lambda _crop: "海賊にとって", image, region
    )
    assert recovered["text"] == "海賊にとって"
    assert recovered["selected_hypothesis_id"] == "manga-ocr-leading-ink"
