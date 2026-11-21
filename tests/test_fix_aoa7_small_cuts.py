"""Small cuts between two long clocks: the lines take the clocks the reference track and the
speech agree on (invented episodes; Aoashi S2 E07 had this shape: TV vs WEB, 0 -> +1.2 -> +2.0
-> +3.0 -> +4.2 s over three minutes, mapped on two clocks)."""

from __future__ import annotations

import random
from pathlib import Path

from pudge import syncing
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles import small_cuts

# A source line i lands at its start + _true_clock(i) on the video.
STEPS = ((90, 1.2), (102, 2.0), (114, 3.0), (126, 4.2))


def _true_clock(i: int) -> float:
    clock = 0.0
    for first, value in STEPS:
        if i >= first:
            clock = value
    return clock


def _two_clocks(i: int) -> float:
    """What a two-clock map makes of the staircase: the first clock up to line 108."""
    return 0.05 if i < 108 else 4.2


def _episode(lines: int = 220, seed: int = 5) -> list[tuple[float, float, str]]:
    rng = random.Random(seed)
    cues, start = [], 6.0
    for i in range(lines):
        cues.append((round(start, 2), round(start + 1.8, 2), f"line {i}"))
        start += rng.uniform(2.6, 5.4)
    return cues


def _reference(true_starts: list[float], *, seed: int = 7) -> list[float]:
    """An English track: most lines with a start near the voice, some missing, some signs."""
    rng = random.Random(seed)
    starts = [t + 0.1 + rng.uniform(-0.15, 0.15) for t in true_starts if rng.random() < 0.85]
    signs = [rng.uniform(0.0, max(true_starts)) for _ in range(len(true_starts) // 5)]
    return sorted(starts + signs)


def _speech(true_starts: list[float], *, seed: int = 3, noise_per_second: float = 0.3) -> list[float]:
    """Onsets at the voice (+0.15 s after the line's start) and noise onsets."""
    rng = random.Random(seed)
    end = max(true_starts) + 30.0
    noise = [rng.uniform(0.0, end) for _ in range(int(noise_per_second * end))]
    return sorted([t + 0.15 for t in true_starts if rng.random() < 0.9] + noise)


def _mapped(raw, clock):
    return [(start + clock(i), end + clock(i), text) for i, (start, end, text) in enumerate(raw)]


def _pairs(raw, output):
    return [(i, i, output[i][0] - raw[i][0]) for i in range(len(raw))]


def test_staircase_of_small_cuts_takes_the_agreed_clocks() -> None:
    raw = _episode()
    truth = [start + _true_clock(i) for i, (start, _end, _text) in enumerate(raw)]
    output = _mapped(raw, _two_clocks)
    corrected, info = small_cuts.correct_small_cuts(
        output, _pairs(raw, output), _reference(truth), _speech(truth)
    )
    assert info["applied"] is True
    errors = [corrected[i][0] - truth[i] for i in range(len(raw))]
    # The stretch the two clocks got wrong is now within a few tenths of the voice ...
    assert sum(abs(error) <= 0.3 for error in errors[92:124]) >= 26
    # ... and every line outside it keeps its clock.
    assert all(corrected[i] == output[i] for i in list(range(0, 88)) + list(range(128, len(raw))))
    order = [start for start, _end, _text in corrected]
    assert all(b >= a for a, b in zip(order, order[1:]))


def test_map_on_the_right_clocks_is_kept() -> None:
    raw = _episode()
    truth = [start + _true_clock(i) for i, (start, _end, _text) in enumerate(raw)]
    output = _mapped(raw, _true_clock)
    corrected, info = small_cuts.correct_small_cuts(
        output, _pairs(raw, output), _reference(truth), _speech(truth)
    )
    assert info["applied"] is False
    assert corrected == [(float(a), float(b), c) for a, b, c in output]


def test_reference_alone_does_not_move_lines_the_speech_keeps() -> None:
    raw = _episode()
    clock = lambda i: 0.05  # noqa: E731 - the output is right everywhere
    truth = [start + clock(i) for i, (start, _end, _text) in enumerate(raw)]
    # The reference track is a second late on lines 90-125 (its own timing slip).
    reference_truth = [t + (1.0 if 90 <= i < 126 else 0.0) for i, t in enumerate(truth)]
    output = _mapped(raw, clock)
    corrected, info = small_cuts.correct_small_cuts(
        output, _pairs(raw, output), _reference(reference_truth), _speech(truth)
    )
    assert info["applied"] is False
    assert corrected == [(float(a), float(b), c) for a, b, c in output]


def test_speech_alone_does_not_move_lines_the_reference_keeps() -> None:
    raw = _episode()
    truth = [start + 0.05 for start, _end, _text in raw]
    speech_truth = [t + (1.5 if 90 <= i < 126 else 0.0) for i, t in enumerate(truth)]
    output = _mapped(raw, lambda i: 0.05)
    _corrected, info = small_cuts.correct_small_cuts(
        output, _pairs(raw, output), _reference(truth), _speech(speech_truth)
    )
    assert info["applied"] is False


def test_too_few_lines_on_the_new_clock_stay() -> None:
    raw = _episode()
    truth = [start + (1.5 if 100 <= i < 104 else 0.0) for i, (start, _end, _text) in enumerate(raw)]
    output = _mapped(raw, lambda i: 0.0)
    corrected, info = small_cuts.correct_small_cuts(
        output, _pairs(raw, output), _reference(truth), _speech(truth)
    )
    assert info["applied"] is False
    assert corrected == [(float(a), float(b), c) for a, b, c in output]


def test_song_lines_inside_a_moved_stretch_move_with_it() -> None:
    raw = _episode()
    raw[110] = (raw[110][0], raw[110][1], "♪ la la ♪")
    truth = [start + _true_clock(i) for i, (start, _end, _text) in enumerate(raw)]
    output = _mapped(raw, _two_clocks)
    corrected, info = small_cuts.correct_small_cuts(
        output, _pairs(raw, output), _reference(truth), _speech(truth)
    )
    assert info["applied"] is True
    assert abs(corrected[110][0] - truth[110]) < 0.3


def _activity(onsets: list[float]) -> list[float]:
    activity = [0.0] * int((max(onsets) + 5.0) * 100)
    for onset in onsets:
        frame = int(round(onset * 100))
        for k in range(frame, min(len(activity), frame + 20)):
            activity[k] = 1.0
    return activity


def _sync_files(tmp_path: Path, monkeypatch):
    raw = _episode()
    truth = [start + _true_clock(i) for i, (start, _end, _text) in enumerate(raw)]
    source = tmp_path / "source.srt"
    output = tmp_path / "output.srt"
    reference = tmp_path / "reference.srt"
    write_srt(raw, source, preserve_order=True)
    write_srt(_mapped(raw, _two_clocks), output, preserve_order=True)
    write_srt([(t, t + 1.5, "Line.") for t in _reference(truth)], reference, preserve_order=True)
    monkeypatch.setattr(syncing, "_audio_speech_activity", lambda *args, **kwargs: _activity(_speech(truth)))
    return source, output, reference, truth


def test_sync_step_moves_the_small_cuts_by_the_embedded_reference(tmp_path: Path, monkeypatch) -> None:
    source, output, reference, truth = _sync_files(tmp_path, monkeypatch)
    result: dict[str, object] = {
        "engine": "embedded-reference+alass",
        "timing_reference": str(reference),
        "timing_reference_language": "eng",
    }
    path = syncing._correct_clock_blocks(
        tmp_path / "video.mkv", source, output, result, tmp_path / "cache", syncing.SyncConfig()
    )
    assert path != output and result["output"] == str(path)
    assert result["small_cut_correction"]["applied"] is True
    moved = parse_srt(path)
    assert abs(moved[100][0] - truth[100]) < 0.35
    assert abs(moved[120][0] - truth[120]) < 0.35


def test_sync_step_ignores_a_japanese_transcript_reference(tmp_path: Path, monkeypatch) -> None:
    source, output, reference, _truth = _sync_files(tmp_path, monkeypatch)
    result: dict[str, object] = {
        "engine": "japanese-stt+alass",
        "timing_reference": str(reference),
        "timing_reference_language": "ja",
    }
    syncing._correct_clock_blocks(
        tmp_path / "video.mkv", source, output, result, tmp_path / "cache", syncing.SyncConfig()
    )
    assert result["small_cut_correction"] == {
        "version": small_cuts.VERSION,
        "applied": False,
        "reason": "no_reference_track",
    }


# --- the rebuild rule: episodes prepared before the step are prepared again once -------------

_TWO_CLOCKS_ALIGNMENT = {
    "engine": "alass",
    "timing_reference": "/cache/timing-reference/ref.srt",
    "timing_reference_language": "eng",
    "final_timing_runs": [
        {"first_cue": 1, "last_cue": 111, "offset_seconds": 0.039},
        {"first_cue": 112, "last_cue": 334, "offset_seconds": 4.08},
    ],
}


def _selection(alignment: dict[str, object], source: str = "jimaku"):
    from pudge.subtitle_migrations import Selection

    return Selection(object(), {"id": 1, "source": source, "details": {"alignment": alignment}})


def test_rebuild_rule_takes_reference_timed_two_clock_selections_before_the_step() -> None:
    from pudge.subtitle_migrations import RULES_BY_ID, _small_cut_candidate

    rule = RULES_BY_ID["small_cuts"]
    assert rule.kind == "generation" and rule.scope == "unwatched"
    assert _small_cut_candidate(_selection(_TWO_CLOCKS_ALIGNMENT))
    # Already through the step, manual, a Japanese reference, one clock, or a far jump: left alone.
    assert not _small_cut_candidate(
        _selection({**_TWO_CLOCKS_ALIGNMENT, "small_cut_correction": {"applied": False}})
    )
    assert not _small_cut_candidate(_selection(_TWO_CLOCKS_ALIGNMENT, source="manual"))
    assert not _small_cut_candidate(_selection({**_TWO_CLOCKS_ALIGNMENT, "timing_reference_language": "jpn"}))
    one = {**_TWO_CLOCKS_ALIGNMENT, "final_timing_runs": _TWO_CLOCKS_ALIGNMENT["final_timing_runs"][:1]}
    assert not _small_cut_candidate(_selection(one))
    far = {
        **_TWO_CLOCKS_ALIGNMENT,
        "final_timing_runs": [{"offset_seconds": 0.0}, {"offset_seconds": 88.0}],
    }
    assert not _small_cut_candidate(_selection(far))
    assert not _small_cut_candidate(_selection({}))


def test_rebuild_rule_prepares_an_unwatched_episode_again_once(tmp_path, monkeypatch) -> None:
    from pudge import subtitle_migrations
    from subtitle_rebuild_support import assert_rebuilding_with_subtitles_kept
    from t71g08_support import subtitle_library

    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    row = manager.db.episode_by_path(video)
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=row.media_id,
        episode=row.episode,
        source="jimaku",
        candidate_name="old.srt",
        candidate_path=row.subtitle_path,
        status="selected",
        details={"alignment": dict(_TWO_CLOCKS_ALIGNMENT)},
    )
    assert subtitle_migrations.run_rule(manager, "small_cuts") == 1
    assert_rebuilding_with_subtitles_kept(manager, video)
    assert subtitle_migrations.run_rule(manager, "small_cuts") == 0  # once


def test_rebuild_rule_leaves_a_watched_episode_alone(tmp_path, monkeypatch) -> None:
    from pudge import subtitle_migrations
    from subtitle_rebuild_support import assert_not_prepared_again
    from t71g08_support import subtitle_library

    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    row = manager.db.episode_by_path(video)
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=row.media_id,
        episode=row.episode,
        source="jimaku",
        candidate_name="old.srt",
        candidate_path=row.subtitle_path,
        status="selected",
        details={"alignment": dict(_TWO_CLOCKS_ALIGNMENT)},
    )
    row.state = "watched"
    manager.db.upsert_episode(row)
    assert subtitle_migrations.run_rule(manager, "small_cuts") == 0
    assert_not_prepared_again(manager, video)
