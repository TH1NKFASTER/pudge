"""Stray clock blocks go back to their neighbours' clock when speech onsets clearly say so."""

from __future__ import annotations

import random
from pathlib import Path

from pudge import syncing
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles import clock_blocks


def _speech(true_starts: list[float], *, seed: int = 3, noise_per_second: float = 0.25, length: float = 0.0):
    """Onsets at each true line start (+0.15 s) and random noise onsets."""
    rng = random.Random(seed)
    end = length or (max(true_starts) + 30.0)
    noise = [rng.uniform(0.0, end) for _ in range(int(noise_per_second * end))]
    return sorted([start + 0.15 for start in true_starts] + noise)


def _episode(lines: int = 200, spacing: float = 4.0):
    return [(5.0 + spacing * i, 5.0 + spacing * i + 1.5, f"line {i}") for i in range(lines)]


def _output(raw, shifts):
    return [(start + shifts(i), end + shifts(i), text) for i, (start, end, text) in enumerate(raw)]


def _pairs(raw, output):
    return [(i, i, output[i][0] - raw[i][0]) for i in range(len(raw))]


def test_block_between_agreeing_clocks_goes_back() -> None:
    raw = _episode()
    truth = [start for start, _end, _text in raw]
    output = _output(raw, lambda i: 5.0 if 80 <= i < 100 else 0.0)
    corrected, info = clock_blocks.correct_clock_blocks(output, _pairs(raw, output), _speech(truth))
    assert info["applied"] is True
    assert info["moved_cues"] == 20
    assert [round(start, 3) for start, _end, _text in corrected] == [round(t, 3) for t in truth]


def test_real_clock_change_is_kept() -> None:
    raw = _episode()
    shift = lambda i: 5.0 if 80 <= i < 100 else 0.0  # noqa: E731
    output = _output(raw, shift)
    # The audio agrees with the output: the block really is 5 s later.
    speech = _speech([start for start, _end, _text in output])
    corrected, info = clock_blocks.correct_clock_blocks(output, _pairs(raw, output), speech)
    assert info["applied"] is False
    assert info["blocks"] and info["blocks"][0]["applied"] is False
    assert corrected == [(float(a), float(b), c) for a, b, c in output]


def test_tail_block_far_off_goes_back() -> None:
    raw = _episode()
    truth = [start for start, _end, _text in raw]
    output = _output(raw, lambda i: 1.3 + (100.0 if i >= 180 else 0.0))
    speech = _speech([t + 1.3 for t in truth], length=1000.0)
    corrected, info = clock_blocks.correct_clock_blocks(output, _pairs(raw, output), speech)
    assert info["applied"] is True
    assert info["blocks"][0]["kind"] == "tail"
    assert round(corrected[-1][0], 3) == round(truth[-1] + 1.3, 3)


def test_tail_on_its_own_clock_is_kept() -> None:
    raw = _episode()
    output = _output(raw, lambda i: 1.3 + (100.0 if i >= 180 else 0.0))
    speech = _speech([start for start, _end, _text in output])
    _corrected, info = clock_blocks.correct_clock_blocks(output, _pairs(raw, output), speech)
    assert info["applied"] is False


def test_single_stray_line_takes_the_clock_around_it() -> None:
    raw = _episode()
    truth = [start for start, _end, _text in raw]
    output = _output(raw, lambda i: 5.0 if i == 120 else -0.1)
    speech = _speech([t - 0.1 for t in truth])
    corrected, info = clock_blocks.correct_clock_blocks(output, _pairs(raw, output), speech)
    assert info["applied"] is True and info["moved_cues"] == 1
    assert round(corrected[120][0], 3) == round(truth[120] - 0.1, 3)


def test_repeated_line_matched_far_away_is_not_a_clock() -> None:
    raw = _episode()
    output = _output(raw, lambda i: 0.0)
    pairs = _pairs(raw, output)
    # A repeated line paired with a copy 40 lines later.
    pairs[50] = (50, 90, output[90][0] - raw[50][0])
    _corrected, info = clock_blocks.correct_clock_blocks(output, pairs, _speech([s for s, _e, _t in raw]))
    assert info["runs"] == 1 and info["applied"] is False


def test_block_that_would_break_line_order_is_kept() -> None:
    raw = _episode(spacing=2.0)
    truth = [start for start, _end, _text in raw]
    # Lines 80-99 sit 5 s late, and the source has them out of place by the same amount:
    # moving them back would put them before the lines around them.
    raw = [(s - 30.0, e - 30.0, t) if 80 <= i < 100 else (s, e, t) for i, (s, e, t) in enumerate(raw)]
    output = [
        (s + (35.0 if 80 <= i < 100 else 0.0), e + (35.0 if 80 <= i < 100 else 0.0), t)
        for i, (s, e, t) in enumerate(raw)
    ]
    _corrected, info = clock_blocks.correct_clock_blocks(output, _pairs(raw, output), _speech(truth))
    assert info["applied"] is False
    assert info["blocks"][0]["in_order"] is False


def test_sync_step_writes_the_corrected_file(tmp_path: Path, monkeypatch) -> None:
    raw_cues = _episode()
    truth = [start for start, _end, _text in raw_cues]
    source = tmp_path / "source.srt"
    output = tmp_path / "output.srt"
    write_srt(raw_cues, source, preserve_order=True)
    write_srt(_output(raw_cues, lambda i: 5.0 if 80 <= i < 100 else 0.0), output, preserve_order=True)
    onsets = _speech(truth)
    activity = [0.0] * int((max(onsets) + 5.0) * 100)
    for onset in onsets:
        frame = int(round(onset * 100))
        for k in range(frame, min(len(activity), frame + 20)):
            activity[k] = 1.0
    monkeypatch.setattr(syncing, "_audio_speech_activity", lambda *args, **kwargs: activity)
    result: dict[str, object] = {"engine": "embedded-reference+timeline"}
    path = syncing._correct_clock_blocks(
        tmp_path / "video.mkv", source, output, result, tmp_path / "cache", syncing.SyncConfig()
    )
    assert path != output and result["output"] == str(path)
    assert result["clock_block_correction"]["applied"] is True
    moved = parse_srt(path)
    assert abs(moved[90][0] - truth[90]) < 0.01


def test_clock_change_moves_to_where_the_speech_changes() -> None:
    raw = _episode()
    truth = [start + (5.0 if i >= 120 else 0.0) for i, (start, _end, _text) in enumerate(raw)]
    # The output changes clock 20 lines too early.
    output = _output(raw, lambda i: 5.0 if i >= 100 else 0.0)
    corrected, info = clock_blocks.correct_clock_blocks(output, _pairs(raw, output), _speech(truth))
    assert info["applied"] is True
    assert [b["kind"] for b in info["blocks"] if b["applied"]] == ["boundary"]
    assert [round(start, 3) for start, _end, _text in corrected] == [round(t, 3) for t in truth]


def test_clock_change_where_the_speech_changes_is_kept() -> None:
    raw = _episode()
    output = _output(raw, lambda i: 5.0 if i >= 100 else 0.0)
    speech = _speech([start for start, _end, _text in output])
    _corrected, info = clock_blocks.correct_clock_blocks(output, _pairs(raw, output), speech)
    assert info["applied"] is False


def test_lines_between_two_clock_changes_move_once() -> None:
    raw = _episode(300)
    truth = [start + (5.0 if i >= 200 else 0.0) for i, (start, _end, _text) in enumerate(raw)]
    # Lines 100-199 on a third clock between the two others.
    output = _output(raw, lambda i: 5.0 if i >= 200 else (-3.0 if i >= 100 else 0.0))
    corrected, info = clock_blocks.correct_clock_blocks(
        output, _pairs(raw, output), _speech(truth, length=1300.0)
    )
    applied = [b for b in info["blocks"] if b["applied"]]
    assert len(applied) == 1
    assert info["moved_cues"] == 100


def test_block_tested_as_a_whole_is_not_split_by_the_boundary_pass() -> None:
    raw = _episode()
    # Lines 80-100 on a clock 3.3 s late; the speech puts only their first 7 on the clock
    # around them, the other 14 where they are.
    output = _output(raw, lambda i: 3.3 if 80 <= i < 101 else 0.0)
    truth = [output[i][0] - (3.3 if 80 <= i < 87 else 0.0) for i in range(len(raw))]
    _corrected, info = clock_blocks.correct_clock_blocks(output, _pairs(raw, output), _speech(truth))
    assert info["applied"] is False
    assert [b["kind"] for b in info["blocks"]] == ["between"]
