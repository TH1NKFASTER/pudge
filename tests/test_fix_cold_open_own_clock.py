"""A cold open before the OP keeps its own clock (Futsutsuka na Akujo de wa Gozaimasu ga E04).

User: "the subtitles are the right ones but timed badly, at the very start at least". The
AT-X capture's cold open runs 32.5 s ahead of the CR video, the rest 42.5 s. Its 4 s TV
captions matched the English track one line off at -40 s too; the path paid for the 10 s
jump and took -40 s for the one window before the OP, so the cold open came 7-10 s early and
the hidden early edit skipped the speech check that held back the WEB files.

Invented timings only.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from pudge.subtitle_formats import parse_srt
from pudge.subtitles.timeline_alignment import (
    _leading_window_own_clock,
    _WindowMatch,
    align_subtitle_timelines,
)

COLD_CLOCK = -32.5
MAIN_CLOCK = -42.5


def _stamp(value: float) -> str:
    ms = int(round(value * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _srt(path: Path, cues: list[tuple[float, float]]) -> Path:
    blocks = [f"{i}\n{_stamp(s)} --> {_stamp(e)}\nline {i}\n" for i, (s, e) in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _tv_capture(tmp_path: Path) -> tuple[Path, Path]:
    """Reference (video clock): a cold open of 4 s captions, an OP gap, then dialogue."""
    rng = random.Random(5)
    cold: list[tuple[float, float]] = []
    t = 12.0
    for index in range(18):
        cold.append((t, t + 3.9))
        t += 5.0 if index == 8 else 4.0
    main: list[tuple[float, float]] = []
    t = 175.0
    while t < 1300.0:
        length = rng.uniform(1.2, 4.5)
        main.append((t, t + length))
        t += length + rng.uniform(0.1, 3.5)
        if rng.random() < 0.05:
            t += rng.uniform(6.0, 15.0)
    source = [(a - COLD_CLOCK, b - COLD_CLOCK) for a, b in cold] + [
        (a - MAIN_CLOCK, b - MAIN_CLOCK) for a, b in main
    ]
    # The reference lacks two of the cold-open captions (lines it does not subtitle).
    reference = [cue for index, cue in enumerate(cold) if index not in (3, 12)] + main
    return _srt(tmp_path / "tv.srt", source), _srt(tmp_path / "reference.srt", reference)


def test_the_cold_open_before_the_op_gets_its_own_clock(tmp_path: Path) -> None:
    source, reference = _tv_capture(tmp_path)
    out, result = align_subtitle_timelines(source, reference, tmp_path / "cache", force=True)
    assert result["accepted"] is True
    cold, main = result["timeline_segments"]
    assert abs(cold["offset_seconds"] - COLD_CLOCK) <= 0.3, cold  # was -40.5: a line off
    assert abs(main["offset_seconds"] - MAIN_CLOCK) <= 0.3, main
    # The clocks meet in the OP gap (source 112-217 s), not inside a scene.
    assert 112.0 <= cold["source_end"] <= 217.5
    assert result["timeline_leading_own_clock"]["applied"] is True
    # The early edit it shows is checked against Japanese speech like any other.
    assert result["timeline_early_edit_audio_verification"]["required"] is True
    starts = [start for start, _end, _text in parse_srt(out)]
    assert abs(starts[0] - 12.0) <= 0.3 and abs(starts[18] - 175.0) <= 0.3


def _window(center: float, offset: float, score: float, matched: int, coverage: float = 0.9) -> _WindowMatch:
    return _WindowMatch(
        center=center,
        offset=offset,
        score=score,
        matched=matched,
        source_count=16,
        reference_count=16,
        onset_coverage=coverage,
        onset_f1=coverage,
        activity_f1=0.8,
        mean_error=0.2,
        gap_fingerprint=0.8,
        edge_hint_distance=None,
    )


def _cues_with_op_gap() -> list[tuple[float, float, str]]:
    cold = [(40.0 + 4.0 * i, 43.9 + 4.0 * i, "x") for i in range(17)]  # ends at 107.9 s
    main = [(215.0 + 4.0 * i, 218.0 + 4.0 * i, "x") for i in range(200)]
    return cold + main


def _path(first: _WindowMatch) -> list[_WindowMatch]:
    return [first, *(_window(252.0 + 36.0 * i, MAIN_CLOCK, 3.2, 18) for i in range(6))]


def test_a_clearly_better_own_clock_replaces_the_path_clock() -> None:
    path_choice = _window(72.0, -40.0, 2.83, 11, 0.79)
    own = _window(72.0, COLD_CLOCK, 3.10, 14, 0.88)
    path, meta = _leading_window_own_clock(
        _cues_with_op_gap(), _path(path_choice), [(72.0, [own, path_choice])]
    )
    assert meta["applied"] is True and path[0] is own
    assert meta["gap_start_seconds"] == 107.9 and meta["gap_end_seconds"] == 215.0


def test_the_path_clock_stays_without_clear_evidence_or_an_op_gap() -> None:
    path_choice = _window(72.0, -40.0, 2.83, 11, 0.79)
    cues = _cues_with_op_gap()
    for own, reason in (
        (_window(72.0, COLD_CLOCK, 2.90, 14, 0.88), "own_clock_not_clearly_better"),  # score margin
        (_window(72.0, COLD_CLOCK, 3.10, 12, 0.88), "own_clock_not_clearly_better"),  # 1 line more
        (_window(72.0, COLD_CLOCK, 3.10, 14, 0.70), "own_clock_not_clearly_better"),  # coverage
        (_window(72.0, 30.0, 3.10, 14, 0.88), "own_clock_too_far_from_next_clock"),  # a chance peak
    ):
        path, meta = _leading_window_own_clock(cues, _path(path_choice), [(72.0, [own, path_choice])])
        assert meta["applied"] is False and meta["reason"] == reason and path[0] is path_choice
    # No dialogue-free gap after the window: one scene, continuity is evidence.
    continuous = [(40.0 + 4.0 * i, 43.9 + 4.0 * i, "x") for i in range(300)]
    own = _window(72.0, COLD_CLOCK, 3.10, 14, 0.88)
    path, meta = _leading_window_own_clock(continuous, _path(path_choice), [(72.0, [own, path_choice])])
    assert meta["reason"] == "no_long_gap_after_first_window" and path[0] is path_choice


def test_kurumi_e03_cold_open_takes_the_clock_speech_found() -> None:
    """FX Senshi Kurumi-chan E03 (real timings): speech had 0 s before the OP, -4.67 s after."""
    from test_fix_timeline_late_crossover_and_tail_offset import FIXTURE, _srt as kurumi_srt

    data = json.loads(FIXTURE.read_text())
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = kurumi_srt(root / "source.srt", data["source"], "ja")
        reference = kurumi_srt(root / "reference.srt", data["reference"], "en")
        _out, result = align_subtitle_timelines(source, reference, root / "cache", force=True)
    cold, middle, _late = result["timeline_segments"]
    assert cold["offset_seconds"] == 0.0 and middle["offset_seconds"] == -5.0  # cold open was -5.0
    assert 68.5 <= cold["source_end"] <= 178.04  # inside the OP gap, not 70 s into the episode
    assert result["timeline_early_edit_audio_verification"]["required"] is True
