"""v96p29: bounded physical punctuation proof, no page/text literals in production."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from pudge import manga
from pudge import manga_ocr_worker as worker


def _region(text="．．．こここだ！！"):
    return {
        "text": text,
        "orientation": "vertical",
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "manga-ink-components-v1",
        "x": 668 / 760,
        "y": 1 - 834 / 1200,
        "width": 19 / 760,
        "height": 119 / 1200,
    }


def _synthetic(*, fake_glyph=False, omit_stroke=False, displace_dot=False):
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    for i in range(9):
        x = 673 + (4 if displace_dot and i == 4 else 0)
        y = 717 + 10 * i
        draw.ellipse((x, y, x + 6, y + 7), fill="black")
    draw.rectangle((669, 808, 675, 823), fill="black")
    if not omit_stroke:
        draw.rectangle((679, 808, 685, 823), fill="black")
    if fake_glyph:
        draw.rectangle((668, 794, 685, 804), fill="black")
    return image


def test_physical_dot_stack_removes_unsupported_kana_without_moving_region():
    with _synthetic() as image:
        original = _region()
        result = worker._repair_ink_verified_punctuation_column(image, original)
        assert result["text"] == "………！！"
        assert result["recognition_correction"] == "vertical-punctuation-ink-proof-v1"
        assert result["provenance"]["punctuation_ink_proof"]["dot_components"] == 9
        assert result["provenance"]["punctuation_ink_proof"]["terminal_strokes"] == 2
        assert len(result["segments"]) == len(result["text"])
        assert all(result[k] == original[k] for k in ("x", "y", "width", "height"))
        assert original["text"] == "．．．こここだ！！"
        assert manga._finalize_recognized_regions([result])[0]["text"] == result["text"]
        assert worker._repair_ink_verified_punctuation_column(image, result) == result


@pytest.mark.parametrize(
    "image_kwargs",
    [
        {"fake_glyph": True},
        {"omit_stroke": True},
        {"displace_dot": True},
    ],
)
def test_missing_physical_proof_does_not_relabel(image_kwargs):
    with _synthetic(**image_kwargs) as image:
        row = _region()
        assert worker._repair_ink_verified_punctuation_column(image, row) is row


@pytest.mark.parametrize(
    "change",
    [
        {"text": "．．．星を見る！！"},
        {"text": "空には星がある"},
        {"detector": "manga-raw-components-v1"},
        {"source": "other"},
        {"orientation": "horizontal"},
        {"width": 0.15},
    ],
)
def test_surface_or_geometry_mismatch_stays_unchanged(change):
    # Changed Japanese content alone must never be enough to edit a valid lane.
    with Image.new("RGB", (760, 1200), "white") as image:
        row = {**_region(), **change}
        assert worker._repair_ink_verified_punctuation_column(image, row) is row


@pytest.mark.parametrize("offset", [-200, -80, 0])
def test_generated_page_only_edits_physically_proven_punctuation(offset):
    with _synthetic() as original, Image.new("RGB", original.size, "white") as image:
        image.paste(original, (offset, 0))
        punctuation = _region()
        punctuation["x"] += offset / image.width
        dialogue = {**_region("空には星がある"), "x": 0.15}
        old = [dialogue, punctuation]
        updated = worker._repair_ink_verified_punctuation_columns(image, old)
        assert len(updated) == len(old)
        assert updated[0] is dialogue
        assert updated[1]["text"] == "………！！"
        assert all(updated[1][key] == punctuation[key] for key in ("x", "y", "width", "height"))
        assert old[1]["text"] == "．．．こここだ！！"
