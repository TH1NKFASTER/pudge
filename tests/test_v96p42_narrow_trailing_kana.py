"""Pixel-evidenced suffix bounds for narrow vertical lanes; no private pages."""
from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _page(with_bottom_stroke: bool = True):
    image = Image.new('RGB', (100, 180), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((33, 100, 35, 111), fill='black')
    draw.rectangle((39, 101, 43, 102), fill='black')
    if with_bottom_stroke:
        draw.rectangle((38, 109, 43, 110), fill='black')
    return image


def test_narrow_lane_recovers_three_detached_strokes():
    image = _page()
    # A 14 px-wide region is too narrow for the old 16 px acceptance guard.
    assert worker._extend_three_track_trailing_ink_v96p41(
        image, (30., 20., 44., 98.)
    ) == (30., 20., 44., 113.)


def test_narrow_lane_does_not_extend_for_two_strokes():
    box = (30., 20., 44., 98.)
    assert worker._extend_three_track_trailing_ink_v96p41(
        _page(with_bottom_stroke=False), box
    ) == box


def test_very_narrow_lane_remains_rejected():
    box = (30., 20., 42., 98.)
    assert worker._extend_three_track_trailing_ink_v96p41(
        _page(), box
    ) == box
