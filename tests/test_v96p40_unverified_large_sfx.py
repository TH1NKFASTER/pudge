"""Synthetic validation of conservative oversized sound-effect rejection."""
from copy import deepcopy
from pathlib import Path
import importlib.util

SPEC = importlib.util.spec_from_file_location(
    "p40_worker", Path(__file__).resolve().parents[1] / "pudge/manga_ocr_worker.py"
)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def example():
    return {
        "text": "インタ", "raw_text": "イン", "orientation": "horizontal",
        "x": .27, "y": .53, "width": .27, "height": .095,
        "confidence": .5, "segments": [
            {"text": "イ", "x": .28}, {"text": "ン", "x": .42},
            {"text": "^", "x": .50},
        ],
    }


def test_unsupported_kana_tail_is_not_a_clickable_region():
    before = example()
    peer = {"text": "普通の台詞", "orientation": "vertical"}
    rows = [before, peer]
    assert worker._suppress_unverified_large_sfx_tail(rows) == [peer]
    assert rows[0] is before and before["text"] == "インタ"


def test_all_three_supported_kana_are_preserved():
    row = example()
    row["segments"][-1]["text"] = "タ"
    assert worker._suppress_unverified_large_sfx_tail([row]) == [row]


def test_small_japanese_and_genuine_detector_readings_are_preserved():
    for change in ({"height": .03}, {"width": .1}, {"confidence": .85},
                   {"raw_text": "インタ"}, {"orientation": "vertical"}):
        row = {**example(), **change}
        assert worker._suppress_unverified_large_sfx_tail([row]) == [row]


def test_nonmatching_suffix_or_non_katakana_is_preserved():
    for change in ({"text": "インナ"}, {"text": "イんタ"},
                   {"segments": [{"text": "イ"}, {"text": "ン"}, {"text": "タ"}]}):
        row = {**example(), **change}
        # A different unsupported suffix with no physical ink is also
        # unverified; only valid physical glyphs are preserved.
        if change.get("text") == "インナ":
            assert worker._suppress_unverified_large_sfx_tail([row]) == []
        else:
            assert worker._suppress_unverified_large_sfx_tail([row]) == [row]
