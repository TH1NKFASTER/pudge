"""Picture edits put into real episodes: the aligner either finds the new clock or rejects.

Each case moves every cue after one real cue gap by a known amount (an inserted or removed
scene in one master) and checks the result against the true position of every line. A wrong
map that is accepted without the Japanese-speech check is what the user sees as "subtitles
are N seconds off from here on".

- a 4 s scene in the middle of Dorohedoro E05 gave a false one-window tail clock over the
  ending (the last scene and the preview 3-4 s off);
- a 1.2 s edit in the middle was merged into one clock: half of the episode 1.2 s off;
- a 15 s scene 2.5 minutes before the end was dropped as an "unproven tail": the last scene
  and the preview 15 s off;
- a 4 s scene in Re:Zero E19 got its clock change 40 s late.

Fixtures: cue timings of the real episodes (tests/fixtures/*timings*.json).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.synthetic_timeline import align_cues, cues_off, real_fixture, real_truth, shift_after_gap


def _reference_insert(name: str, at: float, seconds: float):
    source, reference = real_fixture(name)
    truth = real_truth(name, source)
    reference2, cut = shift_after_gap(reference, at, seconds)
    truth2 = {text: value + seconds if value >= cut else value for text, value in truth.items()}
    return source, reference2, truth2, cut


def _source_insert(name: str, at: float, seconds: float):
    source, reference = real_fixture(name)
    truth = real_truth(name, source)
    source2, cut = shift_after_gap(source, at, seconds)
    return source2, reference, truth, cut


def _off_share(starts: dict[str, float], truth: dict[str, float]) -> float:
    return len(cues_off(starts, truth)) / max(1, len(truth))


def test_the_clock_after_a_mid_episode_scene_holds_to_the_end(tmp_path: Path) -> None:
    source, reference, truth, _cut = _reference_insert("doro", 376.0, 4.0)
    result, starts = align_cues(source, reference, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    segments = result["timeline_segments"]
    assert abs(segments[-1]["offset_seconds"] - 3.0) <= 0.5, segments
    assert _off_share(starts, truth) <= 0.01, segments


def test_a_small_mid_episode_edit_gets_its_own_clock(tmp_path: Path) -> None:
    source, reference, truth, cut = _source_insert("doro", 712.0, 1.2)
    result, starts = align_cues(source, reference, tmp_path)
    assert result["accepted"] is True, result.get("reason")
    segments = result["timeline_segments"]
    assert len(segments) == 2, segments
    assert segments[0]["offset_seconds"] == -1.0
    assert abs(segments[1]["offset_seconds"] - -2.2) <= 0.25, segments
    assert abs(segments[1]["source_start"] - cut) <= 20.0, (segments, cut)
    assert _off_share(starts, truth) <= 0.01, segments


@pytest.mark.parametrize(("before_end", "seconds"), [(150.0, 15.0), (180.0, 25.0)])
def test_a_scene_inserted_before_the_ending_moves_the_ending_or_rejects(
    before_end: float, seconds: float, tmp_path: Path
) -> None:
    source, reference = real_fixture("doro")
    reference_end = max(end for _start, end, _text in reference)
    source, reference, truth, _cut = _reference_insert("doro", reference_end - before_end, seconds)
    result, starts = align_cues(source, reference, tmp_path)
    if result["accepted"]:
        last = result["timeline_segments"][-1]
        assert abs(last["offset_seconds"] - (seconds - 1.0)) <= 0.5, result["timeline_segments"]
        assert _off_share(starts, truth) <= 0.01, result["timeline_segments"]


@pytest.mark.xfail(
    strict=True,
    reason="TL-4: 40 s of lines after the cut fit both clocks about equally (different line splits); "
    "the whole-scan crossover vote still picks the later cut",
)
def test_a_four_second_scene_in_rezero_switches_the_clock_at_the_scene_or_rejects(tmp_path: Path) -> None:
    source, reference, truth, cut = _reference_insert("rezero", 778.0, 4.0)
    result, starts = align_cues(source, reference, tmp_path)
    if result["accepted"]:
        source_cut = cut + 41.75
        boundaries = [row["source_time"] for row in result["timeline_boundaries"]]
        assert any(abs(value - source_cut) <= 15.0 for value in boundaries), (boundaries, source_cut)


# name: (fixture, side, near, seconds, still-failing reason or None)
_EDITS = {
    "doro-reference+4s-mid": ("doro", "reference", 376.0, 4.0, None),
    "doro-source+1.2s-mid": ("doro", "source", 712.0, 1.2, None),
    "doro-reference+1.5s-mid": ("doro", "reference", 706.0, 1.5, "TL-7"),
    "doro-reference+15s-before-ending": ("doro", "reference", 1271.0, 15.0, None),
    "rezero-reference+4s-mid": ("rezero", "reference", 778.0, 4.0, "TL-7"),
}


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            value[:4],
            id=key,
            marks=[pytest.mark.xfail(strict=True, reason=value[4])] if value[4] else [],
        )
        for key, value in _EDITS.items()
    ],
)
def test_a_wrong_map_is_rejected_or_sent_to_the_speech_check(edit: tuple, tmp_path: Path) -> None:
    name, side, at, seconds = edit
    insert = _reference_insert if side == "reference" else _source_insert
    source, reference, truth, _cut = insert(name, at, seconds)
    result, starts = align_cues(source, reference, tmp_path)
    if result["accepted"] and not result["timeline_early_edit_audio_verification"]["required"]:
        assert _off_share(starts, truth) <= 0.01, result["timeline_segments"]
