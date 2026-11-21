"""Subtitle onset lead: a timing that starts right at the voice (copied from an English
reference or matched on cue activity) reads late for Japanese subtitles, which lead the
voice slightly.  The lead is measured against the audio's own speech onsets and a late
timing moves earlier (capped, never later).  Invented data only."""

from __future__ import annotations

import random
from pathlib import Path

import pudge.syncing as syncing
from pudge.config import SyncConfig
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles import onset_lead

RATE = 100.0


def _speech(onsets: list[float], duration: float, *, length: float = 1.6) -> list[float]:
    frames = [0.0] * int(duration * RATE)
    for onset in onsets:
        for k in range(int(onset * RATE), min(len(frames), int((onset + length) * RATE))):
            frames[k] = 1.0
    return frames


def _voice_onsets(count: int = 120, seed: int = 4) -> list[float]:
    rng = random.Random(seed)
    times, t = [], 5.0
    for _ in range(count):
        times.append(round(t, 2))
        t += rng.uniform(2.5, 6.0)
    return times


def _cues(onsets: list[float], lead: float, seed: int = 9) -> list[tuple[float, float, str]]:
    rng = random.Random(seed)
    cues = []
    for index, onset in enumerate(onsets):
        start = onset - lead + rng.uniform(-0.04, 0.04)
        cues.append((start, start + 1.8, f"台詞{index}"))
    return cues


def test_speech_onsets_need_a_silence_before():
    frames = [0.0] * 50 + [1.0] * 30 + [0.0] * 5 + [1.0] * 30 + [0.0] * 40 + [1.0] * 10
    assert onset_lead.speech_onsets(frames) == [0.5, 1.55]  # the 50 ms dip is no new onset


def test_timing_at_the_voice_moves_earlier_to_the_japanese_lead():
    onsets = _voice_onsets()
    late = _cues(onsets, lead=0.0)
    measurement = onset_lead.onset_lead([c[0] for c in late], onset_lead.speech_onsets(_speech(onsets, 700)))
    assert abs(float(measurement["lead_seconds"])) < 0.03
    shift = onset_lead.lead_correction(measurement)
    assert abs(shift - min(onset_lead.TARGET_LEAD_SECONDS, onset_lead.MAX_CORRECTION_SECONDS)) < 0.035
    moved = onset_lead.shift_earlier(late, shift)
    assert [round(b - a, 3) for a, b, _ in moved] == [round(b - a, 3) for a, b, _ in late]
    assert all(abs((a - m) - shift) < 1e-6 for (a, _, _), (m, _, _) in zip(late, moved))


def test_timing_that_already_leads_is_kept_and_never_moved_later():
    # A lead beyond every gold track's (0.4 s here before onset-lead-2) does move later:
    # tests/test_fix_tvlead_large_lead.py.
    onsets = _voice_onsets()
    frames = _speech(onsets, 700)
    for lead in (0.15, 0.22):
        cues = _cues(onsets, lead=lead)
        measurement = onset_lead.onset_lead([c[0] for c in cues], onset_lead.speech_onsets(frames))
        assert onset_lead.lead_correction(measurement) == 0.0


def test_correction_is_capped_and_needs_enough_matched_cues():
    onsets = _voice_onsets()
    frames = _speech(onsets, 700)
    very_late = _cues(onsets, lead=-0.3)  # 0.3 s after the voice: a real offset, not a lead
    measurement = onset_lead.onset_lead([c[0] for c in very_late], onset_lead.speech_onsets(frames))
    assert onset_lead.lead_correction(measurement) == onset_lead.MAX_CORRECTION_SECONDS
    few = _cues(onsets[:20], lead=0.0)
    measurement = onset_lead.onset_lead([c[0] for c in few], onset_lead.speech_onsets(frames))
    assert onset_lead.lead_correction(measurement) == 0.0
    # Music everywhere (no onsets near the lines): nothing is measured, nothing moves.
    measurement = onset_lead.onset_lead([c[0] + 2.0 for c in _cues(onsets, 0.0)], [1.0, 900.0])
    assert onset_lead.lead_correction(measurement) == 0.0


def _write_speech(tmp_path: Path, frames: list[float]) -> Path:
    import numpy as np

    path = tmp_path / "speech.npz"
    np.savez_compressed(path, speech=np.asarray(frames, dtype=float))
    return path


def test_final_output_reads_the_audio_not_the_embedded_subtitles(tmp_path: Path, monkeypatch):
    onsets = _voice_onsets()
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    aligned = tmp_path / "aligned.srt"
    late = _cues(onsets, lead=0.0)
    write_srt(late, aligned)
    speech = _write_speech(tmp_path, _speech(onsets, 700))
    detectors: list[str] = []

    def fake_speech(_video, _cache, config, **_kwargs):
        detectors.append(config.vad)
        return speech, {"reason": "applied"}

    monkeypatch.setattr(syncing, "prepare_speech_reference", fake_speech)
    monkeypatch.setattr(syncing, "_has_embedded_text_subtitle", lambda *_a, **_k: True)
    result: dict[str, object] = {"engine": "embedded-reference+timeline"}
    output = syncing._correct_onset_lead(video, aligned, result, tmp_path / "cache", SyncConfig())
    assert detectors == ["webrtc"]  # never the embedded subtitle track as "speech"
    assert output != aligned and result["output"] == str(output)
    info = result["onset_lead_correction"]
    assert info["applied"] is True and float(info["shift_seconds"]) < -0.1
    moved = parse_srt(output)
    assert all(
        abs((a[0] - b[0]) + float(info["shift_seconds"])) < 0.002 for a, b in zip(parse_srt(aligned), moved)
    )


def test_exact_release_timing_is_never_touched(tmp_path: Path, monkeypatch):
    onsets = _voice_onsets()
    aligned = tmp_path / "aligned.srt"
    write_srt(_cues(onsets, lead=0.0), aligned)
    monkeypatch.setattr(
        syncing,
        "prepare_speech_reference",
        lambda *a, **k: (_write_speech(tmp_path, _speech(onsets, 700)), {}),
    )
    result: dict[str, object] = {"engine": "exact-release-crc"}
    assert syncing._correct_onset_lead(tmp_path / "v.mkv", aligned, result, tmp_path, SyncConfig()) == aligned
    assert "onset_lead_correction" not in result


def test_optimize_candidates_applies_the_correction_to_the_chosen_file(tmp_path: Path, monkeypatch):
    from pudge.models import SubtitleCandidate

    onsets = _voice_onsets()
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    source = tmp_path / "ep.ja.srt"
    write_srt(_cues(onsets, lead=0.0), source)
    candidate = SubtitleCandidate(
        path=source, source="jimaku", score=100.0, name=source.name, details={"episode_match": "exact"}
    )
    monkeypatch.setattr(
        syncing,
        "_optimize_candidates_unguarded",
        lambda *a, **k: (
            candidate,
            source,
            {"sync_was_successful": True, "reason": "applied", "engine": "alass"},
        ),
    )
    monkeypatch.setattr(syncing, "_guard_alignment_result", lambda *a, **k: None)
    monkeypatch.setattr(
        syncing, "_recover_rejected_clock", lambda _v, _s, path, result, *a, **k: (path, result)
    )
    monkeypatch.setattr(syncing, "subtitle_quality_accepted", lambda result: (True, "ok"))
    monkeypatch.setattr(
        syncing,
        "prepare_speech_reference",
        lambda *a, **k: (_write_speech(tmp_path, _speech(onsets, 700)), {}),
    )
    chosen, output, result = syncing.optimize_candidates(video, [candidate], tmp_path / "cache", SyncConfig())
    assert chosen is candidate
    assert output is not None and output != source
    assert result["onset_lead_correction"]["applied"] is True
    assert parse_srt(output)[0][0] < parse_srt(source)[0][0] - 0.1
