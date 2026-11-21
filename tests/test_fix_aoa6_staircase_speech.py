"""A map on clocks a few seconds apart is checked against the Japanese speech clock, which
replaces it only when more lines then start on English line starts and speech onsets
(invented episodes; Aoashi S2 E06 had this shape: its first six minutes on one ALASS clock
while the TV file drifted from +1.1 to +5.3 s)."""

from __future__ import annotations

import random
from pathlib import Path

from pudge import syncing
from pudge.subtitle_formats import write_srt
from pudge.subtitles import staircase_check

ALIGNMENT = {
    "engine": "alass",
    "timing_reference": "/cache/timing-reference/ref.srt",
    "timing_reference_language": "eng",
    "alass_shift_spread_seconds": 6.2,
    "alass_blocks": 3,
    "alass_distinct_shifts": 3,
}


def _episode(seed: int = 7, count: int = 220) -> list[tuple[float, float, str]]:
    """Dialogue lines on the video's own clock (what a right map gives)."""
    rng = random.Random(seed)
    cues, t = [], 3.0
    for i in range(count):
        t += rng.uniform(2.2, 6.5)
        cues.append((round(t, 3), round(t + rng.uniform(1.0, 2.0), 3), f"台詞{i}です"))
    return cues


def _shifted(cues, shift_of):
    return [(s + shift_of(i), e + shift_of(i), text) for i, (s, e, text) in enumerate(cues)]


def _signals(cues, seed: int = 3):
    rng = random.Random(seed)
    reference = [s + rng.uniform(-0.08, 0.08) for s, _e, _t in cues if rng.random() < 0.85]
    onsets = [s + 0.15 + rng.uniform(-0.1, 0.1) for s, _e, _t in cues if rng.random() < 0.8]
    onsets += [rng.uniform(0, cues[-1][1]) for _ in range(60)]  # breaths, effects
    return sorted(reference), sorted(onsets)


def test_a_map_on_clocks_a_few_seconds_apart_is_a_staircase():
    assert staircase_check.is_staircase_map(ALIGNMENT)
    assert not staircase_check.is_staircase_map({**ALIGNMENT, "alass_shift_spread_seconds": 0.4})
    assert not staircase_check.is_staircase_map({**ALIGNMENT, "alass_shift_spread_seconds": 45.0})
    assert not staircase_check.is_staircase_map({**ALIGNMENT, "alass_blocks": 1, "alass_distinct_shifts": 1})
    assert not staircase_check.is_staircase_map({})


def test_the_right_map_has_more_lines_on_both_signals():
    truth = _episode()
    reference, onsets = _signals(truth)
    wrong = _shifted(truth, lambda i: -4.5 if i < 70 else 0.0)  # the first block 4.5 s early
    right = staircase_check.line_hits(truth, reference, onsets)
    off = staircase_check.line_hits(wrong, reference, onsets)
    assert right["available"] and off["available"]
    assert right["both"] > off["both"] + 30
    assert staircase_check.speech_map_wins(off, right) == (True, "speech_map_on_more_starts")
    assert staircase_check.speech_map_wins(right, off)[0] is False


def test_a_speech_map_no_better_or_worse_on_one_signal_keeps_alass():
    current = {"available": True, "both": 100, "reference": 130, "speech": 120}
    assert (
        staircase_check.speech_map_wins(current, {**current, "both": 107})[1]
        == "speech_map_not_clearly_better"
    )
    worse_reference = {"available": True, "both": 120, "reference": 129, "speech": 140}
    assert staircase_check.speech_map_wins(current, worse_reference)[1] == "one_signal_worse"
    assert staircase_check.speech_map_wins({"available": False}, current)[1] == "signal_unavailable"


def _check(tmp_path: Path, monkeypatch, speech_cues):
    truth = _episode()
    reference, onsets = _signals(truth)
    aligned = tmp_path / "alass.srt"
    write_srt(_shifted(truth, lambda i: -4.5 if i < 70 else 0.0), aligned)
    reference_path = tmp_path / "reference.srt"
    write_srt([(t, t + 1.0, "line") for t in reference], reference_path)
    speech_path = tmp_path / "speech.srt"
    write_srt(speech_cues(truth), speech_path)
    calls = []
    monkeypatch.setattr(syncing, "_episode_speech_onsets", lambda *_a, **_k: onsets)

    def fake_stt(video, subtitle, cache_dir, config, **kwargs):
        calls.append(subtitle)
        return speech_path, {
            "sync_was_successful": True,
            "reference_alignment_reliable": True,
            "engine": "japanese-stt+text-clock-plateaus",
            "reason": "applied",
        }

    monkeypatch.setattr(syncing, "_try_japanese_stt_fallback", fake_stt)
    path, diagnostics = syncing._check_staircase_map_with_speech(
        tmp_path / "video.mkv",
        tmp_path / "source.srt",
        aligned,
        reference_path,
        tmp_path,
        syncing.SyncConfig(),
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        alass_path="alass",
        verbose=False,
    )
    return path, diagnostics, calls, speech_path


def test_a_better_speech_map_replaces_the_staircase(tmp_path, monkeypatch):
    path, diagnostics, calls, speech_path = _check(tmp_path, monkeypatch, lambda truth: truth)
    assert path == speech_path and len(calls) == 1
    assert diagnostics["decision"] == "speech" and diagnostics["result"]["engine"].startswith("japanese-stt")
    assert diagnostics["speech"]["both"] > diagnostics["alass"]["both"]


def test_a_wrong_speech_map_is_not_taken(tmp_path, monkeypatch):
    # one clock 4 s late for the whole episode (a short transcript can do that)
    path, diagnostics, _calls, _speech = _check(
        tmp_path, monkeypatch, lambda truth: _shifted(truth, lambda i: 4.0)
    )
    assert path is None and diagnostics["decision"] == "alass"
    assert diagnostics["reason"] in {"speech_map_not_clearly_better", "one_signal_worse"}


def test_without_speech_onsets_nothing_is_transcribed(tmp_path, monkeypatch):
    monkeypatch.setattr(syncing, "_episode_speech_onsets", lambda *_a, **_k: None)
    monkeypatch.setattr(
        syncing,
        "_try_japanese_stt_fallback",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no STT")),
    )
    path, diagnostics = syncing._check_staircase_map_with_speech(
        tmp_path / "v.mkv",
        tmp_path / "s.srt",
        tmp_path / "a.srt",
        tmp_path / "r.srt",
        tmp_path,
        syncing.SyncConfig(),
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        alass_path="alass",
        verbose=False,
    )
    assert path is None and diagnostics == {
        "version": staircase_check.VERSION,
        "attempted": False,
        "decision": "alass",
        "reason": "speech_onsets_unavailable",
    }


# --- the rebuild rule ---------------------------------------------------------------------


def _selection(alignment, source="jimaku"):
    from pudge.subtitle_migrations import Selection

    return Selection(object(), {"id": 1, "source": source, "details": {"alignment": alignment}})


def test_rebuild_rule_takes_unchecked_reference_staircases_only():
    from pudge.subtitle_migrations import RULES_BY_ID, _staircase_map_unchecked

    rule = RULES_BY_ID["staircase_speech"]
    assert rule.kind == "generation" and rule.scope == "unwatched" and rule.available is not None
    assert _staircase_map_unchecked(_selection(ALIGNMENT))
    assert not _staircase_map_unchecked(
        _selection({**ALIGNMENT, "staircase_speech_check": {"decision": "alass"}})
    )
    assert not _staircase_map_unchecked(_selection(ALIGNMENT, source="manual"))
    assert not _staircase_map_unchecked(_selection({**ALIGNMENT, "timing_reference_language": "jpn"}))
    assert not _staircase_map_unchecked(_selection({**ALIGNMENT, "engine": "japanese-stt+alass"}))
    assert not _staircase_map_unchecked(_selection({**ALIGNMENT, "alass_shift_spread_seconds": 0.5}))
    assert not _staircase_map_unchecked(_selection({}))


def test_rebuild_rule_prepares_an_unwatched_episode_again_once(tmp_path, monkeypatch):
    from subtitle_rebuild_support import assert_rebuilding_with_subtitles_kept
    from t71g08_support import subtitle_library

    from pudge import subtitle_migrations

    monkeypatch.setattr("pudge.subtitles.stt.japanese_stt_available", lambda **_kwargs: True)
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
        details={"alignment": dict(ALIGNMENT)},
    )
    assert subtitle_migrations.run_rule(manager, "staircase_speech") == 1
    assert_rebuilding_with_subtitles_kept(manager, video)
    assert subtitle_migrations.run_rule(manager, "staircase_speech") == 0  # once
