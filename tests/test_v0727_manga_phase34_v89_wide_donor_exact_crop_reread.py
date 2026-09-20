from __future__ import annotations

from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


def _draw_vertical_glyphs(image: Image.Image, *, x0: int, widths: int, rows: list[tuple[int, int]]) -> None:
    draw = ImageDraw.Draw(image)
    for top, bottom in rows:
        draw.rectangle((x0, top, x0 + widths, bottom), fill="black")


def _donor(*, text: str, x: float, y: float, width: float, height: float, count: int, coverage: float) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": text,
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": 0.25,
        "detector": "wide-vertical-text-donor-v1",
        "source": worker._LAYOUT_LINE_SOURCE,
        "recognition_selection": "wide-vertical-layout-donor-v1",
        "selected_hypothesis_id": "wide-vertical-layout-donor-v1",
        "hypotheses": [
            {
                "id": "wide-vertical-layout-donor-v1",
                "text": text,
                "source": "recognized-wide-region",
                "selected": True,
            }
        ],
        "provenance": {
            "proposal_kind": "vertical_text_line_raw" if count > 1 else "vertical_text_line",
            "component_count": count,
            "component_coverage": coverage,
            "cluster_context_donor": True,
            "wide_vertical_text_donor": True,
        },
    }


def test_v89_exact_crop_rereads_truncated_wide_vertical_donor() -> None:
    image = Image.new("RGB", (400, 600), "white")
    # Six observed vertical glyph slots, matching あやまれ！！.
    _draw_vertical_glyphs(
        image,
        x0=80,
        widths=20,
        rows=[(100, 118), (125, 142), (150, 168), (176, 194), (202, 214), (220, 226)],
    )
    donor = _donor(
        text="まれ！",
        x=0.20,
        y=1.0 - 228 / 600,
        width=0.055,
        height=(228 - 98) / 600,
        count=1,
        coverage=1.0,
    )

    out = worker._reread_wide_vertical_layout_donors_exact_crop(
        lambda _crop: "あやまれ！！",
        image,
        [donor],
    )

    repaired = out[0]
    assert repaired["text"] == "あやまれ！！"
    assert repaired["raw_text"] == "あやまれ！！"
    assert repaired["selected_hypothesis_id"] == "wide-vertical-layout-donor-exact-crop-v1"
    assert repaired["provenance"]["wide_vertical_exact_crop_reread"] is True
    assert repaired["provenance"]["wide_vertical_exact_crop_original_text"] == "まれ！"
    assert "".join(segment["text"] for segment in repaired["segments"]) == "あやまれ！！"
    assert all(str(segment["source"]).startswith("layout-line-ink-v2") for segment in repaired["segments"])


def test_v89_rejects_unrelated_exact_crop_text_even_for_wide_donor() -> None:
    image = Image.new("RGB", (400, 600), "white")
    _draw_vertical_glyphs(
        image,
        x0=80,
        widths=20,
        rows=[(100, 118), (125, 142), (150, 168), (176, 194), (202, 214), (220, 226)],
    )
    donor = _donor(
        text="まれ！",
        x=0.20,
        y=1.0 - 228 / 600,
        width=0.055,
        height=(228 - 98) / 600,
        count=1,
        coverage=1.0,
    )

    out = worker._reread_wide_vertical_layout_donors_exact_crop(
        lambda _crop: "ぜんぜん違う",
        image,
        [donor],
    )

    assert out[0]["text"] == "まれ！"
    assert "wide_vertical_exact_crop_reread" not in out[0]["provenance"]
