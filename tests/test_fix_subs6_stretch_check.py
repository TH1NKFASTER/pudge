"""An audio map that stretched a one-clock file is undone when the speech onsets say so."""

from __future__ import annotations

import random
from pathlib import Path

from pudge import syncing
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles import scale_check


def _source(lines: int = 400):
    rng = random.Random(11)
    cues, start = [], 20.0
    for i in range(lines):
        cues.append((start, start + 1.6, f"line {i}"))
        start += rng.uniform(2.5, 5.0)
    return cues


def _speech(starts, *, seed: int = 2, noise_per_second: float = 0.2):
    rng = random.Random(seed)
    end = max(starts) + 30.0
    return sorted(
        [s + 0.15 for s in starts] + [rng.uniform(0.0, end) for _ in range(int(noise_per_second * end))]
    )


def _stretched(cues, stretch: float, shift: float, pivot: float = 700.0):
    """Every line ``stretch`` of its distance from ``pivot`` away from the clock ``shift``
    (an audio map fitted through the right clock mid-file)."""
    return [(s + shift + stretch * (s - pivot), e + shift + stretch * (s - pivot), t) for s, e, t in cues]


def _pairs(source, output):
    return [(i, source[i][0]) for i in range(len(source))]


def test_a_stretch_the_speech_does_not_follow_is_undone() -> None:
    source = _source()
    truth = [s - 9.7 for s, _e, _t in source]
    output = _stretched(source, 0.0417, -9.7)
    moved, info = scale_check.check_stretch(output, _pairs(source, output), _speech(truth))
    assert info["applied"] is True
    assert abs(float(info["clock_seconds"]) + 9.7) < 0.3
    assert max(abs(m[0] - t) for m, t in zip(moved, truth)) < 0.3


def test_a_stretch_the_speech_follows_is_kept() -> None:
    source = _source()
    output = _stretched(source, 0.0417, -9.7)
    moved, info = scale_check.check_stretch(
        output, _pairs(source, output), _speech([s for s, _e, _t in output])
    )
    assert info["applied"] is False
    assert info["reason"] == "stretch_fits_speech"
    assert moved == [(float(s), float(e), t) for s, e, t in output]


def test_one_clock_output_is_not_a_stretch() -> None:
    source = _source()
    output = [(s + 4.0, e + 4.0, t) for s, e, t in source]
    _moved, info = scale_check.check_stretch(
        output, _pairs(source, output), _speech([s for s, _e, _t in output])
    )
    assert info["applied"] is False and info["reason"] == "not_stretched"


def test_a_few_lines_on_their_own_clock_do_not_hide_the_stretch() -> None:
    source = _source()
    truth = [s - 9.7 for s, _e, _t in source]
    output = _stretched(source, 0.0417, -9.7)
    # The first lines came out of another block of the map, far from the stretch.
    output[:6] = [(s + 70.0, e + 70.0, t) for s, e, t in source[:6]]
    _moved, info = scale_check.check_stretch(output, _pairs(source, output), _speech(truth))
    assert info["applied"] is True


def test_sync_step_writes_the_one_clock_file(tmp_path: Path, monkeypatch) -> None:
    source_cues = _source()
    truth = [s - 9.7 for s, _e, _t in source_cues]
    source = tmp_path / "source.srt"
    output = tmp_path / "output.srt"
    write_srt(source_cues, source, preserve_order=True)
    write_srt(_stretched(source_cues, 0.0417, -9.7), output, preserve_order=True)
    onsets = _speech(truth)
    activity = [0.0] * int((max(onsets) + 5.0) * 100)
    for onset in onsets:
        frame = int(round(onset * 100))
        for k in range(frame, min(len(activity), frame + 20)):
            activity[k] = 1.0
    monkeypatch.setattr(syncing, "_audio_speech_activity", lambda *args, **kwargs: activity)
    result: dict[str, object] = {"engine": "alass+ffsubsync"}
    path = syncing._correct_clock_blocks(
        tmp_path / "video.mkv", source, output, result, tmp_path / "cache", syncing.SyncConfig()
    )
    assert path != output and result["output"] == str(path)
    assert result["stretch_check"]["applied"] is True
    moved = parse_srt(path)
    assert abs(moved[200][0] - truth[200]) < 0.3
