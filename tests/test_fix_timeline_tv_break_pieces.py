"""TV-timed files with a kept commercial break, against the disc video's English track.

Sync benchmark, BLUE REFLECTION RAY E13/E15/E17/E18 (Retimed From TV): the Japanese file
runs on one clock before the break and on one two minutes earlier after it, beyond the
120 s window search. The episodes stayed unsynced (raw file, 127 s off) because

* the post-break clock was found for the first piece after the break only: a later piece
  (after a dialogue-free scene) with the same clock was dropped as a duplicate, and its
  windows never saw a clock beyond the search;
* a piece was scored with half of each bordering gap: the reference's busy ED karaoke,
  put into the source's ED gap by the right clock, made a chance clock win;
* a piece whose reference lacks some lines ranked a chance clock above its own, and the
  clock of the next piece, beyond the search, was not offered there;
* one or two chance windows over the cold open and the OP were segments of their own
  (support 1) and rejected the whole map;
* an English track with signs and songs (2.8x the Japanese lines) caps onset F1 near
  0.5, under the 0.42 the check wanted, although every source line had its counterpart.

Fixture: cue timings of a real episode with placeholder texts (tests/fixtures).
"""

from __future__ import annotations

import random
from pathlib import Path

from pudge.subtitles.timeline_alignment import _WindowMatch
from tests.synthetic_timeline import align_cues, cues_off, real_fixture, real_truth, shift_after_gap


def _without(cues, start: float, end: float):
    return [cue for cue in cues if not (start <= cue[0] < end)]


def _tv_break_with_quiet_scene(break_seconds: float = 125.0, at: float = 700.0):
    """A kept break at ``at`` and a dialogue-free scene later (both sides lose its lines)."""
    source, reference = real_fixture("doro")
    source = _without(source, 1000.0, 1032.0)
    reference = _without(reference, 999.0, 1031.0)
    truth = real_truth("doro", source)
    shifted, _cut = shift_after_gap(source, at, break_seconds)
    return shifted, reference, truth


def test_the_break_clock_holds_after_a_quiet_scene_past_the_break(tmp_path: Path) -> None:
    source, reference, truth = _tv_break_with_quiet_scene()
    result, starts = align_cues(source, reference, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    offsets = [round(float(row["offset_seconds"]), 1) for row in result["timeline_segments"]]
    assert any(abs(value - (-1.0 - 125.0)) <= 0.5 for value in offsets), offsets
    assert len(cues_off(starts, truth, limit=0.5)) <= 0.02 * len(truth), offsets


def test_a_busy_reference_ending_inside_the_source_gap_does_not_hide_the_clock(tmp_path: Path) -> None:
    source, reference, truth = _tv_break_with_quiet_scene()
    rng = random.Random(5)
    last = max(start for start, _end, _text in reference)
    # Karaoke-like ED on the reference only, right after its last line: on the true clock
    # it falls into the long trailing gap of the source.
    burst, t = [], last + 4.0
    while t < last + 88.0:
        burst.append((round(t, 3), round(t + rng.uniform(0.8, 2.5), 3), f"ed{len(burst)}"))
        t += rng.uniform(0.25, 0.6)
    result, starts = align_cues(source, sorted(reference + burst), tmp_path)
    assert result["accepted"] is True, result.get("reason")
    assert len(cues_off(starts, truth, limit=0.5)) <= 0.02 * len(truth)


def test_a_reference_with_many_extra_song_lines_is_still_accepted(tmp_path: Path) -> None:
    source, reference, truth = _tv_break_with_quiet_scene()
    rng = random.Random(3)
    ordered = sorted(reference)
    # Karaoke lines of the OP and the ED on the English track only (the Japanese file has
    # none there): three times the source's lines, onset F1 capped under 0.42.
    songs = [(a[1], b[0]) for a, b in zip(ordered, ordered[1:]) if b[0] - a[1] >= 60.0]
    extra = []
    for low, high in songs:
        t = low + 1.0
        while t < high - 1.0:
            extra.append((round(t, 3), round(t + rng.uniform(0.3, 0.8), 3), f"song{len(extra)}"))
            t += 0.2 * rng.uniform(0.8, 1.2)
    assert len(reference) + len(extra) >= 3.0 * len(source)
    result, starts = align_cues(source, sorted(reference + extra), tmp_path)
    assert result["accepted"] is True, (result.get("reason"), result.get("after"))
    assert len(cues_off(starts, truth, limit=0.5)) <= 0.02 * len(truth)


def _window(center: float, offset: float, score: float) -> _WindowMatch:
    return _WindowMatch(
        center=center,
        offset=offset,
        score=score,
        matched=8,
        source_count=10,
        reference_count=10,
        onset_coverage=0.8,
        onset_f1=0.8,
        activity_f1=0.8,
        mean_error=0.2,
        gap_fingerprint=0.5,
        edge_hint_distance=None,
    )


def _leading_guard(*args):
    from pudge.subtitles.timeline_alignment import _suppress_weak_leading_path_clock

    return _suppress_weak_leading_path_clock(*args)


def _opening_cues() -> list[tuple[float, float, str]]:
    # A short cold open, a 95 s OP without lines, then dialogue.
    cues = [(8.0 + 4.0 * i, 10.0 + 4.0 * i, f"c{i}") for i in range(8)]
    cues += [(135.0 + 3.0 * i, 137.0 + 3.0 * i, f"d{i}") for i in range(300)]
    return cues


def test_two_disagreeing_chance_windows_before_the_op_are_dropped() -> None:
    path = [_window(50.0, 69.0, 2.27), _window(122.0, 60.5, 2.23)]
    path += [_window(158.0 + 36.0 * i, -7.25, 3.0) for i in range(12)]
    kept, info = _leading_guard(_opening_cues(), path, (3.7, -217.0))
    assert info["applied"] is True, info
    assert [row.offset for row in kept[:1]] == [-7.25]
    assert len(kept) == len(path) - 2


def test_leading_windows_on_one_clock_are_a_clock_of_their_own() -> None:
    path = [_window(50.0, 69.0, 2.27), _window(122.0, 69.5, 2.23)]
    path += [_window(158.0 + 36.0 * i, -7.25, 3.0) for i in range(12)]
    kept, info = _leading_guard(_opening_cues(), path, (3.7, -217.0))
    assert info["applied"] is False and kept == path


def test_one_chance_window_needs_the_first_onset_hint_on_the_next_clock() -> None:
    path = [_window(50.0, 71.5, 2.17)] + [_window(122.0 + 36.0 * i, -7.0, 3.0) for i in range(12)]
    kept, info = _leading_guard(_opening_cues(), path, (-7.15, -219.0))
    assert info["applied"] is True and kept == path[1:]
    kept, info = _leading_guard(_opening_cues(), path, (3.7, -219.0))
    assert info["applied"] is False and kept == path


def test_the_clock_before_a_break_is_tuned_although_one_line_loses_its_neighbour() -> None:
    from pudge.subtitles.timeline_alignment import _fine_tune_segment_offsets

    # 91 lines before the break: windows said -7.0 s, the lines -7.4 s (median error 0.5 ->
    # 0.1 s); refused for one line that only the window clock put near a reference line.
    rng = random.Random(8)
    lines, time = [], 20.0
    for _ in range(91):
        time += rng.uniform(4.0, 9.0)
        lines.append(round(time, 3))
    stray = lines[-1] + 30.0
    reference = sorted([value - 7.4 + rng.uniform(-0.08, 0.08) for value in lines] + [stray - 7.0 + 1.2])
    tuned, rows = _fine_tune_segment_offsets(
        [{"offset_seconds": -7.0, "support": 11}], [], lines + [stray], reference
    )
    assert rows and rows[0]["applied"] is True, rows
    assert abs(float(tuned[0]["offset_seconds"]) - (-7.4)) <= 0.06
