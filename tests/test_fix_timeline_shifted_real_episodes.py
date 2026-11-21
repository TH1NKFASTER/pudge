"""Real episodes shifted as a whole keep the timing they had before the wider cut search.

The cut search was widened past windows next to a cut (for a staircase -19 -> -16.25 ->
-14.5 that hid the real cut). Past an outlier window it put Re:Zero E19's opening cut into the
cold open by a chance crossover: the episode then needed the Japanese speech check and was not
Ready without it. With Dorohedoro E05 shifted by -42.23 s the cut moved from 63.8 s to 221 s
(50 more lines 20 s off). The widened range is now used past a staircase, or past other windows
only when the usual range finds no crossover. Expected values are those of
timeline-v6.22-bridged-singleton-tail.

A weak one-window tail is no longer a reason to reject before the tail guards run; when a guard
then drops it without the ending's lines fitting the kept clock, the map goes to the speech
check instead of being accepted silently (a real +20 s ending clock was dropped: ending and
preview 20 s off).
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from tests.synthetic_timeline import align_cues, cues_off, evaluate, real_fixture, real_truth, sample_params


@pytest.mark.parametrize(
    ("shift", "expected"),
    [
        (-20.0, [(0.0, -12.0), (233.935, -21.75)]),
        (-15.36, [(0.0, -16.0), (238.575, -26.5)]),
        (2.91, [(0.0, -34.0), (256.845, -44.75)]),
    ],
)
def test_shifted_rezero_keeps_its_opening_cut_without_a_speech_check(
    shift: float, expected: list[tuple[float, float]], tmp_path: Path
) -> None:
    source, reference = real_fixture("rezero")
    shifted = [(s + shift, e + shift, t) for s, e, t in source if s + shift >= 0.5]
    result, _starts = align_cues(shifted, reference, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    segments = [(s["source_start"], s["offset_seconds"]) for s in result["timeline_segments"]]
    assert [offset for _start, offset in segments] == [offset for _start, offset in expected]
    assert all(abs(a[0] - b[0]) <= 0.1 for a, b in zip(segments, expected)), segments
    assert result["timeline_early_edit_audio_verification"]["required"] is False


def test_shifted_dorohedoro_with_jitter_keeps_its_opening_cut(tmp_path: Path) -> None:
    source, reference = real_fixture("doro")
    truth = real_truth("doro", source)
    rng = random.Random(4)  # the draws of the review's shift sweep, trial 0
    shift = round(rng.uniform(-80, 80), 2)
    rng.choice([0.0, 0.0, 0.08, 0.15])
    jitter = rng.choice([0.0, 0.0, 0.1, 0.2])
    assert (shift, jitter) == (-42.23, 0.2)
    shifted = []
    for start, end, text in source:
        rng.random()
        move = shift + rng.uniform(-jitter, jitter)
        if start + move >= 0.5:
            shifted.append((start + move, end + move, text))
    kept = {text for _s, _e, text in shifted}
    result, starts = align_cues(shifted, reference, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    cuts = [s["source_start"] for s in result["timeline_segments"][1:]]
    assert abs(cuts[0] - 63.8) <= 1.0, result["timeline_segments"]  # not 221.0
    # Moving the Japanese file does not move where its lines belong on the video.
    shifted_truth = {text: value for text, value in truth.items() if text in kept}
    assert len(cues_off(starts, shifted_truth)) <= 14


def test_a_dropped_weak_tail_whose_lines_do_not_fit_goes_to_the_speech_check(tmp_path: Path) -> None:
    seed = 2016  # a 20 s scene only in the video 2 minutes before the end
    row = evaluate(sample_params(seed), seed, tmp_path)
    if row["accepted"]:
        assert row["frac_bad"] <= 0.01 or row["risk_required"], row
