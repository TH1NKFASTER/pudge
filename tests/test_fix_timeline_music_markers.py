"""Music-only marker cues (♪～, ♬～) are no timing evidence for the timeline aligner.

Jimaku Japanese files mark the opening/ending songs with chained ``♬～`` / ``♪～`` cues while
the embedded English track has nothing there (or its own lyric lines). As onsets those
markers matched dense dialogue by chance: one ``♪～`` cue after Kurumi-chan E03's last line
made the whole episode fail validation, and a ``♬～`` chain over Re:Zero E19's opening gap
moved the post-opening clock change by two minutes or reordered cues. ``♪ lyric ♪`` lines
with words stay evidence: dropping them on one side only puts a false head clock on Kurumi
when the English track has plain lyric lines at the same times.

Fixtures: cue timings of the real episodes (tests/fixtures/*timings*.json).
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from tests.synthetic_timeline import align_cues, cues_off, lyric_lines, real_fixture, real_truth

REZERO_OPENING_MARKERS = [
    (205.959, 230.007, "♬～"),
    (230.107, 244.448, "♬～"),
    (244.548, 245.995, "♬～"),
    (246.095, 254.965, "♬～"),
    (255.065, 279.362, "♬～"),
    (279.462, 303.042, "♬～"),
]


def test_one_music_marker_after_the_last_line_keeps_kurumi_aligned(tmp_path: Path) -> None:
    source, reference = real_fixture("kurumi")
    result, starts = align_cues(source + [(1305.0, 1308.0, "♪～")], reference, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    # The cold open (source 8-68 s, before the OP gap 68.5-178 s) runs on 0 s: 12 of its 16 lines
    # start within 0.5 s of an English line there (4 at -5 s), and Japanese speech found 0 s
    # before the OP too (test_fix_fresh_rebuild_keeps_subtitles_and_late_cut.py). REAL_CLOCKS
    # puts it on -5 s; since the leading-window clock (test_fix_cold_open_own_clock.py) the
    # aligner gives it its own clock, so its truth is taken from that evidence here.
    cold, first, last = result["timeline_segments"]
    assert cold["offset_seconds"] == 0.0 and 68.5 <= cold["source_end"] <= 178.04, cold
    assert first["offset_seconds"] == -5.0
    assert 1.2 <= last["offset_seconds"] <= 1.6, last
    opening, boundary = result["timeline_boundaries"]
    assert 68.5 <= opening["source_time"] <= 178.04, opening
    assert 1016.0 <= boundary["source_time"] <= 1024.0, boundary
    truth = real_truth("kurumi", source)
    truth.update({text: start for start, end, text in source if end <= 68.5})
    assert cues_off(starts, truth) == []


def test_a_music_marker_chain_over_the_rezero_opening_keeps_both_clocks(tmp_path: Path) -> None:
    source, reference = real_fixture("rezero")
    result, starts = align_cues(source + REZERO_OPENING_MARKERS, reference, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    assert [(s["offset_seconds"], s["source_end"]) for s in result["timeline_segments"]] == [
        (-31.5, 253.935),
        (-41.75, None),
    ]
    assert not result["timeline_early_edit_audio_verification"]["required"]
    assert cues_off(starts, real_truth("rezero", source)) == []


@pytest.mark.parametrize("seed", [1, 3])
def test_lyric_lines_on_both_sides_keep_kurumi_without_a_head_clock(seed: int, tmp_path: Path) -> None:
    source, reference = real_fixture("kurumi")
    rng = random.Random(seed)
    lyrics = lyric_lines(rng, 68.5, 178.0, "op")
    english = [
        (s - 5.0 + rng.gauss(0, 0.06), e - 5.0, f"EN lyric {i}") for i, (s, e, _t) in enumerate(lyrics)
    ]
    result, starts = align_cues(source + lyrics, reference + english, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    offsets = [segment["offset_seconds"] for segment in result["timeline_segments"]]
    assert offsets[0] == -5.0 and len(offsets) == 2, result["timeline_segments"]
    assert cues_off(starts, real_truth("kurumi", source)) == []


@pytest.mark.xfail(
    strict=True,
    reason="TL-1: one-sided '♪ lyric ♪' lines with words at the very start still decide the first clock",
)
def test_an_episode_that_starts_with_japanese_lyric_lines_keeps_the_dorohedoro_clock(tmp_path: Path) -> None:
    source, reference = real_fixture("doro")
    truth = real_truth("doro", source)
    opening_start = 67.0
    lyrics = lyric_lines(random.Random(0), opening_start, 145.8, "op")
    # The episode starts with the opening: drop the cold open on both sides.
    kept = [cue for cue in source if cue[0] >= opening_start]
    first_true = min(truth[text] for _s, _e, text in kept)
    crop = max(0.0, min(opening_start, first_true - 2.0) - 3.0)
    source2 = [(s - crop, e - crop, t) for s, e, t in kept + lyrics]
    reference2 = [(s - crop, e - crop, t) for s, e, t in reference if s >= first_true - 2.0 and s - crop >= 0]
    truth2 = {text: truth[text] - crop for _s, _e, text in kept}
    result, starts = align_cues(source2, reference2, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    assert [segment["offset_seconds"] for segment in result["timeline_segments"]] == [-1.0]
    assert cues_off(starts, truth2) == []
