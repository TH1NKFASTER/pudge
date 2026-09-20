"""Synthetic regression for shifted vertical contextual lanes (no published pixels)."""
from __future__ import annotations

from copy import deepcopy

import pytest
from PIL import Image

from pudge import manga_ocr_worker as worker


WIDTH, HEIGHT = 760, 1200
OLD = "うえおか"
FULL = "あいうえおか"


def sample():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    row = {
        "text": OLD,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "manga-context-gap-components-v1",
        "provenance": {"proposal_kind": "contextual_missing_vertical_text_line"},
        "orientation": "vertical", "x": 100 / WIDTH, "y": 1 - 480 / HEIGHT,
        "width": 24 / WIDTH, "height": 80 / HEIGHT,
        "segments": [{"text": char, "orientation": "vertical", "x": 109 / WIDTH,
                      "width": 25 / WIDTH, "y": (1 - (417 + 16 * i) / HEIGHT),
                      "height": 16 / HEIGHT} for i, char in enumerate(OLD)],
    }
    return image, row


class Model:
    def __init__(self, readings):
        self.readings = iter(readings)
        self.calls = []

    def __call__(self, image):
        self.calls.append(image.size)
        return next(self.readings)


@pytest.fixture
def case(monkeypatch):
    image, row = sample()

    def geometry(_image, anchored):
        assert anchored["x"] > row["x"]
        result = dict(anchored)
        result["y"] = 1 - 480 / HEIGHT
        result["height"] = 114 / HEIGHT
        result["provenance"] = {**anchored["provenance"], "leading_ink_geometry": {
            "extension_px": 34, "new_top_px": 366, "old_top_px": 400,
        }}
        return result

    monkeypatch.setattr(worker, "_vertical_leading_ink_geometry", geometry)
    monkeypatch.setattr(worker, "_leading_extension_has_centered_ink", lambda *_: True)
    monkeypatch.setattr(worker, "_leading_prefix_width_supported", lambda *_: True)
    yield image, row
    image.close()


def test_synthetic_clipped_prefix_uses_consensus_and_keeps_geometry(case):
    image, row = case
    snapshot = deepcopy(row)
    model = Model([FULL] * 3)
    result = worker._recover_shifted_contextual_leading_lanes(model, image, [row])
    assert len(model.calls) == 3
    assert row == snapshot
    assert result[0]["text"] == FULL
    assert result[0]["recognizer_retry"] == "shifted-context-gap-leading-consensus-v1"
    assert result[0]["x"] > row["x"]
    assert len(result[0]["segments"]) == len(FULL)
    assert worker._recover_shifted_contextual_leading_lanes(Model([]), image, result) == result


@pytest.mark.parametrize("readings", [
    [FULL, OLD, FULL + "ほ"],
    [FULL, "違う", "あいうえおかな"],
    [OLD] * 3,
])
def test_no_unverified_prefix_change(case, readings):
    image, row = case
    model = Model(readings)
    assert worker._recover_shifted_contextual_leading_lanes(model, image, [row])[0] is row


@pytest.mark.parametrize("change", [
    {"source": "other"}, {"detector": "other"},
    {"orientation": "horizontal"}, {"recognizer_retry": "done"},
    {"segments": []},
])
def test_other_sources_and_layouts_are_untouched(case, change):
    image, row = case
    modified = {**row, **change}
    model = Model([])
    assert worker._recover_shifted_contextual_leading_lanes(model, image, [modified]) == [modified]
    assert model.calls == []


def test_no_physical_leading_extension_means_no_ocr(case, monkeypatch):
    image, row = case
    monkeypatch.setattr(worker, "_vertical_leading_ink_geometry", lambda *_: None)
    model = Model([])
    assert worker._recover_shifted_contextual_leading_lanes(model, image, [row]) == [row]
    assert model.calls == []


def test_centered_ink_and_prefix_width_checks_remain_mandatory(case, monkeypatch):
    image, row = case
    for guard in ("_leading_extension_has_centered_ink", "_leading_prefix_width_supported"):
        with monkeypatch.context() as scope:
            scope.setattr(worker, guard, lambda *_: False)
            model = Model([FULL] * 3)
            assert worker._recover_shifted_contextual_leading_lanes(model, image, [row]) == [row]
