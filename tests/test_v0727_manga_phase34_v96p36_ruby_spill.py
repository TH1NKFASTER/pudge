"""Synthetic, copyright-safe tests for narrow ruby/main-print fusion."""
from __future__ import annotations

from copy import deepcopy

import pytest

from pudge import manga_ocr_worker as worker


LAYOUT = worker._LAYOUT_LINE_SOURCE


def case():
    main = {
        "text": "新しい資料を読む", "source": LAYOUT,
        "orientation": "vertical", "x": .30, "y": .35,
        "width": .06, "height": .25,
        "segments": [{"text": char} for char in "新しい資料を読む"],
    }
    contaminated = {
        "text": "あいうえおかきく．．！！", "source": LAYOUT,
        "orientation": "vertical", "x": .353, "y": .39,
        "width": .018, "height": .16,
        "provenance": {"black_ratio": .05},
    }
    ruby = {
        "text": "あたらしい", "source": LAYOUT,
        "orientation": "vertical", "x": .375, "y": .38,
        "width": .015, "height": .12,
        "provenance": {"black_ratio": .05},
    }
    other = {"text": "これは別の文です", "source": LAYOUT, "orientation": "vertical",
             "x": .58, "y": .60, "width": .06, "height": .18}
    return [main, contaminated, ruby, other]


def test_only_contaminated_ruby_removed_and_input_untouched():
    before = case()
    snapshot = deepcopy(before)
    after = worker._suppress_mixed_ruby_spill_vertical_regions(before)
    assert before == snapshot
    assert [id(item) for item in after] == [id(before[0]), id(before[2]), id(before[3])]
    assert worker._suppress_mixed_ruby_spill_vertical_regions(after) == after


@pytest.mark.parametrize("change", [
    {"text": "あいうえおかきく"}, {"width": .035},
    {"source": "other"}, {"orientation": "horizontal"},
    {"provenance": {"black_ratio": .45}}, {"y": .70},
])
def test_insufficient_evidence_keeps_region(change):
    original = case()
    modified = {**original[1], **change}
    updated = worker._suppress_mixed_ruby_spill_vertical_regions(
        [original[0], modified, original[2], original[3]])
    assert any(row is modified for row in updated)


def test_peer_geometry_and_print_evidence_required():
    original = case()
    for edit in ({"x": .10}, {"width": .02}, {"height": .10}, {"text": "あいうえお"}):
        peer = {**original[0], **edit}
        assert original[1] in worker._suppress_mixed_ruby_spill_vertical_regions(
            [peer, original[1], original[2], original[3]])
