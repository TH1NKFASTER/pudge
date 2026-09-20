"""Synthetic regressions for v96p35. No manga page images or extracted OCR fixtures.

Full-volume, page-specific OCR acceptance is an optional private integration check,
not a requirement for the public unit suite.
"""
from __future__ import annotations

from copy import deepcopy

import pytest
from PIL import Image, ImageDraw

from pudge import manga, manga_ocr_worker as worker


WIDTH, HEIGHT = 760, 1200
LAYOUT = worker._LAYOUT_LINE_SOURCE


def region(text: str, box: tuple[int, int, int, int], **fields: object) -> dict:
    left, top, right, bottom = box
    return {
        "text": text,
        "raw_text": text,
        "orientation": "vertical",
        "source": LAYOUT,
        "x": left / WIDTH,
        "y": 1 - bottom / HEIGHT,
        "width": (right - left) / WIDTH,
        "height": (bottom - top) / HEIGHT,
        **fields,
    }


class Model:
    def __init__(self, answers: list[str]):
        self.answers = iter(answers)
        self.calls: list[tuple[int, int]] = []

    def __call__(self, image: Image.Image) -> str:
        self.calls.append(image.size)
        return next(self.answers)


@pytest.fixture
def full_lane(monkeypatch):
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    clipped = region(
        "ねェだと！？", (200, 190, 234, 445),
        source="expanded-vision-rectangle",
        recognizer_retry="vertical-leading-ink-v1",
    )
    independent = region("", (200, 130, 234, 375))
    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: [independent])
    # Segment construction is already covered by the dedicated layout tests;
    # keep this regression about candidate geometry and consensus.
    monkeypatch.setattr(
        worker, "_layout_line_character_segments",
        lambda _row, text, **_kw: [{"text": char} for char in text],
    )
    yield image, clipped, independent
    image.close()


def test_full_lane_requires_observed_longer_component(full_lane):
    image, clipped, independent = full_lane
    found = worker._full_lane_above_clipped_vision_tail_candidates(image, [clipped])
    assert len(found) == 1 and found[0][0] == 0
    assert found[0][1] == independent
    blocked = deepcopy(clipped)
    blocked["recognizer_retry"] = "unverified"
    assert worker._full_lane_above_clipped_vision_tail_candidates(image, [blocked]) == []


def test_full_lane_only_accepts_matching_ocr_consensus(full_lane):
    image, clipped, _ = full_lane
    model = Model(["許さねェだと！？"] * 3)
    recovered = worker._recover_full_lane_above_clipped_vision_tail(model, image, [clipped])
    assert len(model.calls) == 3
    assert recovered[0]["text"] == "許さねェだと！？"
    assert recovered[0]["recognizer_retry"] == "full-layout-component-vision-tail-consensus-v1"
    assert len(recovered[0]["segments"]) == len(recovered[0]["text"])
    assert manga._finalize_recognized_regions(worker._finalize_worker_output_regions(recovered))


@pytest.mark.parametrize("responses", [
    ["許さねェだと！？", "許さねェだと！？別", "別の言葉"],
    ["ねェだと！？"] * 3,
    ["", "", ""],
])
def test_unverified_full_lane_does_not_overwrite_old_text(full_lane, responses):
    image, clipped, _ = full_lane
    original = deepcopy(clipped)
    assert worker._recover_full_lane_above_clipped_vision_tail(Model(responses), image, [clipped]) == [original]


@pytest.fixture
def ruby_lane(monkeypatch):
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    ruby = region("じゅうぶん", (400, 1045, 415, 1095))
    # Four discrete synthetic ink blocks, not pixels from any published page.
    draw = ImageDraw.Draw(image)
    for top in (1048, 1070, 1092, 1114):
        draw.rectangle((373, top, 396, top + 12), fill="black")
    monkeypatch.setattr(
        worker, "_layout_line_character_segments",
        lambda _row, text, **_kw: [{"text": char} for char in text],
    )
    yield image, ruby
    image.close()


def test_short_ruby_needs_physical_main_ink(ruby_lane):
    image, ruby = ruby_lane
    assert len(worker._short_ruby_adjacent_lower_main_candidates(image, [ruby])) == 1
    blank = Image.new("RGB", image.size, "white")
    try:
        assert worker._short_ruby_adjacent_lower_main_candidates(blank, [ruby]) == []
    finally:
        blank.close()


def test_short_ruby_recovers_main_text_without_removing_furigana(ruby_lane):
    image, ruby = ruby_lane
    model = Model(["充分だ"] * 3)
    result = worker._recover_short_ruby_adjacent_lower_main(model, image, [ruby])
    assert len(model.calls) == 3
    assert result[0] == ruby
    assert len(result) == 2
    assert result[1]["text"] == "充分だ"
    assert result[1]["recognizer_retry"] == "short-ruby-main-ink-consensus-v1"
    assert len(result[1]["segments"]) == 3


@pytest.mark.parametrize("responses", [
    ["充分だ", "充分です", "充分かな"],
    ["じゅうぶん"] * 3,
    ["", "", ""],
])
def test_unverified_ruby_candidate_cannot_create_text(ruby_lane, responses):
    image, ruby = ruby_lane
    assert worker._recover_short_ruby_adjacent_lower_main(Model(responses), image, [ruby]) == [ruby]


def test_edge_connected_art_is_suppressed_but_dialogue_survives():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 715, 9, 855), fill="black")
    draw.rectangle((0, 900, 9, 1040), fill="black")
    fake = region("こちらの", (0, 700, 24, 1100), raw_text="", recognizer_retry="vertical-y-context-v1")
    normal = region("許さない！！！", (100, 700, 150, 900))
    recovered = region("おれは友達を", (200, 700, 240, 1000))
    rows = [fake, normal, recovered]
    unchanged = deepcopy(rows)
    try:
        kept = worker._suppress_unverified_left_edge_art_ocr(image, rows)
        assert kept == [normal, recovered]
        assert rows == unchanged
        blank = Image.new("RGB", image.size, "white")
        try:
            assert worker._suppress_unverified_left_edge_art_ocr(blank, rows) == rows
        finally:
            blank.close()
    finally:
        image.close()
