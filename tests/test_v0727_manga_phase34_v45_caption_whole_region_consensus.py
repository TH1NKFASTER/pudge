from __future__ import annotations

from PIL import Image

from pudge.manga_ocr_worker import (
    _coherent_horizontal_multiline_exact_observation,
    _horizontal_observation_blocks_layout,
    _refresh_horizontal_line_with_mangaocr,
    _split_horizontal_multiline_region,
    _vertical_seed_candidate,
)


def _seg(text: str, x: float, y: float, width: float, height: float) -> dict[str, object]:
    return {
        "text": text,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "vision-accurate-range-v2",
        "orientation": "horizontal",
    }


def _p08_region() -> dict[str, object]:
    return {
        "text": "村の少年モンキーブロ・",
        "raw_text": "特の少 モンキー・D・ ルフィ",
        "x": 0.664817,
        "y": 0.534000,
        "width": 0.186066,
        "height": 0.089071,
        "orientation": "horizontal",
        "confidence": 0.5,
        "detector": "vision-contrast+vision-inverted+vision-rectangles-original",
        "segments": [
            _seg("約", 0.670817, 0.591673, 0.052450, 0.025398),
            _seg("の", 0.717524, 0.591483, 0.030412, 0.025229),
            _seg("少", 0.742193, 0.591263, 0.034359, 0.025259),
            _seg("モ", 0.673538, 0.561260, 0.057203, 0.025270),
            _seg("ン", 0.725181, 0.561506, 0.030230, 0.025142),
            _seg("キ", 0.749852, 0.561623, 0.040098, 0.025189),
            _seg("ー", 0.784390, 0.561788, 0.035164, 0.025165),
            _seg("・", 0.813995, 0.561929, 0.020362, 0.025095),
            _seg("ロ", 0.828797, 0.561999, 0.016086, 0.025074),
            _seg("ル", 0.715789, 0.540000, 0.040197, 0.021667),
            _seg("フ", 0.755987, 0.540000, 0.025658, 0.021667),
            _seg("ィ", 0.781645, 0.540000, 0.028882, 0.021667),
        ],
    }


def _p17_region() -> dict[str, object]:
    return {
        "text": "山賊棟梁ヒグマ",
        "raw_text": "山賊棟梁 ヒグマ",
        "x": 0.836105,
        "y": 0.524000,
        "width": 0.112000,
        "height": 0.058667,
        "orientation": "horizontal",
        "confidence": 0.5,
        "detector": "vision-contrast+vision-inverted+vision-rectangles-original",
        "segments": [
            _seg("山", 0.850000, 0.560000, 0.017763, 0.016667),
            _seg("賊", 0.867763, 0.560000, 0.026316, 0.016667),
            _seg("棟", 0.894079, 0.560000, 0.026316, 0.016667),
            _seg("梁", 0.920395, 0.560000, 0.016447, 0.016667),
            _seg("ヒ", 0.842105, 0.530000, 0.034079, 0.023333),
            _seg("グ", 0.876184, 0.530000, 0.036842, 0.023333),
            _seg("マ", 0.913026, 0.530000, 0.029079, 0.023333),
        ],
    }


def test_trace_p17_caption_is_not_promoted_to_vertical_seed() -> None:
    region = _p17_region()
    assert _coherent_horizontal_multiline_exact_observation(region)
    assert not _vertical_seed_candidate(region)


def test_trace_p17_caption_blocks_nested_short_vertical_artifacts() -> None:
    region = _p17_region()
    proposal = {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.868421,
        "y": 0.530833,
        "width": 0.038158,
        "height": 0.044167,
    }
    assert _horizontal_observation_blocks_layout(region, proposal)


def test_trace_p17_caption_splits_to_observed_correct_rows() -> None:
    pieces = _split_horizontal_multiline_region(_p17_region())
    assert [piece["text"] for piece in pieces] == ["山賊棟梁", "ヒグマ"]
    assert ["".join(seg["text"] for seg in piece["segments"]) for piece in pieces] == [
        "山賊棟梁",
        "ヒグマ",
    ]
    assert all(piece.get("caption_region_consensus") for piece in pieces)


def test_trace_p08_caption_combines_full_crop_and_detector_rows() -> None:
    pieces = _split_horizontal_multiline_region(_p08_region())
    assert [piece["text"] for piece in pieces] == [
        "村の少年",
        "モンキー・D・",
        "ルフィ",
    ]
    assert ["".join(seg["text"] for seg in piece["segments"]) for piece in pieces] == [
        "村の少年",
        "モンキー・D・",
        "ルフィ",
    ]
    # The real trace is missing one Vision range in each of the first two rows.
    # v45 may synthesize only those individual boxes, not the whole row.
    assert len(pieces[0]["segments"]) == 4
    assert len(pieces[1]["segments"]) == 7
    synthetic = [
        seg
        for piece in pieces[:2]
        for seg in piece["segments"]
        if seg.get("source") == "caption-consensus-synthetic-v1"
    ]
    assert len(synthetic) == 3
    assert all(seg.get("geometry_status") == "approximate" for seg in synthetic)


def test_caption_consensus_rows_are_not_degraded_by_line_refresh() -> None:
    piece = _split_horizontal_multiline_region(_p08_region())[0]

    class BadLocalModel:
        def __call__(self, _image: Image.Image) -> str:
            return "約の少"

    image = Image.new("RGB", (760, 1200), "white")
    try:
        refreshed = _refresh_horizontal_line_with_mangaocr(BadLocalModel(), image, piece)
    finally:
        image.close()
    assert refreshed["text"] == "村の少年"
    assert "".join(seg["text"] for seg in refreshed["segments"]) == "村の少年"


def test_single_row_horizontal_text_is_not_caption_protected() -> None:
    region = {
        "text": "ざい",
        "raw_text": "ざい",
        "x": 0.4,
        "y": 0.4,
        "width": 0.08,
        "height": 0.05,
        "orientation": "horizontal",
        "confidence": 0.3,
        "detector": "vision-rectangles-original",
        "segments": [
            _seg("ざ", 0.41, 0.41, 0.03, 0.02),
            _seg("い", 0.45, 0.41, 0.03, 0.02),
        ],
    }
    assert not _coherent_horizontal_multiline_exact_observation(region)
