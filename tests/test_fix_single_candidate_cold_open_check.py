"""One file aligned alone: Japanese speech that carries the cold open off a one-clock
subtitle timeline (which the video's embedded reference confirms) does not replace that
timeline, as in the multi-candidate path.  Invented data only."""

from __future__ import annotations

from pathlib import Path

import pytest

import pudge.syncing as syncing
from pudge.config import SyncConfig
from pudge.subtitle_formats import write_exact_srt


def _episode(cold_shift: float):
    cold = [(10.0 + 4.0 * i, 12.5 + 4.0 * i, f"cold{i}") for i in range(12)]
    rest = [(140.0 + 4.0 * i, 142.5 + 4.0 * i, f"line{i}") for i in range(200)]
    timeline = cold + rest
    speech = [(s + cold_shift, e + cold_shift, t) for s, e, t in cold] + rest
    return timeline, speech


@pytest.mark.parametrize("reference_follows", ["timeline", "speech"])
def test_single_file_keeps_the_timeline_when_speech_moves_the_cold_open(
    tmp_path: Path, monkeypatch, reference_follows: str
):
    timeline, speech = _episode(cold_shift=72.0)
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    source = tmp_path / "episode.ja.srt"
    write_exact_srt([(s + 1.0, e + 1.0, t) for s, e, t in timeline], source)
    timeline_out = tmp_path / "timeline.srt"
    write_exact_srt(timeline, timeline_out)
    speech_out = tmp_path / "speech.srt"
    write_exact_srt(speech, speech_out)
    reference = tmp_path / "embedded.en.srt"
    basis = timeline if reference_follows == "timeline" else speech
    write_exact_srt([(s + 0.1, e, "en") for s, e, _t in basis], reference)

    monkeypatch.setattr(
        syncing, "extract_embedded_timing_reference", lambda *a, **k: (reference, {"language": "eng"})
    )
    monkeypatch.setattr(syncing, "_exact_release_zero_offset_result", lambda *a: None)
    monkeypatch.setattr(syncing, "_record_timeline_debug_attempt", lambda *a, **k: None)
    monkeypatch.setattr(syncing, "_timeline_needs_audio_verification", lambda *a: True)
    monkeypatch.setattr(
        syncing,
        "align_subtitle_timelines",
        lambda *a, **k: (
            timeline_out,
            {
                "reason": "applied",
                "sync_was_successful": True,
                "timeline_alignment_reliable": True,
                "engine": "embedded-reference+timeline",
                "offset_seconds": -1.0,
                "timeline_segments": [{"offset_seconds": -1.0, "support": 22, "kind": "stable"}],
            },
        ),
    )
    monkeypatch.setattr(
        syncing,
        "_try_japanese_stt_fallback",
        lambda *a, **k: (
            speech_out,
            {
                "reason": "applied",
                "sync_was_successful": True,
                "reference_alignment_reliable": True,
                "engine": "japanese-stt+alass",
            },
        ),
    )
    path, result = syncing._optimize_subtitle_unguarded(
        video, source, tmp_path / "cache", SyncConfig(engine="alass", use_container_chapters=False)
    )
    if reference_follows == "timeline":
        assert path == timeline_out, result
        conflict = result["speech_clock_conflict"]
        assert conflict["selection_reason"] == "embedded_timeline_over_speech_cold_open_move"
        assert conflict["speech_cold_open_delta_seconds"] == 72.0
        assert result["sync_was_successful"] is True
    else:
        # The reference agrees with speech: the speech clock is used, as before.
        assert path == speech_out
        assert result["embedded_timeline_audio_verification"]["accepted"] is True
