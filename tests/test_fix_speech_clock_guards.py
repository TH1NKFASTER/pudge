"""Speech (STT) clocks that moved right subtitle clocks, and karaoke layers on timing tracks.

Sync benchmark (Mac run of 2026-10-25), synthetic data only:

* Odd Taxi E06 / Dead Mount Death Play E12: the opening-plateau check moved everything
  after the OP by -0.3 / +0.5 s because the shifted clock matched two more Whisper
  segment starts within its 0.9 s tolerance (40 -> 42, 33 -> 35) while the matched
  lines sat twice as far from them (mean error 0.15 -> 0.27 s, 0.20 -> 0.44 s).
* Madougushi Dahlia E02: speech moved the cold open 72 s later, into the OP, while the
  same-video subtitle timeline (one clock for the whole episode) and the English track
  had it in place.
* BLUE REFLECTION RAY E13: the English ED karaoke drew each lyric again for every
  syllable and glow layer: 24000 events, which no timing check could use.
"""

from __future__ import annotations

import random
from pathlib import Path

from pudge.subtitle_formats import write_exact_srt
from pudge.syncing import _drop_typesetting_fragments, _local_speech_shift_estimate


def _dialogue(count: int = 40, seed: int = 4, start: float = 160.0) -> list[float]:
    rng = random.Random(seed)
    values, time = [], start
    for _ in range(count):
        time += rng.uniform(2.0, 5.0)
        values.append(round(time, 3))
    return values


def test_two_more_loose_matches_with_a_worse_error_do_not_move_a_clock() -> None:
    rng = random.Random(1)
    starts = _dialogue()
    # Whisper segments right on the lines (+-0.15 s), except every tenth line, whose segment
    # starts 1.0-1.15 s early (just outside the 0.9 s match tolerance): a -0.3 s shift
    # catches those four as well while every other line moves 0.3 s away from its segment.
    reference = [
        value - rng.uniform(1.0, 1.15) if index % 10 == 0 else value + rng.uniform(-0.15, 0.15)
        for index, value in enumerate(starts)
    ]
    estimate = _local_speech_shift_estimate(starts, sorted(reference), max_shift_seconds=3.0)
    assert estimate["matched_gain"] >= 2 and estimate["mean_error_gain_seconds"] < -0.2, estimate
    assert estimate["accepted"] is False, estimate
    assert estimate["shift_seconds"] == 0.0


def test_a_clear_clock_change_is_still_found() -> None:
    rng = random.Random(2)
    starts = _dialogue()
    reference = sorted(value + 0.6 + rng.uniform(-0.1, 0.1) for value in starts)
    estimate = _local_speech_shift_estimate(starts, reference, max_shift_seconds=3.0)
    assert estimate["accepted"] is True, estimate
    assert abs(float(estimate["shift_seconds"]) - 0.6) <= 0.11


def _write(path: Path, cues: list[tuple[float, float, str]]) -> Path:
    write_exact_srt(cues, path)
    return path


def _speech_moves_cold_open_off_a_stable_timeline(*args):
    from pudge.syncing import _speech_moves_cold_open_off_a_stable_timeline as check

    return check(*args)


def _episode(cold_shift: float) -> tuple[list[tuple[float, float, str]], list[tuple[float, float, str]]]:
    cold = [(10.0 + 4.0 * i, 12.5 + 4.0 * i, f"cold{i}") for i in range(12)]  # ends at 56.5
    rest = [(140.0 + 4.0 * i, 142.5 + 4.0 * i, f"line{i}") for i in range(200)]
    timeline = cold + rest
    speech = [(s + cold_shift, e + cold_shift, t) for s, e, t in cold] + rest
    return timeline, speech


def test_speech_that_moves_the_cold_open_into_the_op_loses_to_the_reference(tmp_path: Path) -> None:
    timeline, speech = _episode(cold_shift=72.0)
    reference = [(s + 0.1, e, "en") for s, e, _t in timeline]
    moved, meta = _speech_moves_cold_open_off_a_stable_timeline(
        {"timeline_segments": [{"offset_seconds": -1.0, "support": 22, "kind": "stable"}]},
        _write(tmp_path / "timeline.srt", timeline),
        _write(tmp_path / "speech.srt", speech),
        _write(tmp_path / "reference.srt", reference),
    )
    assert moved is True, meta
    assert meta["speech_cold_open_delta_seconds"] == 72.0


def test_speech_keeps_a_cold_open_move_the_reference_confirms(tmp_path: Path) -> None:
    timeline, speech = _episode(cold_shift=72.0)
    # The English track has the cold open where speech put it: the timeline was wrong there.
    reference = [(s + 0.1, e, "en") for s, e, _t in speech]
    moved, meta = _speech_moves_cold_open_off_a_stable_timeline(
        {"timeline_segments": [{"offset_seconds": -1.0, "support": 22, "kind": "stable"}]},
        _write(tmp_path / "timeline.srt", timeline),
        _write(tmp_path / "speech.srt", speech),
        _write(tmp_path / "reference.srt", reference),
    )
    assert moved is False, meta


def test_a_piecewise_timeline_is_not_judged_here(tmp_path: Path) -> None:
    timeline, speech = _episode(cold_shift=72.0)
    reference = [(s + 0.1, e, "en") for s, e, _t in timeline]
    moved, meta = _speech_moves_cold_open_off_a_stable_timeline(
        {
            "timeline_segments": [
                {"offset_seconds": -1.0, "support": 12},
                {"offset_seconds": 70.0, "support": 9},
            ]
        },
        _write(tmp_path / "timeline.srt", timeline),
        _write(tmp_path / "speech.srt", speech),
        _write(tmp_path / "reference.srt", reference),
    )
    assert moved is False and meta["reason"] == "timeline_not_one_clock"


def test_karaoke_layers_of_one_lyric_become_one_line() -> None:
    lines = [(5.0 + 3.0 * i, 7.5 + 3.0 * i, f"Dialogue line number {i}") for i in range(300)]
    karaoke = []
    for verse in range(8):
        start = 950.0 + 6.0 * verse
        text = f"{{\\k20}}Lyric verse number {verse} of the ending song"
        for syllable in range(12):  # one event per syllable and glow layer, all overlapping
            for layer in range(3):
                begin = start + 0.4 * syllable
                karaoke.append((round(begin, 3), round(start + 5.5 + 0.01 * layer, 3), text))
    kept, removed = _drop_typesetting_fragments(lines + karaoke)
    lyric_events = [cue for cue in kept if "Lyric" in cue[2]]
    assert len(lyric_events) == 8, len(lyric_events)
    assert removed == len(karaoke) - 8
    assert [cue for cue in kept if "Dialogue" in cue[2]] == lines


def test_repeated_lines_that_do_not_overlap_are_kept() -> None:
    lines = [
        (5.0 + 3.0 * i, 7.0 + 3.0 * i, "Same words again" if i % 10 == 0 else f"line {i}") for i in range(60)
    ]
    kept, removed = _drop_typesetting_fragments(lines)
    assert removed == 0 and kept == lines
