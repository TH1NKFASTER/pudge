"""Generated drawing connections and title/dialogue conflicts, no page corpus."""

from copy import deepcopy

import pytest
from PIL import Image, ImageDraw
from synthetic_ocr import region

from pudge import manga_ocr_worker as worker


def _scene(*, scale=1, offset=0, connected=True):
    size = (round(760 * scale), round(1200 * scale))
    image = Image.new("RGB", size, "white")

    def box(values):
        return tuple(round(v * scale) for v in values)

    left = 330 + offset
    row = region("ほら", box((left, 450, left + 8, 478)), size=size, raw_text="", confidence=0.4)
    draw = ImageDraw.Draw(image)
    draw.rectangle(box((left - 36, 414, left - 1, 513)), fill="black")
    # Just four pixels enter the claimed text, connected to a much bigger drawing.
    x = left if connected else left + 2
    draw.rectangle(box((x, 462, x, 465)), fill="black")
    return image, row


@pytest.mark.parametrize("scale,offset", [(1, -180), (1, 0), (1, 220), (1.5, 0)])
def test_weak_guess_on_connected_drawing_is_removed(scale, offset):
    image, suspect = _scene(scale=scale, offset=offset)
    with image:
        dialogue = region("空がきれい", (100, 700, 140, 900), confidence=0.99)
        rows = [suspect, dialogue]
        before = deepcopy(rows)
        assert worker._art_connected_short_vertical_noise(image, suspect, rows)
        result = worker._suppress_art_connected_short_vertical_noise(image, rows)
        assert result == [dialogue] and result[0] is dialogue
        assert rows == before


@pytest.mark.parametrize(
    "change",
    [
        {"raw_text": "ほら"},
        {"confidence": 0.92},
        {"detector": "other"},
        {"source": "other"},
        {"orientation": "horizontal"},
        {"width": 0.08},
        {"text": "星"},
        {"text": "今日は星を見る"},
    ],
)
def test_durable_reading_or_geometry_guard_protects_text(change):
    image, suspect = _scene()
    with image:
        suspect.update(change)
        assert not worker._art_connected_short_vertical_noise(image, suspect, [suspect])
        assert worker._suppress_art_connected_short_vertical_noise(image, [suspect]) == [suspect]


@pytest.mark.parametrize("kind", ["blank", "disconnected", "independent_glyphs", "too_much_inside"])
def test_art_needs_physical_connection_and_large_outside_support(kind):
    image, suspect = _scene(connected=kind != "disconnected")
    with image:
        draw = ImageDraw.Draw(image)
        if kind == "blank":
            draw.rectangle((0, 0, 759, 1199), fill="white")
        elif kind == "independent_glyphs":
            draw.rectangle((294, 414, 370, 514), fill="white")
            draw.rectangle((331, 451, 336, 458), fill="black")
            draw.rectangle((331, 466, 336, 474), fill="black")
        elif kind == "too_much_inside":
            draw.rectangle((330, 450, 337, 477), fill="black")
        assert not worker._art_connected_short_vertical_noise(image, suspect, [suspect])


def _title_rows():
    donor = region(
        "あれ", (310, 500, 335, 570), raw_text="", confidence=0.4, detector="wide-vertical-text-donor-v1"
    )
    title = region("SAMPLE TITLE", (260, 515, 430, 557), orientation="horizontal", confidence=0.99)
    return donor, title


def test_weak_vertical_donor_borrowing_horizontal_latin_title_is_removed():
    with Image.new("RGB", (760, 1200), "white") as image:
        donor, title = _title_rows()
        dialogue = region("空がきれい", (100, 500, 130, 620), confidence=0.99)
        rows = [donor, title, dialogue]
        before = deepcopy(rows)
        assert worker._suppress_art_connected_short_vertical_noise(image, rows) == [title, dialogue]
        assert rows == before
        assert not worker._art_connected_short_vertical_noise(image, donor, [donor, dialogue])


@pytest.mark.parametrize(
    "change",
    [
        {"confidence": 0.8},
        {"orientation": "vertical"},
        {"text": "空がきれい"},
        {"width": 0.1},
        {"height": 0.1},
        {"x": 0.8},
        {"y": 0.1},
    ],
)
def test_title_conflict_requires_independently_verified_overlapping_latin_peer(change):
    with Image.new("RGB", (760, 1200), "white") as image:
        donor, title = _title_rows()
        title.update(change)
        assert not worker._art_connected_short_vertical_noise(image, donor, [donor, title])


def test_strong_vertical_donor_over_title_is_protected():
    with Image.new("RGB", (760, 1200), "white") as image:
        donor, title = _title_rows()
        donor["confidence"] = 0.99
        assert worker._suppress_art_connected_short_vertical_noise(image, [donor, title]) == [donor, title]
