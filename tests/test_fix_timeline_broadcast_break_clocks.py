"""A TV-timed subtitle keeps the commercial break the disc release does not have.

A Japanese file timed on a TV recording ("Retimed From TV", a raw TV capture) runs on two
clocks against the disc video: one before the break and one, about two minutes earlier,
after it; a capture that starts with the previous programme adds a minute or more to both.
The second clock lay outside the window search (+-120 s), and against a busy English track
(songs, signs) a 72 s window often ranks the right clock below chance peaks, so the path
bounced between unrelated clocks and the episode was rejected (or ALASS made a wrong map).
Each piece of the source between long dialogue-free gaps is now matched as a whole and its
clock stays a candidate in every window of that piece.

Fixture: cue timings of a real episode with placeholder texts (tests/fixtures).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.synthetic_timeline import align_cues, cues_off, real_fixture, real_truth, shift_after_gap


def _tv_timed(lead: float, break_seconds: float, at: float = 700.0):
    source, reference = real_fixture("doro")
    truth = real_truth("doro", source)
    shifted, _cut = shift_after_gap(source, at, break_seconds)
    tv = [(s + lead, e + lead, t) for s, e, t in shifted]
    return tv, reference, truth


@pytest.mark.parametrize(
    ("lead", "break_seconds"),
    [
        (0.0, 125.0),  # disc-relative file that kept the break: second clock beyond 120 s
        (90.0, 120.0),  # raw TV capture: both clocks far off
    ],
)
def test_both_clocks_of_a_tv_timed_file_are_found(tmp_path: Path, lead: float, break_seconds: float) -> None:
    source, reference, truth = _tv_timed(lead, break_seconds)
    result, starts = align_cues(source, reference, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    offsets = [round(float(row["offset_seconds"]), 1) for row in result["timeline_segments"]]
    assert any(abs(value - (-1.0 - lead)) <= 0.5 for value in offsets), offsets
    assert any(abs(value - (-1.0 - lead - break_seconds)) <= 0.5 for value in offsets), offsets
    assert len(cues_off(starts, truth, limit=0.5)) <= 0.02 * len(truth), offsets


def test_an_unbroken_file_keeps_its_single_clock(tmp_path: Path) -> None:
    source, reference = real_fixture("doro")
    truth = real_truth("doro", source)
    result, starts = align_cues(source, reference, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    assert len(result["timeline_segments"]) == 1, result["timeline_segments"]
    assert not cues_off(starts, truth, limit=0.5)
