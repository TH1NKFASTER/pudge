"""Synthetic leading two-character ink-band regression; no published page data."""
from __future__ import annotations

from copy import deepcopy

import pytest
from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


WIDTH, HEIGHT = 760, 1200
PREFIX = "青空"
TAIL_TEXT = "を見上げる！"
BOX = (180, 108, 209, 170)
BANDS = [(110, 132), (144, 166)]


def baseline():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    painter = ImageDraw.Draw(image)
    for top, bottom in BANDS:
        painter.rectangle((185, top, 204, bottom), fill="black")
    tail = {
        "text": TAIL_TEXT, "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "orientation": "vertical", "x": 179 / WIDTH, "y": 1 - 350 / HEIGHT,
        "width": 34 / WIDTH, "height": 160 / HEIGHT,
        "recognizer_retry": "vertical-leading-ink-v1",
        "provenance": {"leading_ink_geometry": {"extension_px": 45}},
    }
    return image, tail


class Model:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    def __call__(self, crop):
        self.calls.append(crop.size)
        return next(self.answers)


@pytest.fixture
def case(monkeypatch):
    image, tail = baseline()

    def candidates(_image, rows):
        if any(row.get("recognizer_retry") == "leading-two-glyph-ink-consensus-v1" for row in rows):
            return []
        if tail not in rows:
            return []
        return [(tail, BOX, BANDS)]

    monkeypatch.setattr(worker, "_missing_two_glyph_prefix_candidates", candidates)
    yield image, tail
    image.close()


def test_two_physically_banded_glyphs_added_without_rewriting_tail(case):
    image, tail = case
    snapshot = deepcopy(tail)
    model = Model([PREFIX] * 3)
    result = worker._recover_missing_two_glyph_prefix(model, image, [tail])
    assert result[0] is tail and tail == snapshot
    assert len(model.calls) == 3 and len(result) == 2
    added = result[1]
    assert added["text"] == PREFIX
    assert added["recognizer_retry"] == "leading-two-glyph-ink-consensus-v1"
    assert [segment["text"] for segment in added["segments"]] == list(PREFIX)
    assert all(segment["height"] > .005 for segment in added["segments"])
    assert worker._recover_missing_two_glyph_prefix(Model([]), image, result) == result


@pytest.mark.parametrize("answers", [
    [PREFIX, "青草", "緑空"], [PREFIX, "青い空", "青青"],
    [TAIL_TEXT] * 3, ["", "", ""],
])
def test_uncertain_readings_never_create_prefix(case, answers):
    image, tail = case
    assert worker._recover_missing_two_glyph_prefix(Model(answers), image, [tail]) == [tail]


def test_without_physical_candidate_no_ocr(case, monkeypatch):
    image, tail = case
    monkeypatch.setattr(worker, "_missing_two_glyph_prefix_candidates", lambda *_: [])
    model = Model([])
    assert worker._recover_missing_two_glyph_prefix(model, image, [tail]) == [tail]
    assert not model.calls
