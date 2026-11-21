"""The block before the opening song moves to its own clock only when transcript and onsets agree.

Invented data only: a cold open of six lines, an opening song of music marks, then the episode.
"""

from __future__ import annotations

import random
from pathlib import Path

from pudge import syncing
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles import cold_open

COLD_OPEN = [6.0, 9.5, 13.0, 16.5, 20.0, 24.0]
LINES = [
    "あさのそらはあおい",
    "きのうのよるはさむかった",
    "ねこがやねのうえでねている",
    "はやくがっこうへいこう",
    "てがみをかいておくる",
    "かわのみずはつめたい",
]


def _episode(cold_shift: float):
    cues = [(t + cold_shift, t + cold_shift + 2.0, LINES[i]) for i, t in enumerate(COLD_OPEN)]
    cues += [(30.0 + 15.0 * k, 44.0 + 15.0 * k, "♪～") for k in range(5)]
    t = 110.0
    for i in range(120):
        cues.append((t, t + 2.0, f"本編の台詞{i}"))
        t += 4.0
    return cues


def _speech(true_starts, seed: int = 3):
    rng = random.Random(seed)
    noise = [rng.uniform(0.0, 600.0) for _ in range(120)]
    return sorted([t + 0.1 for t in true_starts] + noise)


def _transcript_score(true_starts):
    """Text sits beside a line when the line is within a second of where it is spoken."""

    def score(cues):
        compared = [abs(start - true) for (start, _end, _text), true in zip(cues, true_starts)]
        return sum(1.0 if gap <= 1.0 else 0.3 for gap in compared) / len(compared)

    return score


def test_cold_open_five_seconds_early_moves_to_the_speech() -> None:
    output = _episode(cold_shift=-5.0)
    true_starts = list(COLD_OPEN)
    onsets = _speech(true_starts + [c[0] for c in output[11:]])
    moved, info = cold_open.check_cold_open(output, onsets, _transcript_score(true_starts))
    assert info["applied"] is True
    assert info["shift_seconds"] == 5.0
    assert [round(c[0], 3) for c in moved[:6]] == COLD_OPEN
    assert moved[6:] == [(float(a), float(b), t) for a, b, t in output[6:]]


def test_cold_open_already_right_is_kept() -> None:
    output = _episode(cold_shift=0.0)
    onsets = _speech(list(COLD_OPEN) + [c[0] for c in output[11:]])
    _moved, info = cold_open.check_cold_open(output, onsets, _transcript_score(list(COLD_OPEN)))
    assert info["applied"] is False and info["reason"] == "current_clock_kept"


def test_onsets_alone_do_not_move_it() -> None:
    output = _episode(cold_shift=-5.0)
    onsets = _speech(list(COLD_OPEN) + [c[0] for c in output[11:]])
    flat = lambda cues: 0.5  # noqa: E731 - a transcript with nothing to say there
    _moved, info = cold_open.check_cold_open(output, onsets, flat)
    assert info["applied"] is False
    _moved, info = cold_open.check_cold_open(output, onsets, None)
    assert info["applied"] is False and info["reason"] == "no_transcript"


def test_text_alone_does_not_move_it() -> None:
    output = _episode(cold_shift=-5.0)
    # The speech onsets sit where the lines are now: the text gain has no onset support.
    onsets = _speech([c[0] for c in output])
    _moved, info = cold_open.check_cold_open(output, onsets, _transcript_score(list(COLD_OPEN)))
    assert info["applied"] is False


def test_no_opening_song_no_check() -> None:
    output = [(2.0 + 4.0 * i, 4.0 + 4.0 * i, f"台詞{i}") for i in range(100)]
    _moved, info = cold_open.check_cold_open(output, [1.0, 2.0], lambda cues: 1.0)
    assert info["reason"] == "no_cold_open"


def test_sync_step_uses_the_transcript_of_a_speech_clocked_result(tmp_path: Path, monkeypatch) -> None:
    source_cues = _episode(cold_shift=0.0)
    output_cues = _episode(cold_shift=-5.0)
    true_starts = list(COLD_OPEN)
    source = tmp_path / "source.srt"
    output = tmp_path / "output.srt"
    transcript = tmp_path / "reference.ja.srt"
    write_srt(source_cues, source, preserve_order=True)
    write_srt(output_cues, output, preserve_order=True)
    write_srt([(t, t + 2.0, LINES[i]) for i, t in enumerate(true_starts)], transcript, preserve_order=True)
    onsets = _speech(true_starts + [c[0] for c in output_cues[11:]])
    activity = [0.0] * int(700 * 100)
    for onset in onsets:
        for k in range(int(round(onset * 100)), min(len(activity), int(round(onset * 100)) + 20)):
            activity[k] = 1.0
    monkeypatch.setattr(syncing, "_audio_speech_activity", lambda *args, **kwargs: activity)
    result: dict[str, object] = {
        "engine": "japanese-stt+text-clock-plateaus",
        "stt": {"reference_path": str(transcript)},
    }
    path = syncing._correct_clock_blocks(
        tmp_path / "video.mkv", source, output, result, tmp_path / "cache", syncing.SyncConfig()
    )
    assert result["cold_open_check"]["applied"] is True
    assert abs(parse_srt(path)[0][0] - COLD_OPEN[0]) < 0.01


def test_transcript_of_a_copied_result_is_found_in_this_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    transcript = cache / "stt" / "abc123" / "reference.ja.srt"
    transcript.parent.mkdir(parents=True)
    write_srt([(6.0, 8.0, LINES[0])], transcript, preserve_order=True)
    result = {"stt": {"reference_path": "/elsewhere/pudge-cache/stt/abc123/reference.ja.srt"}}
    assert syncing._transcript_text_score(result) is None
    score = syncing._transcript_text_score(result, cache)
    assert score is not None and score([(6.0, 8.0, LINES[0])]) == 1.0


def test_moved_cold_open_does_not_vote_on_the_episode_lead(tmp_path: Path, monkeypatch) -> None:
    cues = _episode(cold_shift=0.0)
    path = tmp_path / "out.srt"
    write_srt(cues, path, preserve_order=True)
    starts = [c[0] for c in cues]
    # The rest of the episode: 63 lines lead the voice by 0.10 s, 62 by 0.14 s (median 0.10);
    # the six moved cold-open lines lead by 0.4 s and would tip the median to 0.14.
    onsets = [s + (0.4 if i < 6 else (0.10 if i < 69 else 0.14)) for i, s in enumerate(starts)]
    frames = [0.0] * 70000
    for onset in onsets:
        for k in range(int(round(onset * 100)), min(len(frames), int(round(onset * 100)) + 30)):
            frames[k] = 1.0
    monkeypatch.setattr(syncing, "_audio_speech_activity", lambda *args, **kwargs: frames)
    result: dict[str, object] = {
        "engine": "japanese-stt+text-clock-plateaus",
        "cold_open_check": {"applied": True, "moved_cues": [0, 5]},
    }
    syncing._correct_onset_lead(
        tmp_path / "video.mkv", path, result, tmp_path / "cache", syncing.SyncConfig()
    )
    lead = float(result["onset_lead_correction"]["lead_seconds"])
    assert abs(lead - 0.10) < 0.01
