from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _trace_item() -> dict[str, object]:
    return {
        "text": "キミ",
        "raw_text": "き き",
        "orientation": "horizontal",
        "x": 0.462421,
        "y": 0.880667,
        "width": 0.106737,
        "height": 0.092,
        "confidence": 0.3,
        "detector": "vision-contrast+vision-inverted+vision-original",
        "segments": [
            {
                "text": "き",
                "orientation": "horizontal",
                "x": 0.478947,
                "y": 0.905,
                "width": 0.081579,
                "height": 0.058333,
                "source": "vision-accurate-range-v2",
            },
            {
                "text": "き",
                "orientation": "horizontal",
                "x": 0.468421,
                "y": 0.886667,
                "width": 0.094737,
                "height": 0.08,
                "source": "vision-accurate-range-v2",
            },
        ],
    }


class _TraceWidthModel:
    def __call__(self, image: Image.Image) -> str:
        # Exact v47 Mac trace windows:
        # ~143px wide => middle `きっ`
        # 182px wide  => wide `きっ！`
        if image.width <= 150:
            return "きっ"
        if image.width == 182:
            return "きっ！"
        return "きっ、"


def test_right_context_crop_matches_trace_pixel_window_without_generic_padding() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    try:
        middle, middle_geometry = worker._horizontal_right_context_crop(
            image, _trace_item(), right_factor=0.70
        )
        wide, wide_geometry = worker._horizontal_right_context_crop(
            image, _trace_item(), right_factor=1.18
        )
        assert middle is not None and middle_geometry is not None
        assert wide is not None and wide_geometry is not None
        assert middle.size == (143, 120)
        assert wide.size == (182, 120)
    finally:
        if "middle" in locals() and middle is not None:
            middle.close()
        if "wide" in locals() and wide is not None:
            wide.close()
        image.close()


def test_exact_trace_crop_recovers_exclamation_not_noisy_comma() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    try:
        item = _trace_item()
        result = worker._recover_clipped_horizontal_sfx(
            _TraceWidthModel(),
            image,
            item,
            [item],
        )
    finally:
        image.close()

    assert result["text"] == "きっ！"
    assert result["clipped_horizontal_sfx_middle_text"] == "きっ"
    assert result["clipped_horizontal_sfx_wide_text"] == "きっ！"
    assert result["clipped_horizontal_sfx_recovery"] is True
