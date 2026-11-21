"""Speech maps that move the epilogue after the ending song off a one-clock timeline.

Sync benchmark worst-11 (Mac run of fx204), synthetic data only:

* Non Non Biyori Nonstop E07: the same-video subtitle timeline kept the whole episode on one
  clock (0.0 s) but asked speech to verify an early edit. The speech map agreed (+0.15 s)
  everywhere except the six epilogue lines after the ED, which it moved 36 s earlier, into
  the song and before its closing ``～♪``. The English track had them where the timeline put
  them; the speech map replaced the timeline and made a catastrophic region out of a file
  that was right as downloaded.
"""

from __future__ import annotations

from pathlib import Path

from pudge.subtitle_formats import write_exact_srt
from pudge.syncing import _speech_moves_cold_open_off_a_stable_timeline


def _write(path: Path, cues: list[tuple[float, float, str]]) -> Path:
    write_exact_srt(cues, path)
    return path


def _episode(epilogue_shift: float, *, body_shift: float = 0.15):
    """Body lines 30-1300 s, the ED (music marks only) 1310-1400 s, six epilogue lines after."""
    body = [(30.0 + 4.0 * i, 32.5 + 4.0 * i, f"line{i}") for i in range(318)]  # last ends 1300.5
    song = [(1310.0, 1315.0, "♪～"), (1395.0, 1400.0, "～♪")]
    epilogue = [(1401.0 + 3.0 * i, 1403.0 + 3.0 * i, f"after{i}") for i in range(6)]
    timeline = body + song + epilogue
    speech = (
        [(s + body_shift, e + body_shift, t) for s, e, t in body]
        + song
        + [(s + epilogue_shift, e + epilogue_shift, t) for s, e, t in epilogue]
    )
    return timeline, speech


_ONE_CLOCK = {"timeline_segments": [{"offset_seconds": 0.0, "support": 24, "kind": "stable"}]}


def test_speech_that_moves_the_epilogue_into_the_ending_loses_to_the_reference(tmp_path: Path) -> None:
    timeline, speech = _episode(epilogue_shift=-36.0)
    reference = [(s + 0.05, e, "en") for s, e, t in timeline if not t.startswith(("♪", "～"))]
    moved, meta = _speech_moves_cold_open_off_a_stable_timeline(
        _ONE_CLOCK,
        _write(tmp_path / "timeline.srt", timeline),
        _write(tmp_path / "speech.srt", speech),
        _write(tmp_path / "reference.srt", reference),
    )
    assert moved is True, meta
    assert meta["selection_reason"] == "embedded_timeline_over_speech_epilogue_move"
    epilogue = meta["epilogue"]
    assert epilogue["epilogue_lines"] == 6
    assert epilogue["speech_epilogue_delta_seconds"] == -36.0
    assert epilogue["timeline_reference_coverage"] == 1.0
    assert epilogue["speech_reference_coverage"] == 0.0


def test_speech_keeps_an_epilogue_move_the_reference_confirms(tmp_path: Path) -> None:
    timeline, speech = _episode(epilogue_shift=-36.0)
    # The English track has the epilogue where speech put it: the timeline was wrong there.
    reference = [(s + 0.05, e, "en") for s, e, t in speech if not t.startswith(("♪", "～"))]
    moved, meta = _speech_moves_cold_open_off_a_stable_timeline(
        _ONE_CLOCK,
        _write(tmp_path / "timeline.srt", timeline),
        _write(tmp_path / "speech.srt", speech),
        _write(tmp_path / "reference.srt", reference),
    )
    assert moved is False, meta
    assert meta["epilogue"]["reason"] == "reference_does_not_decide"


def test_a_small_epilogue_difference_is_left_to_speech(tmp_path: Path) -> None:
    timeline, speech = _episode(epilogue_shift=1.2)
    reference = [(s + 0.05, e, "en") for s, e, t in timeline if not t.startswith(("♪", "～"))]
    moved, meta = _speech_moves_cold_open_off_a_stable_timeline(
        _ONE_CLOCK,
        _write(tmp_path / "timeline.srt", timeline),
        _write(tmp_path / "speech.srt", speech),
        _write(tmp_path / "reference.srt", reference),
    )
    assert moved is False, meta
    assert meta["epilogue"]["reason"] == "speech_keeps_the_epilogue_clock"


def test_speech_that_moves_the_whole_episode_is_not_an_epilogue_move(tmp_path: Path) -> None:
    timeline, speech = _episode(epilogue_shift=-36.0, body_shift=-36.0)
    reference = [(s + 0.05, e, "en") for s, e, t in timeline if not t.startswith(("♪", "～"))]
    moved, meta = _speech_moves_cold_open_off_a_stable_timeline(
        _ONE_CLOCK,
        _write(tmp_path / "timeline.srt", timeline),
        _write(tmp_path / "speech.srt", speech),
        _write(tmp_path / "reference.srt", reference),
    )
    assert moved is False, meta
    assert meta["epilogue"]["reason"] == "speech_keeps_the_epilogue_clock"


def test_no_long_gap_before_the_last_lines_means_no_epilogue(tmp_path: Path) -> None:
    body = [(30.0 + 4.0 * i, 32.5 + 4.0 * i, f"line{i}") for i in range(340)]
    speech = [(s, e, t) for s, e, t in body[:-6]] + [(s - 36.0, e - 36.0, t) for s, e, t in body[-6:]]
    reference = [(s + 0.05, e, "en") for s, e, _t in body]
    moved, meta = _speech_moves_cold_open_off_a_stable_timeline(
        _ONE_CLOCK,
        _write(tmp_path / "timeline.srt", body),
        _write(tmp_path / "speech.srt", speech),
        _write(tmp_path / "reference.srt", reference),
    )
    assert moved is False, meta
    assert meta["epilogue"]["reason"] == "no_epilogue_after_a_long_gap"


def test_the_cold_open_check_is_unchanged(tmp_path: Path) -> None:
    cold = [(10.0 + 4.0 * i, 12.5 + 4.0 * i, f"cold{i}") for i in range(12)]
    rest = [(140.0 + 4.0 * i, 142.5 + 4.0 * i, f"line{i}") for i in range(200)]
    timeline = cold + rest
    speech = [(s + 72.0, e + 72.0, t) for s, e, t in cold] + rest
    reference = [(s + 0.1, e, "en") for s, e, _t in timeline]
    moved, meta = _speech_moves_cold_open_off_a_stable_timeline(
        _ONE_CLOCK,
        _write(tmp_path / "timeline.srt", timeline),
        _write(tmp_path / "speech.srt", speech),
        _write(tmp_path / "reference.srt", reference),
    )
    assert moved is True, meta
    assert meta["selection_reason"] == "embedded_timeline_over_speech_cold_open_move"
    assert "epilogue" not in meta
